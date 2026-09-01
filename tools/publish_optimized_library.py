#!/usr/bin/env python3
"""Atomically publish the validated optimized library to an isolated OSS bucket.

Credentials are read from a local properties file outside Git. Assets use
content-addressed immutable names; the small songs.json index is uploaded last.
No object is deleted by this publisher, so a failed run cannot remove a live
release and older browser caches continue to work.
"""

from __future__ import annotations

import argparse
import base64
import concurrent.futures
import hashlib
import hmac
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from email.utils import formatdate
from pathlib import Path


PLACEHOLDER = "__PUBLIC_BASE__"
DEFAULT_WORKERS = 6
PUBLISHED_ASSET_INDEX = "data/published-assets-v1.json"
MUTABLE_INDEX_MAX_AGE_SECONDS = 3600
RELEASE_POINTER_MAX_AGE_SECONDS = 60


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def md5_base64(value: bytes) -> str:
    return base64.b64encode(hashlib.md5(value).digest()).decode("ascii")


def atomic_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temp, path)


def load_properties(path: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        result[key.strip()] = value.strip()
    return result


class OssClient:
    def __init__(
        self,
        region: str,
        bucket: str,
        access_id: str,
        secret: str,
        endpoint: str = "",
        timeout_seconds: int = 60,
    ) -> None:
        region = region.strip()
        bucket = bucket.strip()
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{1,61}[a-z0-9]", bucket):
            raise ValueError("ALI_OSS_BUCKET is invalid")
        if not re.fullmatch(r"(?:cn|ap|eu|me|us)-[a-z0-9-]+", region):
            raise ValueError("ALI_OSS_REGION is invalid")
        self.bucket = bucket
        self.access_id = access_id
        self.secret = secret.encode("utf-8")
        endpoint = endpoint.strip().removeprefix("https://").removeprefix("http://").rstrip("/")
        self.endpoint = endpoint or f"oss-{region}.aliyuncs.com"
        self.host = f"{bucket}.{self.endpoint}"
        self.timeout_seconds = max(10, int(timeout_seconds))

    def _authorization(self, method: str, object_key: str, headers: dict[str, str]) -> str:
        lower = {key.lower(): " ".join(str(value).strip().split()) for key, value in headers.items()}
        canonical_headers = "".join(
            f"{key}:{lower[key]}\n" for key in sorted(lower) if key.startswith("x-oss-")
        )
        canonical_resource = f"/{self.bucket}/{object_key}"
        message = "\n".join([
            method,
            lower.get("content-md5", ""),
            lower.get("content-type", ""),
            lower.get("date", ""),
            canonical_headers + canonical_resource,
        ])
        signature = base64.b64encode(hmac.new(self.secret, message.encode("utf-8"), hashlib.sha1).digest()).decode("ascii")
        return f"OSS {self.access_id}:{signature}"

    def request(
        self,
        method: str,
        object_key: str,
        body: bytes | None = None,
        content_type: str = "",
        cache_control: str = "",
        sha256: str = "",
        allow_not_found: bool = False,
    ) -> bytes | None:
        object_key = object_key.lstrip("/")
        headers = {"Date": formatdate(usegmt=True), "Host": self.host}
        if body is not None:
            headers["Content-MD5"] = md5_base64(body)
            headers["Content-Length"] = str(len(body))
        if content_type:
            headers["Content-Type"] = content_type
        if cache_control:
            headers["Cache-Control"] = cache_control
        if sha256:
            headers["x-oss-meta-sha256"] = sha256
        headers["Authorization"] = self._authorization(method, object_key, headers)
        quoted = urllib.parse.quote(object_key, safe="/-_.~")
        request = urllib.request.Request(
            f"https://{self.host}/{quoted}", data=body, headers=headers, method=method
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                return response.read()
        except urllib.error.HTTPError as exc:
            if allow_not_found and exc.code == 404:
                return None
            # OSS puts the decisive NoPermissionType/AuthAction fields near the
            # end of AccessDeniedDetail. Keep the full bounded diagnostic body;
            # it never contains the AccessKey secret or request signature.
            detail = exc.read(4096).decode("utf-8", "replace")
            raise RuntimeError(f"OSS {method} {object_key} failed: HTTP {exc.code} {detail}") from exc

    def exists(self, object_key: str) -> bool:
        return self.request("HEAD", object_key, allow_not_found=True) is not None

    def get(self, object_key: str, allow_not_found: bool = False) -> bytes | None:
        return self.request("GET", object_key, allow_not_found=allow_not_found)

    def put(self, object_key: str, body: bytes, content_type: str, cache_control: str, digest: str) -> None:
        self.request("PUT", object_key, body, content_type, cache_control, digest)


def content_type_for(item: dict) -> str:
    return "image/webp" if item["type"] == "cover" else "audio/mpeg"


def resolve_published_keys(
    client: OssClient,
    object_keys: list[str],
    workers: int = DEFAULT_WORKERS,
    status: dict[str, bool] | None = None,
) -> set[str]:
    """Load one compact cloud index; perform the legacy HEAD scan only once.

    Content-addressed asset keys are immutable and this publisher has no delete
    permission. Therefore an index written after all uploads complete is a safe
    existence cache. A missing or malformed index falls back to the old check
    and is replaced at the end of the successful run.
    """
    expected = set(object_keys)
    body = client.get(PUBLISHED_ASSET_INDEX, allow_not_found=True)
    if body is not None:
        try:
            value = json.loads(body.decode("utf-8"))
            keys = value.get("keys") if isinstance(value, dict) and value.get("schema") == 1 else None
            if isinstance(keys, list) and all(isinstance(key, str) for key in keys):
                if status is not None:
                    status["indexLoaded"] = True
                return expected.intersection(keys)
        except (UnicodeDecodeError, json.JSONDecodeError):
            pass

    if status is not None:
        status["indexLoaded"] = False
    existing: set[str] = set()
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = {pool.submit(client.exists, key): key for key in object_keys}
        for future in concurrent.futures.as_completed(futures):
            if future.result():
                existing.add(futures[future])
    return existing


def published_asset_index_body(object_keys: list[str]) -> bytes:
    return json.dumps(
        {"schema": 1, "keys": sorted(set(object_keys))},
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    script = Path(__file__).resolve()
    library = script.parent.parent
    parser.add_argument("--build", type=Path, default=library.parent / "song-library-build")
    parser.add_argument(
        "--credentials",
        type=Path,
        default=library.parent / "data" / "cloud-library.credentials.properties",
    )
    parser.add_argument("--workers", type=int, default=DEFAULT_WORKERS)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--check", action="store_true", help="verify scoped PutObject/GetObject access only")
    parser.add_argument(
        "--assets-only",
        action="store_true",
        help="upload immutable assets without publishing the live songs.json index",
    )
    args = parser.parse_args()

    build = args.build.resolve()
    credentials = args.credentials.resolve()
    ready = build / "READY"
    manifest_path = build / "manifest.json"
    songs_path = build / "data" / "songs.json"
    if not ready.is_file() or not manifest_path.is_file() or not songs_path.is_file():
        raise SystemExit("Optimized build is not READY; run build_optimized_library.py first")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("errors"):
        raise SystemExit("Optimized manifest contains errors; refusing to publish")
    assets = list(manifest.get("assets", {}).values())
    if not assets or any(not item.get("objectKey") for item in assets):
        raise SystemExit("Manifest has no content-addressed object keys; rebuild with the current builder")

    total_bytes = sum(int(item["outputBytes"]) for item in assets)
    if args.dry_run:
        print(json.dumps({
            "ready": True,
            "assets": len(assets),
            "bytes": total_bytes,
            "gib": round(total_bytes / 1024 ** 3, 4),
            "credentialsPresent": credentials.is_file(),
        }, ensure_ascii=False, indent=2))
        return 0
    if not credentials.is_file():
        raise SystemExit(f"Cloud library credentials are missing: {credentials}")

    props = load_properties(credentials)
    required = ["ALI_OSS_REGION", "ALI_OSS_BUCKET", "ALI_OSS_ACCESS_KEY_ID", "ALI_OSS_ACCESS_KEY_SECRET"]
    missing = [key for key in required if not props.get(key)]
    if missing:
        raise SystemExit("Missing credential settings: " + ", ".join(missing))
    client = OssClient(
        props["ALI_OSS_REGION"], props["ALI_OSS_BUCKET"],
        props["ALI_OSS_ACCESS_KEY_ID"], props["ALI_OSS_ACCESS_KEY_SECRET"],
        props.get("ALI_OSS_ENDPOINT", ""),
    )
    configured_public_base = props.get("ALI_LIBRARY_PUBLIC_BASE", "").rstrip("/")
    public_base = configured_public_base or f"https://{client.host}"
    parsed_base = urllib.parse.urlparse(public_base)
    if parsed_base.scheme != "https" or not parsed_base.netloc or parsed_base.username or parsed_base.password:
        raise SystemExit("ALI_LIBRARY_PUBLIC_BASE must be a plain HTTPS origin without credentials")
    if args.check:
        check_body = json.dumps({
            "publisher": "songbot-library",
            "checkedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }, separators=(",", ":")).encode("utf-8")
        check_key = "data/health/publisher-check.json"
        client.put(check_key, check_body, "application/json; charset=utf-8", "no-store", sha256_bytes(check_body))
        if not client.exists(check_key):
            raise SystemExit("PutObject succeeded but GetObject/HEAD verification failed")
        print(json.dumps({
            "ok": True,
            "bucket": props["ALI_OSS_BUCKET"],
            "region": props["ALI_OSS_REGION"],
            "permissions": ["oss:PutObject", "oss:GetObject"],
            "deletePermissionRequired": False,
        }, ensure_ascii=False, indent=2))
        return 0

    completed = 0
    uploaded = 0
    skipped = 0
    failures: list[str] = []
    asset_keys = [str(item["objectKey"]) for item in assets]
    index_status: dict[str, bool] = {}
    published_keys = resolve_published_keys(client, asset_keys, args.workers, index_status)

    def publish_asset(item: dict) -> str:
        object_key = str(item["objectKey"])
        if object_key in published_keys:
            return "skipped"
        local = build / ("covers" if item["type"] == "cover" else "previews") / str(item["output"])
        if not local.is_file():
            raise RuntimeError(f"missing local output: {local.name}")
        body = local.read_bytes()
        digest = sha256_bytes(body)
        if digest != item["outputSha256"] or len(body) != int(item["outputBytes"]):
            raise RuntimeError(f"local output changed after validation: {local.name}")
        for attempt in range(3):
            try:
                client.put(object_key, body, content_type_for(item), "public,max-age=31536000,immutable", digest)
                return "uploaded"
            except Exception:
                if attempt == 2:
                    raise
                time.sleep(2 ** attempt)
        raise AssertionError("unreachable")

    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        futures = {pool.submit(publish_asset, item): item for item in assets}
        for future in concurrent.futures.as_completed(futures):
            completed += 1
            try:
                result = future.result()
                if result == "uploaded":
                    uploaded += 1
                else:
                    skipped += 1
            except Exception as exc:
                failures.append(f"{futures[future].get('objectKey')}: {exc}")
            if completed == len(assets) or completed % 100 == 0:
                print(f"[OSS] {completed}/{len(assets)} uploaded={uploaded} cached={skipped}", flush=True)

    if failures:
        report = {"ready": False, "uploaded": uploaded, "skipped": skipped, "errors": failures[:50]}
        atomic_json(build / "publish-report.json", report)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 1

    if uploaded or not index_status.get("indexLoaded", False):
        asset_index_body = published_asset_index_body(asset_keys)
        client.put(
            PUBLISHED_ASSET_INDEX,
            asset_index_body,
            "application/json; charset=utf-8",
            "no-store",
            sha256_bytes(asset_index_body),
        )

    if args.assets_only:
        report = {
            "ready": True,
            "uploaded": uploaded,
            "skipped": skipped,
            "assets": len(assets),
            "bytes": total_bytes,
            "indexPublished": False,
        }
        atomic_json(build / "publish-report.json", report)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0

    if not configured_public_base:
        raise SystemExit(
            "ALI_LIBRARY_PUBLIC_BASE is empty; refusing to publish private OSS URLs into live songs.json"
        )

    songs_text = songs_path.read_text(encoding="utf-8").replace(PLACEHOLDER, public_base)
    if PLACEHOLDER in songs_text:
        raise SystemExit("Public-base placeholder remained in songs.json")
    songs = json.loads(songs_text)
    if not isinstance(songs, list) or len(songs) != int(manifest["counts"]["songs"]):
        raise SystemExit("Published songs.json validation failed")
    songs_body = json.dumps(songs, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    release_hash = sha256_bytes(songs_body)
    release_key = f"data/releases/songs-{release_hash[:16]}.json"
    client.put(release_key, songs_body, "application/json; charset=utf-8", "public,max-age=31536000,immutable", release_hash)
    # Atomic switch: the mutable index is the final upload in a successful run.
    client.put(
        "data/songs.json",
        songs_body,
        "application/json; charset=utf-8",
        f"public,max-age={MUTABLE_INDEX_MAX_AGE_SECONDS},stale-while-revalidate=86400",
        release_hash,
    )
    state = {
        "schema": 1,
        "publishedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "release": release_key,
        "releaseSha256": release_hash,
        "assets": len(assets),
        "bytes": total_bytes,
    }
    state_body = json.dumps(state, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    client.put(
        "data/publish-state.json",
        state_body,
        "application/json; charset=utf-8",
        f"public,max-age={RELEASE_POINTER_MAX_AGE_SECONDS},stale-while-revalidate=3600",
        sha256_bytes(state_body),
    )
    report = {"ready": True, "uploaded": uploaded, "skipped": skipped, **state, "publicBase": public_base}
    atomic_json(build / "publish-report.json", report)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("Publish cancelled; live songs.json was not changed.", file=sys.stderr)
        raise SystemExit(130)
