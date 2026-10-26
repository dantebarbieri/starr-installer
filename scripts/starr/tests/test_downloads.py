import contextlib
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tarfile
import tempfile
import threading
import unittest
from unittest.mock import patch
import urllib.error
import urllib.request

from test_install import SOURCE, docker_diagnostics, installer, settings


def load(name):
    spec = importlib.util.spec_from_file_location("starr_" + name, SOURCE / "scripts" / "starr" / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


host = load("serve")
bootstrap = load("bootstrap")
profiles = load("profiles")


@unittest.skipUnless(sys.platform == "linux", "Installer setup paths are Linux-only")
class CliTests(unittest.TestCase):
    def test_noninteractive_flags_set_options_without_prompts(self):
        parser = installer.argument_parser()
        args = parser.parse_args(["--non-interactive", "--yes", "--allow-direct", "--content", "/srv/new-media",
                                  "--uid", "1234", "--gid", "1235", "--timezone", "America/Chicago",
                                  "--sabnzbd", "--jellyfin", "--no-plex", "--auto-update"])
        installer.validate_options(parser, args)
        with patch.object(installer, "ask", side_effect=AssertionError("Unexpected prompt")), \
                patch.object(installer, "yes", side_effect=AssertionError("Unexpected prompt")):
            config = installer.collect(Path("/opt/starr"), args)
        self.assertEqual(config["content"], str(Path("/srv/new-media").resolve()))
        self.assertEqual((config["uid"], config["gid"]), (1234, 1235))
        self.assertTrue(config["sabnzbd"])
        self.assertTrue(config["jellyfin"])
        self.assertTrue(config["auto_update"])
        self.assertFalse(config["plex"])
        self.assertIsNone(config["vpn"])

    def test_noninteractive_requires_explicit_change_and_privacy_consent(self):
        parser = installer.argument_parser()
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            installer.validate_options(parser, parser.parse_args(["--non-interactive"]))
        args = parser.parse_args(["--non-interactive", "--yes"])
        with self.assertRaises(installer.InstallError):
            installer.collect(Path("/opt/starr"), args)

    def test_proton_profile_flags_and_password_files(self):
        with tempfile.TemporaryDirectory() as directory:
            profile = Path(directory) / "wg.conf"
            profile.write_text("dummy profile")
            parser = installer.argument_parser()
            args = parser.parse_args(["--non-interactive", "--yes", "--vpn", "proton",
                                      "--vpn-profile", str(profile), "--vpn-country", "Canada",
                                      "--no-vpn-port-forwarding", "--vpn-static-port", "56789"])
            installer.validate_options(parser, args)
            config = installer.collect(Path("/opt/starr"), args)
            self.assertEqual(config["vpn"]["country"], "Canada")
            self.assertEqual(config["vpn"]["static_port"], 56789)
            self.assertFalse(config["vpn"]["forwarding"])
            password = Path(directory) / "password"
            password.write_text("not-a-real-secret\n")
            self.assertEqual(installer.password_file(password), "not-a-real-secret")

    def test_invalid_flag_combinations_fail_before_docker(self):
        for options in (["--vpn", "none", "--vpn-profile", "/tmp/wg.conf"],
                        ["--no-proxy", "--proxy-domain", "starr.home.arpa"],
                        ["--proxy", "--proxy-port", "8080"],
                        ["--proxy", "--proxy-domain", "public.example"],
                        ["--access-host", "https://server"],
                        ["--non-interactive", "--yes", "--proxy-port", "8088"],
                        ["--subtitle-language", "english"],
                        ["--non-interactive", "--yes", "--vpn", "wireguard"],
                        ["--non-interactive", "--yes", "--soulseek-username", "ignored"],
                        ["--non-interactive", "--yes", "--usenet-server-file", "/tmp/usenet.json"],
                        ["--non-interactive", "--yes", "--plex-claim-file", "/tmp/claim"],
                        ["--non-interactive", "--yes", "--request-profile", "HD - 1080p"],
                        ["--non-interactive", "--yes", "--seerr-media-server", "jellyfin"],
                        ["--no-sabnzbd", "--usenet-server-file", "/tmp/usenet.json"],
                        ["--no-plex", "--plex-claim-file", "/tmp/claim"],
                        ["--no-seerr", "--request-profile", "HD - 1080p"],
                        ["update", "--content", "/srv/ignored"],
                        ["--client", "qbittorrent"],
                        ["--vpn", "openvpn", "--vpn-port-forwarding"],
                        ["--vpn", "wireguard", "--vpn-country", "Canada"],
                        ["--vpn", "proton", "--vpn-username", "ignored"],
                        ["--vpn", "proton", "--vpn-port-forwarding", "--vpn-static-port", "12345"]):
            with self.subTest(options=options), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    parser = installer.argument_parser()
                    installer.validate_options(parser, parser.parse_args(options))

    def test_saved_flags_are_repeat_safe_but_not_silently_ignored(self):
        parser = installer.argument_parser()
        c = settings()
        args = parser.parse_args(["--content", c["content"], "--uid", str(c["uid"]), "--no-plex"])
        installer.saved_options(c, args)
        with self.assertRaises(installer.InstallError):
            installer.saved_options(c, parser.parse_args(["--content", "/srv/different"]))
        installer.saved_options(c, parser.parse_args(["--no-auto-update", "--no-sync-profiles"]))
        self.assertFalse(c["sync_profiles"])

    def test_guided_choices_cannot_silently_discard_supplied_flags(self):
        args = installer.argument_parser().parse_args(["--vpn-country", "Canada"])
        with patch.object(installer, "ask", return_value="2"):
            with self.assertRaisesRegex(installer.InstallError, "only to Proton"):
                installer.vpn_questions("qBittorrent", args)
        args = installer.argument_parser().parse_args(
            ["--non-interactive", "--yes", "--allow-direct", "--soulseek",
             "--soulseek-password-file", "/tmp/not-read"])
        with self.assertRaisesRegex(installer.InstallError, "requires a network username"):
            installer.collect(Path("/opt/starr"), args)


class ReleaseTests(unittest.TestCase):
    def test_profiles_are_verified_before_hosting_and_initial_seeding(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in (*host.PUBLIC_FILES, "scripts/starr/bootstrap.py"):
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(SOURCE / name, path)
            (root / installer.PROFILE_FILES[0]).write_text("unexpected profile policy")
            with self.assertRaisesRegex(ValueError, "checksum mismatch"):
                host.Releases(root, "https://starr.danteb.com").current()
            c = settings(root=root / "install", content=root / "content")
            with patch.object(installer, "owned_directory") as mkdir:
                with self.assertRaisesRegex(installer.InstallError, "unverified profile snapshot"):
                    installer.initialize(c, root)
                mkdir.assert_not_called()

    def test_provenance_rejects_missing_extra_malformed_and_wrong_version_policy(self):
        contents = {name: (SOURCE / name).read_bytes()
                    for name in (*installer.PROFILE_FILES, installer.PROFILE_PROVENANCE)}
        original = json.loads(contents[installer.PROFILE_PROVENANCE])
        for manifest in ([], {**original, "commit": "main"}, {**original, "commit": []},
                         {**original, "recyclarr_version": "latest"},
                         {**original, "profiles": {}}, {**original, "profiles": []},
                         {**original, "profiles": {**original["profiles"], "secrets.yml": "0" * 64}}):
            with self.subTest(manifest=manifest):
                contents[installer.PROFILE_PROVENANCE] = json.dumps(manifest).encode()
                with self.assertRaises(ValueError):
                    installer.verify_profile_snapshot(SOURCE, contents)

    def test_profile_refresh_fetches_only_pinned_public_allowlist_and_verifies(self):
        commit = "a" * 40
        requested = []

        def fetch(url, timeout):
            prefix = f"https://raw.githubusercontent.com/dantebarbieri/homeserver/{commit}/"
            self.assertTrue(url.startswith(prefix))
            name = url.removeprefix(prefix)
            self.assertIn(name, installer.PROFILE_FILES)
            requested.append(name)
            return io.BytesIO((SOURCE / name).read_bytes())

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.object(profiles.urllib.request, "urlopen", side_effect=fetch):
                profiles.refresh(root, commit)
            self.assertEqual(requested, list(installer.PROFILE_FILES))
            self.assertEqual(installer.verify_profile_snapshot(root)["commit"], commit)
            before = (root / installer.PROFILE_PROVENANCE).read_bytes()
            with patch.object(profiles.urllib.request, "urlopen", side_effect=OSError("upstream failed")):
                with self.assertRaises(OSError):
                    profiles.refresh(root, "b" * 40)
            self.assertEqual((root / installer.PROFILE_PROVENANCE).read_bytes(), before)
            for mutable in ("main", "v1.0.0", "../escape"):
                with self.assertRaisesRegex(ValueError, "full lowercase"):
                    profiles.refresh(root, mutable)

    def test_release_is_deterministic_and_never_includes_private_files(self):
        releases = host.Releases(SOURCE, "https://starr.danteb.com")
        one, two = releases.current(), releases.current()
        self.assertEqual(one["bundle"], two["bundle"])
        self.assertEqual(hashlib.sha256(one["bundle"]).hexdigest(), one["sha256"])
        with tarfile.open(fileobj=io.BytesIO(one["bundle"]), mode="r:gz") as archive:
            self.assertEqual(set(archive.getnames()), set(host.PUBLIC_FILES))
            self.assertNotIn("secrets.yml", str(archive.getnames()))
        self.assertNotIn(b"@ORIGIN@", one["python"])
        self.assertNotIn(b"@DIGEST@", one["python"])

    def test_host_requires_https_except_loopback_tests(self):
        for origin in ("http://starr.danteb.com", "https://example.com/path", "https://user:pass@example.com"):
            with self.assertRaises(ValueError):
                host.Releases(SOURCE, origin)

    def test_checkout_changes_publish_new_release_and_keep_previous_bundle(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in (*host.PUBLIC_FILES, "scripts/starr/bootstrap.py"):
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(SOURCE / name, path)
            (root / "recyclarr-configs" / "secrets.yml").write_text("must never ship")
            releases = host.Releases(root, "https://starr.danteb.com")
            before = releases.current()
            script = root / "scripts" / "starr" / "install.py"
            script.write_text(script.read_text() + "\n# changed deployment\n")
            after = releases.current()
            self.assertNotEqual(before["sha256"], after["sha256"])
            self.assertEqual(releases.get(before["sha256"])["bundle"], before["bundle"])
            self.assertNotIn(b"must never ship", after["bundle"])

    def test_bootstrap_checks_digest_before_running_anything(self):
        response = contextlib.nullcontext(io.BytesIO(b"modified bundle"))
        with patch.object(bootstrap.urllib.request, "urlopen", return_value=response), \
                patch.object(bootstrap.subprocess, "call") as execute:
            with self.assertRaisesRegex(ValueError, "verification failed"):
                bootstrap.main()
            execute.assert_not_called()

    def test_extractor_rejects_traversal_links_duplicate_and_wrong_root(self):
        for name, link, duplicate in (("../escape", False, False), ("/escape", False, False),
                                      ("scripts/link", True, False), ("private/state.json", False, False),
                                      ("scripts/starr/install.py", False, True)):
            data = io.BytesIO()
            with tarfile.open(fileobj=data, mode="w:gz") as archive:
                for _ in range(2 if duplicate else 1):
                    member = tarfile.TarInfo(name)
                    member.type = tarfile.SYMTYPE if link else tarfile.REGTYPE
                    member.linkname = "/etc/passwd" if link else ""
                    archive.addfile(member, io.BytesIO(b""))
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                with self.assertRaises(ValueError):
                    bootstrap.extract_bundle(data.getvalue(), Path(directory))


class HttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.releases = host.Releases(SOURCE, "http://127.0.0.1")
        cls.server = host.DownloadServer(("127.0.0.1", 0), cls.releases)
        cls.origin = "http://127.0.0.1:" + str(cls.server.server_address[1])
        cls.releases.origin = cls.origin
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def fetch(self, path):
        return urllib.request.urlopen(self.origin + path, timeout=10)

    def test_routes_and_private_paths(self):
        with self.fetch("/release.json") as response:
            release = json.load(response)
        with self.fetch(release["bundle"]) as response:
            self.assertEqual(hashlib.sha256(response.read()).hexdigest(), release["sha256"])
            self.assertIn("immutable", response.headers["Cache-Control"])
        for path in ("/.env", "/secrets.yml", "/recyclarr-configs/secrets.yml", "/../install.py"):
            with self.assertRaises(urllib.error.HTTPError) as error:
                self.fetch(path)
            self.assertEqual(error.exception.code, 404)
        with self.fetch("/install.py") as response:
            self.assertEqual(response.headers["Cache-Control"], "no-store")

    def test_short_entry_point_and_prominent_typeable_command(self):
        with self.fetch("/i") as response:
            self.assertEqual(response.geturl(), self.origin + "/i")
            self.assertEqual(response.headers["Cache-Control"], "no-store")
            program = response.read()
        with self.fetch("/install.sh") as response:
            self.assertEqual(program, response.read())
        with self.fetch("/") as response:
            page = response.read().decode()
        command = "curl -fsSL https://starr.danteb.com/i | sudo sh"
        self.assertLessEqual(len(command), 55)
        self.assertIn('<code id="quick-command">' + command + "</code>", page)
        self.assertLess(page.index('id="quick-command"'), page.index('id="prepare"'))
        self.assertIn(command, (SOURCE / "scripts" / "starr" / "README.md").read_text())

    def test_static_guide_assets_and_restrictive_policy(self):
        for route, (name, kind) in host.SITE_FILES.items():
            with self.subTest(route=route), self.fetch(route) as response:
                self.assertEqual(response.headers["Content-Type"], kind)
                self.assertEqual(response.headers["Referrer-Policy"], "no-referrer")
                policy = response.headers["Content-Security-Policy"]
                self.assertIn("script-src 'self'", policy)
                self.assertIn("connect-src 'none'", policy)
                self.assertIn("form-action 'none'", policy)
                self.assertNotIn("unsafe-inline", policy)
                self.assertEqual(response.read(), (SOURCE / "scripts" / "starr" / "site" / name).read_bytes())
        for route in ("/site/index.html", "/site/../install.py", "/styles.css/../state.json"):
            with self.assertRaises(urllib.error.HTTPError) as error:
                self.fetch(route)
            self.assertEqual(error.exception.code, 404)

    def test_health_and_head_fail_if_public_sources_are_unavailable(self):
        request = urllib.request.Request(self.origin + "/healthz", method="HEAD")
        with urllib.request.urlopen(request, timeout=10) as response:
            self.assertEqual(response.status, 200)
            self.assertEqual(response.read(), b"")
            self.assertEqual(response.headers["Content-Length"], "3")
        with patch.object(self.releases, "current", side_effect=OSError("missing public source")):
            with self.assertRaises(urllib.error.HTTPError) as error:
                self.fetch("/healthz")
            self.assertEqual(error.exception.code, 503)

    def test_python_pipe_forwards_help_and_errors(self):
        with self.fetch("/install.py") as response:
            program = response.read().decode()
        result = subprocess.run([sys.executable, "-", "--help"], input=program,
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--content", result.stdout)
        self.assertIn("--no-auto-update", result.stdout)
        result = subprocess.run([sys.executable, "-", "--non-interactive"], input=program,
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 2)
        self.assertIn("requires --yes", result.stderr)

    @unittest.skipUnless(sys.platform == "linux", "Shell pipe entry points run on Linux")
    def test_sh_and_bash_pipe_forward_help(self):
        with self.fetch("/i") as response:
            program = response.read().decode()
        for shell in ("sh", "bash"):
            result = subprocess.run([shell, "-s", "--", "--help"], input=program,
                                    capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("--vpn-profile", result.stdout)


@unittest.skipUnless(os.environ.get("STARR_DOCKER_TESTS") == "1", "Opt-in local Linux Docker")
class DockerDownloadTests(unittest.TestCase):
    def test_self_contained_image_and_piped_installer(self):
        with tempfile.TemporaryDirectory(prefix="starr-host-test-") as directory:
            root = Path(directory)
            image = "starr-host-test:" + root.name
            installer.run(["docker", "build", "--pull", "-t", image, SOURCE])
            metadata = json.loads(installer.run(
                ["docker", "image", "inspect", image], capture_output=True, text=True).stdout)[0]["Config"]
            self.assertEqual(metadata["User"], "65534:65534")
            self.assertFalse(metadata.get("Volumes"))
            with socket.socket() as sock:
                sock.bind(("127.0.0.1", 0))
                port = sock.getsockname()[1]
            origin = f"http://127.0.0.1:{port}"
            service = {"image": image, "read_only": True, "cap_drop": ["ALL"],
                       "security_opt": ["no-new-privileges:true"],
                       "ports": [f"127.0.0.1:{port}:8080"],
                       "environment": {"PUBLIC_URL": origin}}
            compose_file = root / "downloads.json"
            installer.write_json(compose_file, {
                "name": "starr-download-" + root.name, "services": {"starr": service}})
            command = ["docker", "compose", "-f", compose_file]
            install_root = root / "install"
            try:
                installer.run([*command, "up", "-d", "--wait", "--wait-timeout", "90"])
                with urllib.request.urlopen(origin + "/healthz", timeout=10) as response:
                    self.assertEqual(response.read(), b"ok\n")
                with urllib.request.urlopen(origin + "/release.json", timeout=10) as response:
                    release = json.load(response)
                with urllib.request.urlopen(origin + release["bundle"], timeout=10) as response:
                    bundle = response.read()
                self.assertEqual(hashlib.sha256(bundle).hexdigest(), release["sha256"])
                with tarfile.open(fileobj=io.BytesIO(bundle), mode="r:gz") as archive:
                    self.assertEqual(set(archive.getnames()), set(host.PUBLIC_FILES))
                for route in host.SITE_FILES:
                    with urllib.request.urlopen(origin + route, timeout=10) as response:
                        self.assertEqual(response.status, 200)
                with urllib.request.urlopen(origin + "/install.sh", timeout=10) as response:
                    program = response.read().decode()
                help_result = subprocess.run(["sh", "-s", "--", "--help"], input=program,
                                             capture_output=True, text=True, timeout=30)
                self.assertEqual(help_result.returncode, 0, help_result.stderr)
                self.assertIn("--auto-update", help_result.stdout)
                if os.environ.get("STARR_DOCKER_OPTIONAL") == "1":
                    flags = ["--non-interactive", "--yes", "--allow-direct",
                             "--root", str(install_root), "--content", str(root / "content"),
                             "--uid", "1000", "--gid", "1000", "--sabnzbd", "--jellyfin",
                             "--seerr", "--no-sync-profiles"]
                    for _ in range(2):
                        result = subprocess.run(["sh", "-s", "--", *flags], input=program,
                                                capture_output=True, text=True, timeout=360)
                        self.assertEqual(result.returncode, 0, result.stderr)
                    state = json.loads((install_root / "state.json").read_text())
                    self.assertTrue(state["jellyfin"])
                    self.assertTrue(state["sabnzbd"])
                    self.assertFalse(state["sync_profiles"])
                    self.assertTrue((install_root / ".provisioned").exists())
            finally:
                if (install_root / "compose.json").exists():
                    docker_diagnostics(install_root)
                    installer.run(["docker", "compose", "-f", install_root / "compose.json",
                                   "down", "--volumes", "--remove-orphans"])
                installer.run([*command, "down", "--volumes", "--remove-orphans"])
                installer.run(["docker", "image", "rm", image])


if __name__ == "__main__":
    unittest.main()
