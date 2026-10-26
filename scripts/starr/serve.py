#!/usr/bin/env python3
"""Read-only public download host. Only explicitly listed public files leave it."""

from collections import OrderedDict
import gzip
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import re
import tarfile
import threading
from urllib.parse import urlsplit

from install import PROFILE_FILES, PROFILE_PROVENANCE, verify_profile_snapshot


PUBLIC_FILES = (
    "scripts/starr/install.py", "scripts/starr/README.md",
    *PROFILE_FILES, PROFILE_PROVENANCE,
) + tuple("scripts/starr/site/icons/" + name for name in (
    "radarr.svg", "sonarr.svg", "lidarr.svg", "prowlarr.svg", "bazarr.svg", "qbittorrent.svg",
    "sabnzbd.svg", "slskd.svg", "plex.svg", "jellyfin.svg", "seerr.svg", "LICENSE", "NOTICE"))
MAX_SOURCE = 8 * 1024 * 1024
SITE_FILES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/styles.css": ("styles.css", "text/css; charset=utf-8"),
    "/app.mjs": ("app.mjs", "text/javascript; charset=utf-8"),
}
SITE_FILES.update({"/icons/" + name.rsplit("/", 1)[1]: (
    "icons/" + name.rsplit("/", 1)[1], "image/svg+xml" if name.endswith(".svg") else "text/plain; charset=utf-8")
    for name in PUBLIC_FILES if name.startswith("scripts/starr/site/icons/")})


class Releases:
    def __init__(self, source, origin):
        self.source = Path(source)
        parsed = urlsplit(origin)
        if (parsed.scheme not in ("https", "http") or not parsed.netloc or parsed.username or
                parsed.password or parsed.path not in ("", "/") or parsed.query or parsed.fragment or
                (parsed.scheme == "http" and parsed.hostname not in ("127.0.0.1", "localhost", "::1"))):
            raise ValueError("PUBLIC_URL must be an HTTPS origin (HTTP loopback is for tests only).")
        self.origin = origin.rstrip("/")
        self.releases = OrderedDict()
        self.lock = threading.Lock()

    def current(self):
        with self.lock:
            # Compare all inputs before/after reading; never publish a detected
            # concurrent checkout write as a release.
            paths = [self.source / name for name in (*PUBLIC_FILES, "scripts/starr/bootstrap.py")]
            before = [(p.stat().st_mtime_ns, p.stat().st_size, p.stat().st_ino) for p in paths]
            if any(p.is_symlink() or not p.is_file() or self.source.resolve() not in p.resolve().parents
                   for p in paths):
                raise ValueError("Public source files must be regular files, not symlinks.")
            if sum(size for _, size, _ in before) > MAX_SOURCE:
                raise ValueError("Public source snapshot is too large.")
            contents = [p.read_bytes() for p in paths]
            after = [(p.stat().st_mtime_ns, p.stat().st_size, p.stat().st_ino) for p in paths]
            if before != after:
                raise ValueError("Source changed during snapshot; retry after the checkout update.")
            verify_profile_snapshot(self.source, dict(zip(PUBLIC_FILES, contents)))
            archive = io.BytesIO()
            with tarfile.open(fileobj=archive, mode="w") as output:
                for name, data in zip(PUBLIC_FILES, contents):
                    member = tarfile.TarInfo(name)
                    member.size = len(data)
                    member.mode = 0o644
                    output.addfile(member, io.BytesIO(data))
            bundle = gzip.compress(archive.getvalue(), mtime=0)
            digest = hashlib.sha256(bundle).hexdigest()
            bootstrap = contents[-1].decode().replace('"@ORIGIN@"', repr(self.origin)).replace(
                '"@DIGEST@"', repr(digest)).encode()
            shell = (b"#!/bin/sh\nset -eu\n"
                     b"command -v python3 >/dev/null 2>&1 || { echo 'Python 3.10+ is required.' >&2; exit 1; }\n"
                     b"exec python3 - \"$@\" <<'STARR_BOOTSTRAP_PY'\n" +
                     bootstrap + b"\nSTARR_BOOTSTRAP_PY\n")
            release = {"bundle": bundle, "python": bootstrap, "shell": shell, "sha256": digest}
            self.releases[digest] = release
            self.releases.move_to_end(digest)
            while len(self.releases) > 8:
                self.releases.popitem(last=False)
            return release

    def get(self, digest):
        with self.lock:
            return self.releases.get(digest)

    def site_file(self, route):
        path = self.source / "scripts" / "starr" / "site" / SITE_FILES[route][0]
        if path.is_symlink() or self.source.resolve() not in path.resolve().parents or not path.is_file():
            raise ValueError("Guide assets must be regular files inside the source directory.")
        with path.open("rb") as stream:
            data = stream.read(MAX_SOURCE + 1)
        if len(data) > MAX_SOURCE:
            raise ValueError("Guide asset is too large.")
        return data


class DownloadServer(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 32

    def __init__(self, address, releases):
        self.releases = releases
        super().__init__(address, Handler)


class Handler(BaseHTTPRequestHandler):
    server_version = "StarrDownloads"
    sys_version = ""
    timeout = 10

    def do_HEAD(self):
        self.do_GET()

    def reply(self, status, body, kind="text/plain; charset=utf-8", immutable=False):
        self.send_response(status)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy",
                         "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self'; "
                         "connect-src 'none'; form-action 'none'; frame-ancestors 'none'; base-uri 'none'")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cache-Control", "public, max-age=31536000, immutable" if immutable else "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def do_GET(self):
        path = urlsplit(self.path).path
        match = re.fullmatch(r"/releases/([0-9a-f]{64})/bundle\.tar\.gz", path)
        if match:
            release = self.server.releases.get(match[1])
            if release is None:
                self.reply(404, b"Release expired or host restarted; download install.sh again.\n")
            else:
                self.reply(200, release["bundle"], "application/gzip", immutable=True)
            return
        if path not in (*SITE_FILES, "/i", "/install.sh", "/install.py", "/release.json", "/healthz"):
            self.reply(404, b"Not found.\n")
            return
        try:
            if path in SITE_FILES:
                self.reply(200, self.server.releases.site_file(path), SITE_FILES[path][1])
                return
            release = self.server.releases.current()
            if path == "/healthz":
                for route in SITE_FILES:
                    self.server.releases.site_file(route)
        except (OSError, ValueError) as error:
            self.log_error("Public release unavailable: %s", error)
            self.reply(503, b"Public release unavailable; retry after the repository update.\n")
            return
        if path == "/healthz":
            self.reply(200, b"ok\n")
        elif path in ("/i", "/install.sh"):
            self.reply(200, release["shell"])
        elif path == "/install.py":
            self.reply(200, release["python"])
        elif path == "/release.json":
            self.reply(200, json.dumps({"sha256": release["sha256"],
                                       "bundle": f"/releases/{release['sha256']}/bundle.tar.gz"}).encode(),
                       "application/json")


if __name__ == "__main__":
    source = os.environ.get("SOURCE_ROOT", "/source")
    releases = Releases(source, os.environ.get("PUBLIC_URL", "https://starr.danteb.com"))
    releases.current()
    DownloadServer(("0.0.0.0", 8080), releases).serve_forever()
