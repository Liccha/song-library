#!/usr/bin/env python3
"""Build a validated, web-optimized song-library release without touching sources.

Inputs are the canonical files in the local song-library clone. Outputs are
written to a sibling build directory so a failed run cannot damage production.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

try:
    from PIL import Image, ImageOps, features
except ImportError as exc:  # pragma: no cover - preflight error path
    raise SystemExit("Pillow is required: python -m pip install Pillow") from exc


CONFIG_VERSION = "song-library-web-v2-content-addressed"
TARGET_IMAGE_BYTES = 50 * 1024
MAX_IMAGE_EDGE = 256
IMAGE_ATTEMPTS = [
    (256, 82), (256, 78), (256, 74), (256, 70), (256, 66), (256, 62),
    (240, 76), (240, 70), (240, 64), (240, 58),
    (224, 70), (224, 64), (224, 58), (224, 52),
    (208, 62), (208, 56), (208, 50),
    (192, 56), (192, 50), (192, 44),
    (176, 44), (160, 38),
]


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def atomic_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temp, path)


def resolve_tool(name: str) -> str:
    found = shutil.which(name)
    if found:
        return found
    legacy = Path.home() / "Desktop" / "bot" / "ffmpeg-master-latest-win64-gpl" / "bin" / f"{name}.exe"
    if legacy.exists():
        return str(legacy)
    raise RuntimeError(f"Required tool not found: {name}")


def run(command: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace")


def probe_audio(ffprobe: str, path: Path) -> dict:
    result = run([
        ffprobe, "-v", "error", "-show_entries",
        "stream=index,codec_name,codec_type,sample_rate,channels,bit_rate:stream_disposition=attached_pic:format=duration,bit_rate",
        "-of", "json", str(path),
    ])
    if result.returncode != 0:
        raise RuntimeError(f"ffprobe failed: {result.stderr.strip()[:500]}")
    parsed = json.loads(result.stdout)
    audio = next((stream for stream in parsed.get("streams", []) if stream.get("codec_type") == "audio"), None)
    if not audio:
        raise RuntimeError("no audio stream")
    duration = float(parsed.get("format", {}).get("duration") or 0)
    if duration <= 0:
        raise RuntimeError("invalid duration")
    return {
        "codec": str(audio.get("codec_name") or ""),
        "duration": duration,
        "sampleRate": int(audio.get("sample_rate") or 0),
        "channels": int(audio.get("channels") or 0),
        "audioBitrate": int(audio.get("bit_rate") or 0),
        "containerBitrate": int(parsed.get("format", {}).get("bit_rate") or 0),
        "attachedPictures": sum(
            1 for stream in parsed.get("streams", [])
            if int(stream.get("disposition", {}).get("attached_pic") or 0) == 1
        ),
    }


def id3v2_bytes(path: Path) -> int:
    with path.open("rb") as handle:
        header = handle.read(10)
    if len(header) < 10 or header[:3] != b"ID3":
        return 0
    size = 10 + ((header[6] & 0x7F) << 21) + ((header[7] & 0x7F) << 14) + ((header[8] & 0x7F) << 7) + (header[9] & 0x7F)
    if header[5] & 0x10:
        size += 10
    return size


def reusable(previous: dict, key: str, source_hash: str, output: Path) -> dict | None:
    old = previous.get("assets", {}).get(key)
    if not old or old.get("configVersion") != CONFIG_VERSION or old.get("sourceSha256") != source_hash or not output.exists():
        return None
    if old.get("outputSha256") != sha256(output) or old.get("outputBytes") != output.stat().st_size:
        return None
    reused = dict(old)
    reused["reused"] = True
    return reused


def prepare_image(source: Path, output: Path, previous: dict) -> tuple[str, dict]:
    key = f"cover:{source.name}"
    source_hash = sha256(source)
    cached = reusable(previous, key, source_hash, output)
    if cached:
        return key, cached
    output.parent.mkdir(parents=True, exist_ok=True)
    temp = output.with_name(f".{output.stem}.{os.getpid()}.{threading.get_ident()}.tmp.webp")
    smallest: tuple[int, int, int] | None = None
    try:
        with Image.open(source) as opened:
            image = ImageOps.exif_transpose(opened)
            image.load()
            source_size = image.size
            alpha = "A" in image.getbands() or "transparency" in image.info
            base = image.convert("RGBA" if alpha else "RGB")
            selected = None
            for edge, quality in IMAGE_ATTEMPTS:
                candidate = base.copy()
                candidate.thumbnail((min(edge, source_size[0]), min(edge, source_size[1])), Image.Resampling.LANCZOS)
                candidate.save(temp, "WEBP", quality=quality, method=6, exact=alpha)
                size = temp.stat().st_size
                if smallest is None or size < smallest[0]:
                    smallest = (size, edge, quality)
                if size <= TARGET_IMAGE_BYTES:
                    selected = (candidate.size, quality, size)
                    break
            if selected is None:
                # Extremely noisy or transparent artwork: continue reducing only
                # the web derivative. The untouched source remains available.
                candidate = base.copy()
                candidate.thumbnail((144, 144), Image.Resampling.LANCZOS)
                candidate.save(temp, "WEBP", quality=32, method=6, exact=alpha)
                selected = (candidate.size, 32, temp.stat().st_size)
            if selected[2] > TARGET_IMAGE_BYTES:
                raise RuntimeError(f"cannot meet 50 KiB target (best {selected[2]} bytes)")
        with Image.open(temp) as verified:
            verified.load()
            if verified.format != "WEBP" or verified.width <= 0 or verified.height <= 0:
                raise RuntimeError("invalid WebP output")
        os.replace(temp, output)
        return key, {
            "type": "cover",
            "configVersion": CONFIG_VERSION,
            "source": source.name,
            "sourceSha256": source_hash,
            "sourceBytes": source.stat().st_size,
            "sourceWidth": source_size[0],
            "sourceHeight": source_size[1],
            "sourceAlpha": alpha,
            "output": output.name,
            "outputSha256": sha256(output),
            "outputBytes": output.stat().st_size,
            "outputWidth": selected[0][0],
            "outputHeight": selected[0][1],
            "quality": selected[1],
            "reused": False,
        }
    finally:
        temp.unlink(missing_ok=True)


def prepare_audio(source: Path, output: Path, previous: dict, ffmpeg: str, ffprobe: str) -> tuple[str, dict]:
    key = f"preview:{source.name}"
    source_hash = sha256(source)
    cached = reusable(previous, key, source_hash, output)
    if cached:
        return key, cached
    output.parent.mkdir(parents=True, exist_ok=True)
    temp = output.with_name(f".{output.stem}.{os.getpid()}.{threading.get_ident()}.tmp.mp3")
    before = probe_audio(ffprobe, source)
    method = "mp3-bitstream-copy" if before["codec"] == "mp3" else "encode-libmp3lame-vbr-q2"
    common = [ffmpeg, "-y", "-v", "error", "-i", str(source), "-map", "0:a:0", "-map_metadata", "-1", "-map_chapters", "-1"]
    codec = ["-c:a", "copy"] if before["codec"] == "mp3" else ["-c:a", "libmp3lame", "-q:a", "2", "-compression_level:a", "0"]
    result = run(common + codec + ["-write_id3v1", "0", str(temp)])
    try:
        if result.returncode != 0 or not temp.exists() or temp.stat().st_size <= 0:
            raise RuntimeError(f"ffmpeg failed: {result.stderr.strip()[:500]}")
        after = probe_audio(ffprobe, temp)
        if after["codec"] != "mp3":
            raise RuntimeError(f"unexpected output codec: {after['codec']}")
        tolerance = max(0.35, before["duration"] * 0.02)
        if abs(after["duration"] - before["duration"]) > tolerance:
            raise RuntimeError(f"duration changed {before['duration']:.3f}s -> {after['duration']:.3f}s")
        os.replace(temp, output)
        return key, {
            "type": "preview",
            "configVersion": CONFIG_VERSION,
            "source": source.name,
            "sourceSha256": source_hash,
            "sourceBytes": source.stat().st_size,
            "sourceCodec": before["codec"],
            "sourceDuration": round(before["duration"], 6),
            "sourceAudioBitrate": before["audioBitrate"],
            "sourceId3v2Bytes": id3v2_bytes(source),
            "sourceAttachedPictures": before["attachedPictures"],
            "method": method,
            "output": output.name,
            "outputSha256": sha256(output),
            "outputBytes": output.stat().st_size,
            "outputDuration": round(after["duration"], 6),
            "outputAudioBitrate": after["audioBitrate"],
            "reused": False,
        }
    finally:
        temp.unlink(missing_ok=True)


def process_parallel(label: str, jobs: list[tuple], worker, workers: int) -> tuple[dict, list[str]]:
    assets: dict[str, dict] = {}
    errors: list[str] = []
    total = len(jobs)
    completed = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(worker, *job): job[0] for job in jobs}
        for future in concurrent.futures.as_completed(futures):
            completed += 1
            try:
                key, value = future.result()
                assets[key] = value
            except Exception as exc:
                errors.append(f"{futures[future].name}: {exc}")
            if completed == total or completed % 100 == 0:
                print(f"[{label}] {completed}/{total}", flush=True)
    return assets, errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    script = Path(__file__).resolve()
    library = script.parent.parent
    parser.add_argument("--library", type=Path, default=library)
    parser.add_argument("--output", type=Path, default=library.parent / "song-library-build")
    parser.add_argument("--workers", type=int, default=min(4, max(1, os.cpu_count() or 1)))
    parser.add_argument("--public-base", default="__PUBLIC_BASE__")
    args = parser.parse_args()

    library = args.library.resolve()
    output = args.output.resolve()
    if output == library or library in output.parents:
        raise SystemExit("Output must be outside the source library")
    if not features.check("webp"):
        raise SystemExit("This Pillow build does not support WebP")
    ffmpeg = resolve_tool("ffmpeg")
    ffprobe = resolve_tool("ffprobe")
    source_covers = library / "covers"
    source_previews = library / "previews"
    source_data = library / "data" / "songs.json"
    if not source_covers.is_dir() or not source_previews.is_dir() or not source_data.is_file():
        raise SystemExit("Canonical covers, previews, or songs.json input is missing")

    output.mkdir(parents=True, exist_ok=True)
    ready = output / "READY"
    ready.unlink(missing_ok=True)
    previous = {}
    manifest_path = output / "manifest.json"
    if manifest_path.exists():
        try:
            previous = json.loads(manifest_path.read_text(encoding="utf-8"))
        except Exception:
            previous = {}

    covers = sorted(path for path in source_covers.iterdir() if path.is_file())
    previews = sorted(path for path in source_previews.iterdir() if path.is_file())
    cover_stems: dict[str, Path] = {}
    for path in covers:
        if path.stem in cover_stems:
            raise SystemExit(f"Duplicate cover id would collide: {cover_stems[path.stem].name}, {path.name}")
        cover_stems[path.stem] = path
    preview_stems: dict[str, Path] = {}
    for path in previews:
        if path.stem in preview_stems:
            raise SystemExit(f"Duplicate preview id would collide: {preview_stems[path.stem].name}, {path.name}")
        preview_stems[path.stem] = path

    started = time.time()
    print(f"Source: {library}")
    print(f"Output: {output}")
    print(f"Covers: {len(covers)}; previews: {len(previews)}; workers: {args.workers}")
    image_jobs = [(path, output / "covers" / f"{path.stem}.webp", previous) for path in covers]
    image_assets, image_errors = process_parallel("covers", image_jobs, prepare_image, args.workers)
    audio_jobs = [(path, output / "previews" / f"{path.stem}.mp3", previous, ffmpeg, ffprobe) for path in previews]
    audio_assets, audio_errors = process_parallel("previews", audio_jobs, prepare_audio, args.workers)
    errors = image_errors + audio_errors

    songs = json.loads(source_data.read_text(encoding="utf-8"))
    if not isinstance(songs, list):
        errors.append("songs.json root must be an array")
        songs = []
    optimized_songs = []
    referenced_covers: set[str] = set()
    referenced_previews: set[str] = set()
    for source_song in songs:
        song = dict(source_song)
        sid = str(song.get("id", ""))
        cover = output / "covers" / f"{sid}.webp"
        preview = output / "previews" / f"{sid}.mp3"
        if cover.exists():
            fallback_cover = str(source_song.get("cover") or "")
            digest = sha256(cover)[:16]
            song["cover"] = f"{args.public_base.rstrip('/')}/assets/covers/{sid}-{digest}.webp"
            if fallback_cover:
                song["coverFallback"] = fallback_cover
            referenced_covers.add(sid)
        else:
            song.pop("cover", None)
            song.pop("coverFallback", None)
        if preview.exists():
            fallback_preview = str(source_song.get("preview") or "")
            digest = sha256(preview)[:16]
            song["preview"] = f"{args.public_base.rstrip('/')}/assets/previews/{sid}-{digest}.mp3"
            if fallback_preview:
                song["previewFallback"] = fallback_preview
            referenced_previews.add(sid)
        else:
            song.pop("preview", None)
            song.pop("previewFallback", None)
        optimized_songs.append(song)

    if len(optimized_songs) != len(songs):
        errors.append("song count changed while rebuilding JSON")
    output_data = output / "data" / "songs.json"
    atomic_json(output_data, optimized_songs)

    all_assets = {**image_assets, **audio_assets}
    for item in all_assets.values():
        stem = Path(str(item["output"])).stem
        digest = str(item["outputSha256"])[:16]
        if item["type"] == "cover":
            item["objectKey"] = f"assets/covers/{stem}-{digest}.webp"
        else:
            item["objectKey"] = f"assets/previews/{stem}-{digest}.mp3"
    source_bytes = sum(item.get("sourceBytes", 0) for item in all_assets.values())
    output_bytes = sum(item.get("outputBytes", 0) for item in all_assets.values())
    manifest = {
        "schema": 1,
        "configVersion": CONFIG_VERSION,
        "builtAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "sourceRoot": str(library),
        "outputRoot": str(output),
        "publicBase": args.public_base,
        "counts": {
            "songs": len(songs),
            "covers": len(covers),
            "previews": len(previews),
            "songsWithCover": len(referenced_covers),
            "songsWithPreview": len(referenced_previews),
        },
        "bytes": {
            "source": source_bytes,
            "output": output_bytes,
            "saved": source_bytes - output_bytes,
            "savedPercent": round((1 - output_bytes / source_bytes) * 100, 3) if source_bytes else 0,
        },
        "reused": sum(1 for item in all_assets.values() if item.get("reused")),
        "errors": errors,
        "assets": dict(sorted(all_assets.items())),
    }
    atomic_json(manifest_path, manifest)
    report = {
        "ready": not errors,
        "elapsedSeconds": round(time.time() - started, 2),
        "counts": manifest["counts"],
        "bytes": manifest["bytes"],
        "reused": manifest["reused"],
        "errors": errors[:50],
    }
    atomic_json(output / "report.json", report)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if errors:
        return 1
    ready.write_text(f"{manifest['builtAt']}\n{sha256(manifest_path)}\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
