#!/usr/bin/env python3
"""Reject a library build when managed cloud media is not locally complete."""

from __future__ import annotations

import csv
from pathlib import Path


def validate_source_assets(rows: list[dict[str, str]], cache_root: Path) -> None:
    cache = cache_root.resolve()
    failures: list[str] = []
    for row in rows:
        song_id = str(row.get("id", "") or "").strip() or "?"
        for field in ("image_path", "audio_path"):
            value = str(row.get(field, "") or "").strip()
            if not value:
                continue
            if value.startswith("cloud-object:"):
                failures.append(f"{song_id}:{field}")
                continue
            path = Path(value)
            try:
                path.resolve().relative_to(cache)
            except (OSError, ValueError):
                continue
            try:
                valid = path.is_file() and path.stat().st_size > 0
            except OSError:
                valid = False
            if not valid:
                failures.append(f"{song_id}:{field}")
    if failures:
        preview = ", ".join(failures[:12])
        suffix = " ..." if len(failures) > 12 else ""
        raise RuntimeError(
            f"managed cloud media is not materialized: {preview}{suffix}; publication aborted")


def main() -> int:
    library = Path(__file__).resolve().parents[1]
    songbot = library.parent
    csv_path = songbot / "songs.csv"
    with csv_path.open("r", encoding="utf-8-sig", newline="") as stream:
        rows = list(csv.DictReader(stream))
    validate_source_assets(rows, songbot / "data" / "cloud-library-assets")
    print(f"managed cloud media preflight passed: {len(rows)} songs")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
