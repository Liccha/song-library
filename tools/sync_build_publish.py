#!/usr/bin/env python3
"""Update the canonical library, publish cloud derivatives, then back up GitHub.

Cloud publication and GitHub backup are deliberately independent: a GitHub
outage does not block the primary cloud release, and a cloud outage leaves the
existing release live while GitHub remains available as the website fallback.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path


def run(label: str, command: list[str], cwd: Path, timeout: int) -> int:
    print(f"[{label}] {' '.join(command)}", flush=True)
    try:
        completed = subprocess.run(command, cwd=cwd, timeout=timeout)
        return completed.returncode
    except subprocess.TimeoutExpired:
        print(f"[{label}] timed out after {timeout}s", file=sys.stderr)
        return 124


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--skip-mobile-cloud", action="store_true")
    parser.add_argument("--skip-sync", action="store_true")
    parser.add_argument("--skip-cloud", action="store_true")
    parser.add_argument("--skip-github", action="store_true")
    args = parser.parse_args()

    tools = Path(__file__).resolve().parent
    library = tools.parent
    python = sys.executable
    started = time.time()

    cloud_hydrator = library.parent / "tools" / "sync_mobile_cloud_to_local.py"
    if not args.skip_mobile_cloud and cloud_hydrator.is_file():
        if run("mobile-cloud", [python, str(cloud_hydrator)], library.parent, 1800) != 0:
            print("[mobile-cloud] hydration failed; the existing public release remains live", file=sys.stderr)
            return 1

    if run("media-preflight", [python, str(tools / "source_asset_preflight.py")], library, 120) != 0:
        print("[media-preflight] managed media is incomplete; publication aborted", file=sys.stderr)
        return 1

    if not args.skip_sync and run("source", [python, "sync.py"], library, 600) != 0:
        return 1
    if run("optimize", [python, str(tools / "build_optimized_library.py")], library, 1800) != 0:
        return 1

    cloud_rc = 0
    credentials = library.parent / "data" / "cloud-library.credentials.properties"
    if not args.skip_cloud:
        if credentials.is_file():
            cloud_rc = run("cloud", [python, str(tools / "publish_optimized_library.py")], library, 3600)
        else:
            print(f"[cloud] credentials not configured; keeping the existing cloud release: {credentials}")

    git_rc = 0
    if not args.skip_github:
        add_rc = run("github", ["git", "add", "data/songs.json", "covers/", "previews/", "tools/"], library, 300)
        if add_rc == 0:
            diff = subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=library)
            if diff.returncode == 1:
                git_rc = run("github", ["git", "commit", "-m", "Library update"], library, 300)
            elif diff.returncode != 0:
                git_rc = diff.returncode
            if git_rc == 0:
                git_rc = run("github", ["git", "push", "origin", "master"], library, 900)
                if git_rc != 0:
                    print("[github] remote is ahead; trying one safe rebase", flush=True)
                    pull_rc = run(
                        "github", ["git", "pull", "--rebase", "--autostash", "origin", "master"],
                        library, 900,
                    )
                    if pull_rc == 0:
                        git_rc = run("github", ["git", "push", "origin", "master"], library, 900)
                    else:
                        subprocess.run(["git", "rebase", "--abort"], cwd=library)
                        git_rc = pull_rc
        else:
            git_rc = add_rc

    elapsed = round(time.time() - started, 2)
    print(f"[done] elapsed={elapsed}s cloud={cloud_rc} github={git_rc}")
    # A cloud failure matters once credentials exist, even if the backup worked.
    return cloud_rc or git_rc


if __name__ == "__main__":
    raise SystemExit(main())
