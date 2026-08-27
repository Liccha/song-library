#!/usr/bin/env python3
"""Initialize the private mobile cloud dataset from the canonical local SQLite database.

The desktop authorization token is read locally and is never printed or written to the
snapshot. Existing cloud datasets are never overwritten by this one-time bootstrap.
"""

from __future__ import annotations

import json
import sqlite3
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
DATABASE = ROOT / "song_data.db"
CONFIG = ROOT / "data" / "cloud-announcement.properties"
MAX_RESPONSE = 256 * 1024


def properties(path: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        result[key.strip()] = value.strip()
    return result


def endpoint(api: str) -> str:
    parsed = urllib.parse.urlparse(api)
    if parsed.scheme != "https" or parsed.hostname not in {"editor.teacharm.moe", "bot-editor.vercel.app"}:
        raise RuntimeError("cloud API origin is not trusted")
    return urllib.parse.urlunparse(("https", parsed.netloc, "/api/mobile-data", "", "", ""))


def table(connection: sqlite3.Connection, name: str) -> dict[str, object]:
    columns = [str(row[1]) for row in connection.execute(f'PRAGMA table_info("{name}")')]
    if not columns:
        raise RuntimeError(f"missing SQLite table: {name}")
    rows = []
    for record in connection.execute(f'SELECT * FROM "{name}" ORDER BY "{columns[0]}"'):
        rows.append({columns[index]: "" if value is None else str(value) for index, value in enumerate(record)})
    if not rows:
        raise RuntimeError(f"refusing to upload empty dataset: {name}")
    return {"columns": columns, "items": rows}


def post(url: str, action: str, token: str, value: dict[str, object]) -> tuple[int, dict[str, object]]:
    request = urllib.request.Request(
        f"{url}?action={urllib.parse.quote(action)}",
        data=json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
        headers={"Authorization": f"Desktop {token}", "Content-Type": "application/json; charset=utf-8", "Accept": "application/json"},
        method="POST",
    )
    try:
        response = urllib.request.urlopen(request, timeout=90)
    except urllib.error.HTTPError as error:
        response = error
    body = response.read(MAX_RESPONSE + 1)
    if len(body) > MAX_RESPONSE:
        raise RuntimeError("cloud response is too large")
    try:
        decoded = json.loads(body.decode("utf-8")) if body else {}
    except Exception as error:
        raise RuntimeError("cloud returned invalid JSON") from error
    return int(response.status), decoded


def main() -> int:
    if not DATABASE.is_file() or not CONFIG.is_file():
        raise RuntimeError("local database or cloud configuration is missing")
    cfg = properties(CONFIG)
    token = cfg.get("desktopToken", "")
    api = endpoint(cfg.get("api", ""))
    if len(token) < 24:
        raise RuntimeError("desktop cloud token is missing")
    with sqlite3.connect(DATABASE) as connection:
        datasets = {
            "bootstrap-songs": table(connection, "songs"),
            "bootstrap-stable": table(connection, "stable_info"),
        }
    results = {}
    for action, value in datasets.items():
        status, response = post(api, action, token, value)
        if status == 409:
            results[action] = {"unchanged": True, "reason": "already initialized"}
        elif 200 <= status < 300:
            results[action] = {"initialized": True, "rows": response.get("total"), "revision": response.get("revision")}
        else:
            raise RuntimeError(f"{action} failed with HTTP {status}: {response.get('error', 'unknown')}")
    print(json.dumps({"ok": True, "datasets": results}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(f"mobile cloud bootstrap failed: {error}", file=sys.stderr)
        raise SystemExit(1)
