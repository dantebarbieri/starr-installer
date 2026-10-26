#!/usr/bin/env python3
"""Verify the public profile pin, or explicitly refresh it from a homeserver commit."""

import argparse
import hashlib
import json
from pathlib import Path
import re
import sys
import urllib.error
import urllib.request

from install import PROFILE_FILES, PROFILE_PROVENANCE, RECYCLARR_VERSION, verify_profile_snapshot


def refresh(root, commit):
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise ValueError("Refresh requires a full lowercase 40-character homeserver commit SHA.")
    contents = {}
    for name in PROFILE_FILES:
        url = f"https://raw.githubusercontent.com/dantebarbieri/homeserver/{commit}/{name}"
        with urllib.request.urlopen(url, timeout=60) as response:
            data = response.read(1024 * 1024 + 1)
        if len(data) > 1024 * 1024:
            raise ValueError(f"Profile exceeds 1 MiB: {name}")
        contents[name] = data
    manifest = {"repository": "dantebarbieri/homeserver", "commit": commit,
                "recyclarr_version": RECYCLARR_VERSION,
                "profiles": {name: hashlib.sha256(data).hexdigest() for name, data in contents.items()}}
    contents[PROFILE_PROVENANCE] = (json.dumps(manifest, indent=2) + "\n").encode()
    verify_profile_snapshot(root, contents)
    for name, data in contents.items():
        path = root / name
        if path.is_symlink() or root.resolve() not in path.resolve().parents:
            raise ValueError(f"Refusing profile path outside checkout: {name}")
    for name, data in contents.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("verify")
    update = commands.add_parser("refresh")
    update.add_argument("--commit", required=True, help="Reviewed homeserver commit SHA, never a branch or tag")
    args = parser.parse_args(argv)
    root = Path(__file__).resolve().parents[2]
    if args.command == "refresh":
        refresh(root, args.commit)
    manifest = verify_profile_snapshot(root)
    print(f"Verified {len(PROFILE_FILES)} public profiles: {manifest['repository']}@{manifest['commit']} "
          f"(Recyclarr {manifest['recyclarr_version']})")


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, KeyError, TypeError, urllib.error.URLError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        sys.exit(1)
