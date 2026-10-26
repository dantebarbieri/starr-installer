#!/usr/bin/env python3
"""Download and run one checksum-verified portable Starr installer release."""

import hashlib
import io
from pathlib import Path, PurePosixPath
import subprocess
import sys
import tarfile
import tempfile
import urllib.error
import urllib.request


ORIGIN = "@ORIGIN@"
DIGEST = "@DIGEST@"
MAX_DOWNLOAD = 8 * 1024 * 1024
MAX_EXTRACTED = 32 * 1024 * 1024


def extract_bundle(data, destination):
    total = 0
    names = set()
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
        for member in archive:
            path = PurePosixPath(member.name)
            if (not member.isfile() or path.is_absolute() or ".." in path.parts or
                    "\\" in member.name or not path.parts or
                    path.parts[0] not in ("scripts", "recyclarr-configs") or member.name in names):
                raise ValueError("Unsafe installer archive member.")
            total += member.size
            if total > MAX_EXTRACTED or len(names) >= 256:
                raise ValueError("Installer archive is too large.")
            names.add(member.name)
            target = destination.joinpath(*path.parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            source = archive.extractfile(member)
            if source is None:
                raise ValueError("Unreadable installer archive member.")
            with source, target.open("xb") as output:
                output.write(source.read())
    if "scripts/starr/install.py" not in names:
        raise ValueError("Installer archive has no entry point.")


def main():
    if sys.version_info < (3, 10):
        raise ValueError("Python 3.10+ is required.")
    url = f"{ORIGIN}/releases/{DIGEST}/bundle.tar.gz"
    with urllib.request.urlopen(url, timeout=60) as response:
        data = response.read(MAX_DOWNLOAD + 1)
    if len(data) > MAX_DOWNLOAD or hashlib.sha256(data).hexdigest() != DIGEST:
        raise ValueError("Installer bundle checksum/size verification failed; nothing was executed.")
    with tempfile.TemporaryDirectory(prefix="starr-download-") as directory:
        root = Path(directory)
        extract_bundle(data, root)
        command = [sys.executable, str(root / "scripts" / "starr" / "install.py"), *sys.argv[1:]]
        # Piped interpreters consume stdin as program text. Prompts need the
        # controlling terminal; unattended callers instead supply CLI options.
        if sys.stdin.isatty():
            return subprocess.call(command)
        try:
            terminal = open("/dev/tty")
        except OSError:
            return subprocess.call(command, stdin=subprocess.DEVNULL)
        with terminal:
            return subprocess.call(command, stdin=terminal)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, ValueError, tarfile.TarError, urllib.error.URLError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        sys.exit(1)
