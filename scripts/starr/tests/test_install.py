import copy
import http.client
import importlib.util
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


SOURCE = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(SOURCE / "scripts" / "starr"))
SPEC = importlib.util.spec_from_file_location("starr_install", SOURCE / "scripts" / "starr" / "install.py")
installer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(installer)


def settings(root="/opt/starr-test", content="/srv/starr-test"):
    return {
        "version": 1, "root": str(root), "content": str(content), "uid": 1000, "gid": 1000,
        "bind": "127.0.0.1", "timezone": "Etc/UTC", "username": "media",
        "password": "test-only-$-not-a-secret", "keys": {x: "a" * 32 for x in installer.APPS},
        "bazarr_key": "b" * 32, "sab_key": "c" * 32, "slskd_key": "d" * 48,
        "sabnzbd": False, "soulseek": False, "plex": False, "jellyfin": False,
        "seerr": False, "vpn": None, "soulseek_vpn": None,
        "soulseek_username": "", "soulseek_password": "", "auto_update": False,
        "sync_profiles": True,
        "subtitle_language": "en",
    }


def vpn():
    return {"mode": "1", "country": "Netherlands", "forwarding": True, "static_port": 0,
            "environment": {"VPN_SERVICE_PROVIDER": "protonvpn", "VPN_TYPE": "wireguard",
                            "WIREGUARD_PRIVATE_KEY": "test-only"}}


def bazarr_settings():
    return {"general": {"enabled_providers": ["opensubtitlescom"], "serie_default_enabled": False,
                        "serie_default_profile": "", "movie_default_enabled": False, "movie_default_profile": ""},
            "whisperai": {"endpoint": "http://127.0.0.1:9000", "timeout": 3600, "response": 5,
                          "pass_video_name": False, "loglevel": "INFO"}}


def configured_bazarr(current):
    result = copy.deepcopy(current)
    result["general"]["enabled_providers"].append("whisperai")
    result["whisperai"].update(endpoint="http://whisperasr:9000", timeout=10800,
                               response=60, pass_video_name=True)
    return result


def archive_schema():
    return {"implementation": "Cardigann", "configContract": "CardigannSettings",
            "name": "Internet Archive (portable)", "fields": [
                {"name": "definitionFile", "value": "internetarchive-portable"},
                {"name": "baseUrl", "value": "https://archive.org/"}]}


NNTP_TEST_SERVER = """
import socketserver
class Handler(socketserver.StreamRequestHandler):
    def handle(self):
        self.wfile.write(b"200 Disposable test server ready\\r\\n")
        for line in self.rfile:
            command = line.strip()
            if command.upper().startswith(b"AUTHINFO USER "):
                response = b"381 Password required\\r\\n"
            elif command.upper().startswith(b"AUTHINFO PASS "):
                response = b"281 Authentication accepted\\r\\n" if command.endswith(b"test-only") else b"481 Invalid password\\r\\n"
            elif command.upper() == b"CAPABILITIES":
                response = b"101 Capabilities\\r\\nVERSION 2\\r\\nREADER\\r\\nAUTHINFO USER\\r\\n.\\r\\n"
            elif command.upper() == b"QUIT":
                self.wfile.write(b"205 Goodbye\\r\\n")
                return
            elif command.upper() == b"MODE READER":
                response = b"200 Reader mode\\r\\n"
            else:
                response = b"500 Unsupported command\\r\\n"
            self.wfile.write(response)
socketserver.ThreadingTCPServer.allow_reuse_address = True
socketserver.ThreadingTCPServer(("0.0.0.0", 119), Handler).serve_forever()
"""


class ComposeTests(unittest.TestCase):
    def test_optional_proxy_uses_gateway_targets_and_stays_private(self):
        c = settings()
        c.update(proxy=True, proxy_domain="starr.home.arpa", proxy_port=8088,
                 soulseek=True, soulseek_username="test", soulseek_vpn=vpn(), vpn=vpn(), sabnzbd=True)
        installer.validate_access(c)
        config = installer.caddy_config(c)
        self.assertIn("reverse_proxy gluetun:8080", config)
        self.assertIn("reverse_proxy gluetun-soulseek:5030", config)
        self.assertIn("reverse_proxy sabnzbd:8080", config)
        self.assertIn("admin off", config)
        self.assertIn("auto_https off", config)
        self.assertIn('respond "Unknown local app hostname" 404', config)
        service = installer.compose_config(c)["services"]["caddy"]
        self.assertEqual(service["ports"], ["127.0.0.1:8088:8080"])
        self.assertEqual(service["cap_drop"], ["ALL"])
        self.assertEqual(service["cap_add"], ["NET_BIND_SERVICE"])
        self.assertTrue(service["read_only"])
        self.assertTrue(all(volume["read_only"] for volume in service["volumes"]))
        self.assertNotIn("caddy", installer.compose_config(settings())["services"])

    def test_base_stack_and_private_ports(self):
        c = settings()
        services = installer.compose_config(c)["services"]
        self.assertEqual(set(services), {*installer.APPS, "bazarr", "whisperasr",
                                         "flaresolverr", "qbittorrent", "recyclarr"})
        for name in ("radarr", "sonarr", "lidarr", "qbittorrent"):
            mounts = services[name]["volumes"]
            self.assertEqual([x["target"] for x in mounts], ["/config", "/data"])
            self.assertTrue(services[name]["ports"][0].startswith("127.0.0.1:"))
        self.assertNotIn("ports", services["whisperasr"])
        self.assertNotIn("ports", services["flaresolverr"])
        self.assertEqual(services["recyclarr"]["image"], "ghcr.io/recyclarr/recyclarr:8.8.0")
        self.assertNotIn("container_name", services["radarr"])

    def test_recyclarr_version_matches_verified_snapshot_policy(self):
        manifest = installer.verify_profile_snapshot(SOURCE)
        self.assertEqual(manifest["recyclarr_version"], "8.8.0")
        self.assertEqual(installer.compose_config(settings())["services"]["recyclarr"]["image"],
                         "ghcr.io/recyclarr/recyclarr:" + manifest["recyclarr_version"])

    def test_vpn_has_no_direct_client_ports_or_bypass(self):
        c = settings()
        c["vpn"] = vpn()
        services = installer.compose_config(c)["services"]
        qb = services["qbittorrent"]
        self.assertEqual(qb["network_mode"], "service:gluetun")
        self.assertNotIn("ports", qb)
        self.assertEqual(qb["depends_on"]["gluetun"]["condition"], "service_healthy")
        self.assertEqual(services["gluetun"]["ports"], ["127.0.0.1:8080:8080"])
        self.assertNotIn("FIREWALL_OUTBOUND_SUBNETS", services["gluetun"]["environment"])
        helper = services["gluetun-port-sync"]
        self.assertTrue(helper["read_only"])
        self.assertEqual(helper["network_mode"], "service:gluetun")

    def test_soulseek_uses_separate_tunnel_and_defers_without_account(self):
        c = settings()
        c.update(soulseek=True, vpn=vpn(), soulseek_vpn=vpn())
        services = installer.compose_config(c)["services"]
        self.assertEqual(services["slskd"]["network_mode"], "service:gluetun-soulseek")
        for name in ("slskd", "soularr", "gluetun-soulseek", "gluetun-soulseek-port-sync"):
            self.assertEqual(services[name]["profiles"], ["soulseek-pending"])
            self.assertNotIn(name, installer.active_services(c))
        c["soulseek_username"] = "test-account"
        self.assertIn("slskd", installer.active_services(c))

    def test_manual_profiles_never_start_on_update(self):
        c = settings()
        c["sync_profiles"] = False
        self.assertNotIn("recyclarr", installer.active_services(c))

    def test_all_optional_services(self):
        c = settings()
        c.update({name: True for name in installer.OPTIONAL})
        services = installer.compose_config(c)["services"]
        for name in ("sabnzbd", "slskd", "soularr", "plex", "jellyfin", "seerr"):
            self.assertIn(name, services)
        self.assertEqual(services["whisperasr"]["environment"]["ASR_DEVICE"], "cpu")

    def test_compose_escapes_dollar_signs(self):
        c = settings()
        c["soulseek"] = True
        env = installer.compose_config(c)["services"]["slskd"]["environment"]
        self.assertEqual(env["SLSKD_PASSWORD"], "test-only-$$-not-a-secret")

    def test_project_identity_is_path_specific(self):
        self.assertNotEqual(installer.compose_config(settings())["name"],
                            installer.compose_config(settings(root="/opt/another"))["name"])

    def test_update_pulls_before_stopping_and_recreates_namespace_consumers(self):
        c = settings()
        c["vpn"] = vpn()
        with patch.object(installer, "compose") as compose:
            installer.update(c)
        calls = [call.args[1:] for call in compose.call_args_list]
        self.assertEqual(calls[0][0], "pull")
        self.assertEqual(calls[1][0], "stop")
        self.assertIn("gluetun-port-sync", calls[1])
        self.assertIn("--force-recreate", calls[-1])
        self.assertIn("qbittorrent", calls[-1])
        self.assertNotIn("gluetun", calls[-1])

    def test_failed_pull_does_not_stop_apps(self):
        with patch.object(installer, "compose", side_effect=installer.InstallError("registry")) as compose:
            with self.assertRaises(installer.InstallError):
                installer.update(settings())
        self.assertEqual(compose.call_count, 1)


class FileTests(unittest.TestCase):
    def test_access_validation_and_direct_links(self):
        for host in ("http://server", "server:80", "server/path", "0.0.0.0", "999.1.1.1", "bad\nhost"):
            with self.subTest(host=host), self.assertRaises((installer.InstallError, ValueError)):
                installer.validate_access({"access_host": host})
        for domain, port in (("example.com", 80), ("bad.home.arpa\n}", 80), ("starr.home.arpa", 8080),
                             ("starr.home.arpa", 0), ("starr.home.arpa", 65536)):
            with self.subTest(domain=domain, port=port), self.assertRaises((installer.InstallError, ValueError)):
                installer.validate_access({"proxy": True, "proxy_domain": domain, "proxy_port": port})
        c = settings()
        c.update(access_host="SERVER.local.", proxy=True, proxy_domain="starr.home.arpa", proxy_port=8088,
                 plex=True, soulseek=True)
        installer.validate_access(c)
        links = {link["name"]: link for link in installer.access_links(c)}
        self.assertEqual(links["plex"]["direct"], "http://server.local:32400/web/")
        self.assertEqual(links["radarr"]["proxy"], "http://radarr.starr.home.arpa:8088/")
        self.assertNotIn("slskd", links)
        c.update(proxy=False, access_host="", bind="0.0.0.0")
        self.assertTrue(all(not link["direct"] and not link["proxy"] for link in installer.access_links(c)))

    def test_access_files_include_offline_icons_and_support_installed_reruns(self):
        with tempfile.TemporaryDirectory() as directory:
            c = settings(root=directory)
            c.update(access_host="192.168.1.100", proxy=True, proxy_domain="starr.home.arpa", proxy_port=80)
            installer.write_access_files(c, SOURCE)
            access = Path(directory) / "access"
            self.assertIn("192.168.1.100 starr.home.arpa radarr.starr.home.arpa",
                          (access / "hosts.txt").read_text())
            self.assertIn('src="icons/radarr.svg"', (access / "index.html").read_text())
            self.assertNotIn("cdn.", (access / "index.html").read_text())
            self.assertTrue(all((access / "icons" / name).is_file() for name in installer.ICON_FILES))
            installer.write_access_files(c, Path(directory) / "no-checkout")
            self.assertIn("radarr.starr.home.arpa", (Path(directory) / "config" / "caddy" / "Caddyfile").read_text())

    def test_access_host_allowlist_merges_without_disabling_qbit_protections(self):
        c = settings()
        c["access_host"] = "server.local"
        api = unittest.mock.Mock()
        api.request.side_effect = [None, {"listen_port": 6881, "web_ui_domain_list": "already.local"},
                                   "", {"web_ui_domain_list": "already.local;server.local"}]
        with patch.object(installer, "API", return_value=api):
            installer.configure_access_hosts(c)
        changed = json.loads(api.request.call_args_list[2].args[2]["json"])
        self.assertEqual(changed, {"web_ui_domain_list": "already.local;server.local"})

    def test_torrent_preference_supports_both_delay_profile_schemas(self):
        old = {"preferredProtocol": "usenet"}
        self.assertEqual(installer.torrent_first(old, False)["preferredProtocol"], "torrent")
        self.assertFalse(old["enableUsenet"])
        new = {"items": [{"name": "Usenet", "protocol": "UsenetDownloadProtocol", "allowed": True, "delay": 0},
                         {"name": "Torrent", "protocol": "TorrentDownloadProtocol", "allowed": False, "delay": 60}]}
        installer.torrent_first(new, True)
        self.assertEqual(new["items"][0]["protocol"], "TorrentDownloadProtocol")
        self.assertEqual(new["items"][0]["delay"], 0)
        self.assertEqual(new["items"][1]["delay"], 60)
        self.assertTrue(all(item["allowed"] for item in new["items"]))
        with self.assertRaises(installer.InstallError):
            installer.torrent_first({"items": []}, False)

    def test_usenet_file_defaults_and_invalid_credentials(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "usenet.json"
            valid = {"host": "news.example.test", "username": "test", "password": "test-only"}
            path.write_text(json.dumps(valid))
            server = installer.usenet_server_file(path)
            self.assertEqual((server["port"], server["connections"], server["ssl"]), (563, 20, True))
            for changed in ({"port": 0}, {"connections": True}, {"ssl": "false"},
                            {"password": "two\nlines"}, {"host": ""}, {"unexpected": 1}):
                with self.subTest(changed=changed), self.assertRaises(installer.InstallError):
                    path.write_text(json.dumps({**valid, **changed}))
                    installer.usenet_server_file(path)

    def test_sabnzbd_never_enables_clients_when_credentials_are_missing_or_invalid(self):
        with tempfile.TemporaryDirectory() as directory:
            c = settings(root=directory)
            c["sabnzbd"] = True
            with patch.object(installer, "API") as api:
                installer.configure_sabnzbd(c)
                api.assert_not_called()
            c["usenet_server"] = {"host": "news.example.test", "username": "test", "password": "test",
                                  "port": 563, "ssl": True, "connections": 1}
            api = unittest.mock.Mock()
            api.request.side_effect = ["5.1", {"value": {"result": False}}]
            with patch.object(installer, "API", return_value=api), self.assertRaisesRegex(
                    installer.InstallError, "connection test failed"):
                installer.configure_sabnzbd(c)
            self.assertFalse((Path(directory) / ".sab-provisioned").exists())
            self.assertEqual(len(api.request.call_args_list), 2)

    def test_plex_libraries_use_token_headers_and_preserve_rerun_edits(self):
        with tempfile.TemporaryDirectory() as directory:
            c = settings(root=directory)
            c["plex"] = True
            api = unittest.mock.Mock()
            libraries = [{"title": name, "Location": [{"path": "/data/media/" + category}]}
                         for name, category in zip(("Movies", "TV", "Music"), installer.ROOTS)]
            api.request.side_effect = [{}, {"MediaContainer": {}}, "", "", "",
                                       {"MediaContainer": {"Directory": libraries}}]
            with patch.object(installer, "plex_token", return_value="test-token"), \
                    patch.object(installer, "API", return_value=api) as constructor:
                installer.configure_plex(c)
                self.assertEqual(constructor.call_args.kwargs["headers"]["X-Plex-Token"], "test-token")
                self.assertTrue(all("test-token" not in call.args[0] for call in api.request.call_args_list))
                api.reset_mock()
                installer.configure_plex(c)
                api.request.assert_not_called()
            self.assertTrue((Path(directory) / ".plex-provisioned").exists())

    def test_sabnzbd_tests_masked_saved_credentials_before_enabling_clients(self):
        with tempfile.TemporaryDirectory() as directory:
            c = settings(root=directory)
            c["sabnzbd"] = True
            c["usenet_server"] = {"host": "news.example.test", "username": "test", "password": "test",
                                  "port": 563, "ssl": True, "connections": 1}
            saved = {**c["usenet_server"], "name": "Portable Usenet", "enable": 1,
                     "password": "********", "ssl_verify": 3}
            api = unittest.mock.Mock()
            api.request.side_effect = ["5.1", {"value": {"result": True}},
                                       {"config": {"servers": [saved]}}, {"config": {"servers": [saved]}},
                                       {"value": {"result": False}}]
            with patch.object(installer, "API", return_value=api) as constructor, \
                    self.assertRaisesRegex(installer.InstallError, "saved Usenet server failed"):
                installer.configure_sabnzbd(c)
            self.assertEqual(constructor.call_count, 1)
            self.assertEqual(api.request.call_args.args[2]["server"], "Portable Usenet")
            self.assertEqual(api.request.call_args.args[2]["password"], "********")
            self.assertFalse((Path(directory) / ".sab-provisioned").exists())

    def test_unclaimed_plex_is_explicitly_deferred_without_marking_complete(self):
        with tempfile.TemporaryDirectory() as directory:
            c = settings(root=directory)
            c["plex"] = True
            with patch.object(installer, "API") as api:
                installer.configure_plex(c)
                api.assert_not_called()
            self.assertFalse((Path(directory) / ".plex-provisioned").exists())

    def test_plex_library_refresh_connections_preserve_rerun_edits(self):
        with tempfile.TemporaryDirectory() as directory:
            c = settings(root=directory)
            c["plex"] = True
            with patch.object(installer, "plex_token", return_value="test-token"), \
                    patch.object(installer, "API"), patch.object(installer, "ensure_model") as ensure:
                installer.configure_media_connections(c)
                self.assertEqual(ensure.call_count, 3)
                for app, call in zip(("radarr", "sonarr", "lidarr"), ensure.call_args_list):
                    self.assertEqual(call.args[1:4], ("/notification", "PlexServer", "Portable Plex"))
                    self.assertEqual(call.args[4]["host"], "plex")
                    self.assertEqual(call.args[4]["authToken"], "test-token")
                    self.assertTrue(call.args[4]["updateLibrary"])
                    self.assertTrue(call.kwargs["onReleaseImport" if app == "lidarr" else "onDownload"])
                    self.assertTrue(call.kwargs["onUpgrade"])
                    self.assertTrue(call.kwargs["onRename"])
                ensure.reset_mock()
                installer.configure_media_connections(c)
                ensure.assert_not_called()
            self.assertTrue((Path(directory) / ".plex-connections-provisioned").exists())

    def test_jellyfin_refresh_connections_reuse_valid_keys_without_active_sessions(self):
        for existing in (True, False):
            with self.subTest(existing=existing), tempfile.TemporaryDirectory() as directory:
                c = settings(root=directory)
                c["jellyfin"] = True
                (Path(directory) / ".jellyfin-managed").write_text("managed")
                key = {"AppName": "Portable Starr", "AccessToken": "test-token", "IsActive": False}
                api = unittest.mock.Mock()
                api.request.side_effect = ([{"Items": [key]}] if existing else
                                           [{"Items": []}, None, {"Items": [key]}])
                with patch.object(installer, "jellyfin_login", return_value=api), \
                        patch.object(installer, "API"), patch.object(installer, "ensure_model") as ensure:
                    installer.configure_media_connections(c)
                self.assertEqual(ensure.call_count, 3)
                self.assertEqual(ensure.call_args.args[4]["apiKey"], "test-token")
                self.assertTrue((Path(directory) / ".jellyfin-connections-provisioned").exists())
                if existing:
                    api.request.assert_called_once_with("/Auth/Keys")
                else:
                    self.assertEqual(api.request.call_args_list[1].args,
                                     ("/Auth/Keys?app=Portable%20Starr", "POST"))

    def test_completed_jellyfin_setup_is_not_reset(self):
        with tempfile.TemporaryDirectory() as directory:
            c = settings(root=directory)
            c["jellyfin"] = True
            api = unittest.mock.Mock()
            api.request.return_value = {"StartupWizardCompleted": True}
            with patch.object(installer, "API", return_value=api):
                installer.configure_jellyfin(c)
            self.assertEqual(api.request.call_args_list, [unittest.mock.call("/System/Info/Public")])
            self.assertTrue((Path(directory) / ".jellyfin-provisioned").exists())

    def test_archive_definition_and_indexer_are_created_once_and_edits_are_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            c = settings(root=directory)
            source = Path(directory) / "config" / "prowlarr" / "Definitions" / "internetarchive.yml"
            source.parent.mkdir(parents=True)
            original = "id: internetarchive\nname: Internet Archive\nsearch:\n  paths:\n    - path: advancedsearch.php\n"
            source.write_text(original)
            saved = dict(archive_schema(), id=11, enable=True, tags=[])
            api = unittest.mock.Mock()
            api.request.side_effect = [{}, [], [archive_schema()], [{"id": 1, "name": "Standard"}],
                                       {"id": 11}, saved]
            with patch.object(installer.os, "chown", create=True):
                installer.configure_internet_archive(c, api)
            target = source.parent / "Custom" / "internetarchive-portable.yml"
            text = target.read_text()
            self.assertIn('    User-Agent: ["Mozilla/5.0 ', text)
            self.assertIn("id: internetarchive-portable\n", text)
            self.assertIn("name: Internet Archive (portable)\n", text)
            self.assertIn("  paths:\n    - path: advancedsearch.php\n", text)
            self.assertEqual(source.read_text(), original)
            create = api.request.call_args_list[4]
            self.assertEqual(create.args[:2], ("/indexer", "POST"))
            self.assertEqual(create.args[2]["tags"], [])
            self.assertTrue(create.args[2]["enable"])
            self.assertEqual(create.args[2]["appProfileId"], 1)
            self.assertTrue((Path(directory) / ".archive-provisioned").exists())
            target.write_text("user-edited definition\n")
            api.reset_mock()
            installer.configure_internet_archive(c, api)
            api.request.assert_not_called()
            self.assertEqual(target.read_text(), "user-edited definition\n")

    def test_archive_existing_renamed_disabled_entries_are_not_duplicated_or_changed(self):
        for definition in ("internetarchive", "internetarchive-homeserver", "internetarchive-portable"):
            with self.subTest(definition=definition), tempfile.TemporaryDirectory() as directory:
                c = settings(root=directory)
                existing = {"name": "User name", "enable": False,
                            "fields": [{"name": "definitionFile", "value": definition}]}
                api = unittest.mock.Mock()
                api.request.side_effect = [{}, [existing]]
                installer.configure_internet_archive(c, api)
                self.assertTrue((Path(directory) / ".archive-provisioned").exists())
                self.assertEqual(len(api.request.call_args_list), 2)

    def test_archive_rejects_changed_source_and_requires_verified_api_creation(self):
        for failure in ("headers", "identity", "connection", "readback"):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as directory:
                c = settings(root=directory)
                source = Path(directory) / "config" / "prowlarr" / "Definitions" / "internetarchive.yml"
                source.parent.mkdir(parents=True)
                source.write_text("id: internetarchive\nname: Internet Archive\nsearch:\n" + (
                    "  headers:\n    Other: value\n" if failure == "headers" else "  paths: []\n"))
                if failure == "identity":
                    source.write_text(source.read_text().replace("id: internetarchive", "id: changed"))
                api = unittest.mock.Mock()
                api.request.side_effect = [
                    {}, [], [archive_schema()], [{"id": 1, "name": "Standard"}],
                    installer.InstallError("Connection test failed") if failure == "connection" else {"id": 11},
                    dict(archive_schema(), enable=False, tags=[])]
                with patch.object(installer.os, "chown", create=True), self.assertRaises(installer.InstallError):
                    installer.configure_internet_archive(c, api)
                self.assertFalse((Path(directory) / ".archive-provisioned").exists())

    def test_bazarr_merges_languages_and_profiles_without_overwriting_existing_defaults(self):
        for keep_defaults in (False, True):
            with self.subTest(keep_defaults=keep_defaults), tempfile.TemporaryDirectory() as directory:
                c = settings(root=directory)
                current = bazarr_settings()
                old_profile = {"profileId": 7, "name": "User French", "items": [{"language": "fr"}],
                               "cutoff": None, "mustContain": ["keep"], "mustNotContain": [],
                               "originalFormat": 0}
                if keep_defaults:
                    current["general"].update(serie_default_enabled=True, serie_default_profile=7,
                                               movie_default_enabled=True, movie_default_profile=7)
                expected = configured_bazarr(current)
                if not keep_defaults:
                    expected["general"].update(serie_default_enabled=True, serie_default_profile=8,
                                                movie_default_enabled=True, movie_default_profile=8)
                profile = {"profileId": 8, "name": "Portable English", "cutoff": None,
                           "mustContain": [], "mustNotContain": [], "originalFormat": 0, "tag": None,
                           "items": [{"id": 1, "language": "en", "audio_exclude": "False",
                                      "audio_only_include": "False", "hi": "False", "forced": "False"}]}
                languages = [{"code2": "fr", "name": "French", "enabled": True},
                             {"code2": "en", "name": "English", "enabled": False}]
                enabled = copy.deepcopy(languages)
                enabled[1]["enabled"] = True
                api = unittest.mock.Mock()
                api.request.side_effect = [current, languages, [old_profile], None, expected,
                                           enabled, [old_profile, profile]]
                with patch.object(installer, "API", return_value=api):
                    installer.configure_bazarr(c)
                    data = api.request.call_args_list[3].args[2]
                    self.assertEqual(data["settings-general-enabled_providers"], ["opensubtitlescom", "whisperai"])
                    self.assertEqual(data["languages-enabled"], ["fr", "en"])
                    self.assertEqual(json.loads(data["languages-profiles"]), [old_profile, profile])
                    self.assertEqual(data.get("settings-general-serie_default_profile"), None if keep_defaults else 8)
                    self.assertTrue((Path(directory) / ".bazarr-provisioned").exists())
                    api.reset_mock()
                    installer.configure_bazarr(c)
                    api.request.assert_not_called()

    def test_bazarr_manual_language_setup_and_failed_readback_are_retryable(self):
        with tempfile.TemporaryDirectory() as directory:
            c = settings(root=directory)
            c["subtitle_language"] = "none"
            current = bazarr_settings()
            api = unittest.mock.Mock()
            api.request.side_effect = [current, None, current]
            with patch.object(installer, "API", return_value=api):
                with self.assertRaisesRegex(installer.InstallError, "did not accept"):
                    installer.configure_bazarr(c)
            self.assertFalse((Path(directory) / ".bazarr-provisioned").exists())
            api.request.reset_mock()
            api.request.side_effect = [current, None, configured_bazarr(current)]
            with patch.object(installer, "API", return_value=api):
                installer.configure_bazarr(c)
            data = api.request.call_args_list[1].args[2]
            self.assertNotIn("languages-profiles", data)
            self.assertNotIn("languages-enabled", data)
            self.assertNotIn("settings-general-serie_default_profile", data)

    def test_subtitle_language_can_be_corrected_only_before_bazarr_setup_completes(self):
        with tempfile.TemporaryDirectory() as directory:
            c = settings(root=directory)
            parser = installer.argument_parser()
            installer.saved_options(c, parser.parse_args(["--subtitle-language", "es"]))
            self.assertEqual(c["subtitle_language"], "es")
            (Path(directory) / ".bazarr-provisioned").write_text("completed")
            with self.assertRaises(installer.InstallError):
                installer.saved_options(c, parser.parse_args(["--subtitle-language", "de"]))

    def test_form_encoding_preserves_multiple_values(self):
        api = installer.API("http://127.0.0.1")
        response = unittest.mock.MagicMock()
        response.__enter__.return_value.read.return_value = b""
        with patch.object(api.opener, "open", return_value=response) as send:
            api.request("/settings", "POST", {"providers": ["first", "second"]}, form=True)
        self.assertEqual(send.call_args.args[0].data, b"providers=first&providers=second")

    def test_remote_docker_context_is_rejected_before_changes(self):
        with patch.dict(os.environ, {"DOCKER_HOST": "ssh://production"}), \
                patch.object(installer, "run") as command:
            with self.assertRaises(installer.InstallError):
                installer.docker_preflight()
            command.assert_not_called()

    def test_rootless_docker_is_rejected(self):
        with patch.dict(os.environ, {"DOCKER_HOST": "unix:///run/user/1000/docker.sock"}), \
                patch.object(installer, "run") as command:
            command.return_value.stdout = 'linux ["name=rootless"]'
            with self.assertRaises(installer.InstallError):
                installer.docker_preflight()

    def test_reject_nested_paths_and_unsafe_roots(self):
        for first, second in (("/srv/a", "/srv/a"), ("/srv/a", "/srv/a/b"), ("/srv/a/b", "/srv/a")):
            with self.assertRaises(installer.InstallError):
                installer.validate_paths(Path(first), Path(second))
        for path in ("/", "/srv", "/opt", "relative", "/srv/percent%path"):
            with self.assertRaises(installer.InstallError):
                installer.absolute_path(path)

    def test_seed_does_not_replace_existing_configuration(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config"
            path.write_text("keep user edits")
            with patch.object(os, "chown", create=True) as chown:
                installer.seed(path, "replacement", settings())
            self.assertEqual(path.read_text(), "keep user edits")
            chown.assert_not_called()

    def test_wireguard_export_parsing_and_hostname_rejection(self):
        key = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "wg.conf"
            text = f"[Interface]\nPrivateKey = {key}\nAddress = 10.2.0.2/32\n" \
                   f"[Peer]\nPublicKey = {key}\nEndpoint = 198.51.100.1:51820\n"
            path.write_text(text)
            env = installer.wireguard_environment(path)
            self.assertEqual(env["WIREGUARD_ENDPOINT_IP"], "198.51.100.1")
            path.write_text(text.replace("198.51.100.1", "vpn.example.com"))
            with self.assertRaises(installer.InstallError):
                installer.wireguard_environment(path)
            self.assertEqual(installer.wireguard_environment(path, proton=True)["VPN_SERVICE_PROVIDER"],
                             "protonvpn")

    def test_openvpn_rejects_scripts_and_external_certificates(self):
        with tempfile.TemporaryDirectory() as directory:
            c = settings(root=directory)
            profile = Path(directory) / "export.ovpn"
            c["vpn"] = {"mode": "3", "source": str(profile), "username": "user", "password": "secret"}
            for text in ("up /tmp/evil.sh", "script-security 2", "ca other-file.crt"):
                profile.write_text(text)
                with self.assertRaises(installer.InstallError):
                    installer.prepare_vpn(copy.deepcopy(c))

    def test_api_schema_change_is_not_silently_ignored(self):
        with self.assertRaises(installer.InstallError):
            installer.set_fields({"fields": [{"name": "host"}]}, {"hostname": "new"})

    def test_qbit_login_checks_protected_endpoint_not_legacy_response(self):
        for response in ("Ok.", ""):
            api = unittest.mock.Mock()
            api.request.side_effect = [response, {"listen_port": 6881}]
            self.assertEqual(installer.qbit_login(api, "media", "secret"), {"listen_port": 6881})
            self.assertEqual(api.request.call_args.args, ("/api/v2/app/preferences",))

    def test_qbit_login_rejects_success_shaped_but_invalid_preferences(self):
        api = unittest.mock.Mock()
        api.request.side_effect = ["Ok.", None]
        with self.assertRaises(installer.InstallError):
            installer.qbit_login(api, "media", "secret")

    def test_existing_api_records_are_preserved(self):
        class Existing:
            def request(self, endpoint):
                return [{"name": "Managed", "implementation": "QBittorrent", "priority": 9}]
        self.assertEqual(installer.ensure_model(Existing(), "/downloadclient", "QBittorrent",
                                                "Managed", {}, priority=1)["priority"], 9)

    def test_cron_is_repeat_safe_and_preserves_other_jobs(self):
        c = settings(root="/opt/starr with spaces")
        c["auto_update"] = True
        installed = []

        def write_cron(args, **kwargs):
            installed[:] = kwargs["input"].splitlines()

        with patch.object(installer.shutil, "which", return_value="/usr/bin/crontab"), \
                patch.object(installer.subprocess, "run") as read, \
                patch.object(installer, "run", side_effect=write_cron):
            read.return_value = subprocess.CompletedProcess([], 0, "0 1 * * * keep-me\n", "")
            installer.install_cron(c)
            read.return_value.stdout = "\n".join(installed)
            installer.install_cron(c)
        self.assertEqual(len(installed), 2)
        self.assertIn("keep-me", installed[0])
        self.assertIn("PATH=", installed[1])
        self.assertIn(shlex.quote(str(Path(c["root"]) / "bin" / "starr.py")), installed[1])

    def test_disabling_cron_removes_only_managed_job(self):
        c = settings()
        c["auto_update"] = True
        jobs = []
        with patch.object(installer.shutil, "which", return_value="/usr/bin/crontab"), \
                patch.object(installer.subprocess, "run") as read, \
                patch.object(installer, "run") as write:
            read.return_value = subprocess.CompletedProcess([], 0, "0 1 * * * keep-me\n", "")
            installer.install_cron(c)
            jobs = write.call_args.kwargs["input"]
            read.return_value.stdout = jobs
            c["auto_update"] = False
            installer.install_cron(c)
            self.assertEqual(write.call_args.kwargs["input"], "0 1 * * * keep-me\n")


@unittest.skipUnless(os.environ.get("STARR_DOCKER_TESTS") == "1",
                     "Opt-in Linux Docker integration; pulls images and uses local Starr UI ports.")
class DockerIntegration(unittest.TestCase):
    def test_optional_and_vpn_compose_variants(self):
        with tempfile.TemporaryDirectory(prefix="starr-compose-") as directory:
            c = settings(Path(directory) / "install", Path(directory) / "content")
            c.update({name: True for name in installer.OPTIONAL})
            c.update(proxy=True, proxy_domain="starr.home.arpa", proxy_port=18088)
            for use_vpn in (False, True):
                c["vpn"] = vpn() if use_vpn else None
                c["soulseek_vpn"] = vpn() if use_vpn else None
                result = installer.run(["docker", "compose", "-f", "-", "config", "--quiet"],
                                       input=json.dumps(installer.compose_config(c)), text=True,
                                       capture_output=True)
                self.assertEqual(result.returncode, 0)

    @unittest.skipUnless(os.environ.get("STARR_DOCKER_OPTIONAL") == "1",
                         "Additional opt-in: pulls Whisper, Plex, Jellyfin and Seerr images.")
    def test_complete_cli_with_optional_media_apps_and_rerun(self):
        with tempfile.TemporaryDirectory(prefix="starr-cli-") as directory:
            root = Path(directory) / "install"
            root.mkdir(mode=0o700)
            c = settings(root, Path(directory) / "content")
            c.update({name: True for name in installer.OPTIONAL})
            c.update(proxy=True, proxy_domain="starr.home.arpa", proxy_port=18088)
            c["sync_profiles"] = False
            c["seerr_media_server"] = "jellyfin"
            installer.write_json(root / "state.json", c)
            try:
                with patch.object(installer, "install_cron"):
                    installer.main(["install", "--root", str(root)])
                    installer.main(["install", "--root", str(root)])
                self.assertTrue((root / ".provisioned").exists())
                self.assertTrue((root / ".jellyfin-provisioned").exists())
                self.assertTrue((root / ".seerr-provisioned").exists())
                self.assertFalse((root / ".plex-provisioned").exists())
                self.assertTrue((root / ".jellyfin-connections-provisioned").exists())
                self.assertFalse((root / ".plex-connections-provisioned").exists())
                for app in ("radarr", "sonarr", "lidarr"):
                    port, version, _, _ = installer.APPS[app]
                    api = installer.API(f"http://127.0.0.1:{port}/api/{version}", c["keys"][app])
                    connections = api.request("/notification")
                    self.assertEqual(len(connections), 1)
                    connection = connections[0]
                    self.assertEqual(connection["implementation"], "MediaBrowser")
                    self.assertTrue(connection["onReleaseImport" if app == "lidarr" else "onDownload"])
                    self.assertTrue(connection["onUpgrade"])
                    self.assertTrue(connection["onRename"])
                    fields = {field["name"]: field.get("value") for field in connection["fields"]}
                    self.assertEqual(fields["host"], "jellyfin")
                    self.assertTrue(fields["updateLibrary"])
                    api.request("/notification/test", "POST", connection)
                jellyfin = installer.jellyfin_login(c)
                for name, path in (("jellyfin", "/System/Info/Public"), ("plex", "/identity"), ("seerr", "/api/v1/status")):
                    api = installer.API("http://127.0.0.1:18088", headers={"Host": name + ".starr.home.arpa:18088"})
                    self.assertTrue(api.request(path, raw=True))
                connection = http.client.HTTPConnection("127.0.0.1", 18088, timeout=15)
                try:
                    connection.request("GET", "/socket", headers={
                        **jellyfin.headers, "Host": "jellyfin.starr.home.arpa:18088",
                        "Connection": "Upgrade", "Upgrade": "websocket", "Sec-WebSocket-Version": "13",
                        "Sec-WebSocket-Key": "dGhlIHNhbXBsZSBub25jZQ=="})
                    self.assertEqual(connection.getresponse().status, 101)
                finally:
                    connection.close()
                folders = jellyfin.request("/Library/VirtualFolders")
                self.assertEqual(len(folders), 3)
                self.assertEqual({path for folder in folders for path in folder["Locations"]},
                                 {f"/data/media/{category}" for category in installer.ROOTS})
                saved = json.loads((root / "config" / "seerr" / "settings.json").read_text())
                seerr = installer.API("http://127.0.0.1:5055/api/v1", saved["main"]["apiKey"])
                self.assertTrue(seerr.request("/settings/public")["initialized"])
                self.assertEqual(seerr.request("/settings/public")["mediaServerType"], 2)
                for app, category in (("radarr", "movies"), ("sonarr", "tv")):
                    connections = seerr.request("/settings/" + app)
                    self.assertEqual(len(connections), 1)
                    self.assertEqual(connections[0]["activeDirectory"], "/data/media/" + category)
                    self.assertEqual(connections[0]["activeProfileName"], "HD-1080p")
                    self.assertTrue(connections[0]["isDefault"])
                self.assertEqual(sum(item["enabled"] for item in seerr.request("/settings/jellyfin/library")), 2)
                for port, path in ((8096, "/System/Info/Public"), (5055, "/api/v1/status"),
                                   (32400, "/identity"), (6767, "/")):
                    api = installer.API(f"http://127.0.0.1:{port}")
                    deadline = installer.time.monotonic() + 180
                    while True:
                        try:
                            self.assertTrue(api.request(path, raw=True))
                            break
                        except (OSError, installer.InstallError):
                            if installer.time.monotonic() > deadline:
                                raise
                            installer.time.sleep(3)
                result = installer.compose(c, "exec", "-T", "bazarr", "curl", "-fsS",
                                           "--retry", "60", "--retry-connrefused", "--retry-delay", "3",
                                           "--retry-max-time", "180",
                                           "http://whisperasr:9000/openapi.json",
                                           capture_output=True, text=True)
                self.assertIn("paths", json.loads(result.stdout))
                with patch.object(installer, "install_cron"):
                    installer.main(["install", "--root", str(root), "--no-proxy"])
                self.assertNotIn("caddy", json.loads((root / "compose.json").read_text())["services"])
                saved_state = json.loads((root / "state.json").read_text())
                self.assertFalse(saved_state["proxy"])
                self.assertEqual(saved_state["proxy_trusted_ips"], [])
                project = installer.compose_config(c)["name"]
                remaining = installer.run(["docker", "ps", "-aq", "--filter", "label=com.docker.compose.project=" + project,
                                           "--filter", "label=com.docker.compose.service=caddy"], capture_output=True, text=True)
                self.assertFalse(remaining.stdout.strip())
            finally:
                if (root / "compose.json").exists():
                    installer.compose(c, "down", "--volumes", "--remove-orphans")

    def test_fresh_install_api_provisioning_and_repeat_run(self):
        if not hasattr(os, "geteuid") or os.geteuid() != 0:
            self.fail("Integration test requires Linux root.")
        with tempfile.TemporaryDirectory(prefix="starr-integration-") as directory:
            root = Path(directory) / "install"
            root.mkdir(mode=0o700)
            c = settings(root, Path(directory) / "content")
            c["sabnzbd"] = True
            c.update(proxy=True, proxy_domain="starr.home.arpa", proxy_port=18088, access_host="127.0.0.1")
            installer.initialize(c, SOURCE)
            config = json.loads((root / "compose.json").read_text())
            config["services"]["nntp-test"] = {
                "image": "python:3.12-alpine", "command": ["python3", "-u", "-c", NNTP_TEST_SERVER]}
            installer.write_json(root / "compose.json", config)
            services = ["qbittorrent", *installer.APPS, "sabnzbd", "bazarr", "flaresolverr", "nntp-test", "caddy"]
            try:
                installer.compose(c, "config", "--quiet")
                installer.compose(c, "pull", *services, "recyclarr")
                installer.compose(c, "up", "-d", *services)
                installer.provision(c)
                installer.provision(c)
                qbit = installer.API("http://127.0.0.1:8080")
                prefs = installer.qbit_login(qbit, c["username"], c["password"])
                self.assertEqual(prefs["save_path"], "/data/torrents")
                self.assertEqual(prefs["temp_path"], "/data/torrents/incomplete")
                self.assertTrue(prefs["auto_tmm_enabled"])
                self.assertFalse(prefs["upnp"])
                self.assertEqual(set(qbit.request("/api/v2/torrents/categories")), set(installer.ROOTS))
                for app in ("radarr", "sonarr", "lidarr"):
                    port, version, _, _ = installer.APPS[app]
                    api = installer.API(f"http://127.0.0.1:{port}/api/{version}", c["keys"][app])
                    self.assertEqual(len(api.request("/rootfolder")), 1)
                    clients = api.request("/downloadclient")
                    self.assertEqual(len(clients), 2)
                    self.assertEqual({x["name"]: x["enable"] for x in clients},
                                     {"Portable qBittorrent": True, "Portable SABnzbd": False})
                    self.assertEqual({x["name"]: x["priority"] for x in clients},
                                     {"Portable qBittorrent": 1, "Portable SABnzbd": 50})
                    self.assertTrue(api.request("/config/mediamanagement")["copyUsingHardlinks"])
                    delay = api.request("/delayprofile")[0]
                    if "preferredProtocol" in delay:
                        self.assertEqual(delay["preferredProtocol"], "torrent")
                        self.assertEqual(delay["usenetDelay"], 60)
                    else:
                        self.assertEqual(delay["items"][0]["protocol"], "TorrentDownloadProtocol")
                        self.assertEqual(delay["items"][0]["delay"], 0)
                        self.assertEqual(next(item["delay"] for item in delay["items"]
                                              if item["protocol"] == "UsenetDownloadProtocol"), 60)
                    if app == "lidarr":
                        self.assertEqual(api.request("/config/metadataprovider")["writeAudioTags"], "no")
                        profile = next(p for p in api.request("/qualityprofile") if p["name"] == "FLAC Only")
                        self.assertEqual({x["quality"]["name"] for x in profile["items"] if x["allowed"]},
                                         {"FLAC", "FLAC 24bit"})
                prowlarr = installer.API("http://127.0.0.1:9696/api/v1", c["keys"]["prowlarr"])
                connections = prowlarr.request("/applications")
                self.assertEqual(len(connections), 3)
                self.assertTrue(all(x["syncLevel"] == "fullSync" for x in connections))
                proxies = prowlarr.request("/indexerproxy")
                self.assertEqual(len(proxies), 1)
                tags = prowlarr.request("/tag")
                self.assertEqual(len(tags), 1)
                self.assertEqual(tags[0]["label"], "flaresolverr")
                self.assertEqual(proxies[0]["tags"], [tags[0]["id"]])
                self.assertEqual(next(item["value"] for item in proxies[0]["fields"] if item["name"] == "host"),
                                 "http://flaresolverr:8191")
                indexers = prowlarr.request("/indexer")
                self.assertEqual(len(indexers), 1)
                archive = indexers[0]
                self.assertEqual(archive["name"], "Internet Archive (portable)")
                self.assertTrue(archive["enable"])
                self.assertEqual(archive["tags"], [])
                self.assertEqual(next(item["value"] for item in archive["fields"]
                                      if item["name"] == "definitionFile"), "internetarchive-portable")
                definition = root / "config" / "prowlarr" / "Definitions" / "Custom" / "internetarchive-portable.yml"
                self.assertIn("User-Agent:", definition.read_text())
                archive.update(name="User Archive", enable=False, priority=31)
                prowlarr.request(f"/indexer/{archive['id']}", "PUT", archive)
                installer.configure_internet_archive(c, prowlarr)
                kept = prowlarr.request(f"/indexer/{archive['id']}")
                self.assertEqual(kept["name"], "User Archive")
                self.assertFalse(kept["enable"])
                self.assertEqual(kept["priority"], 31)
                bazarr = installer.API("http://127.0.0.1:6767", c["bazarr_key"])
                bazarr_settings = installer.ready(bazarr, "/api/system/settings")
                self.assertTrue(bazarr_settings["general"]["use_sonarr"])
                self.assertEqual(bazarr_settings["sonarr"]["ip"], "sonarr")
                self.assertEqual(bazarr_settings["whisperai"]["endpoint"], "http://whisperasr:9000")
                self.assertEqual(bazarr_settings["general"]["enabled_providers"], ["whisperai"])
                self.assertEqual(bazarr_settings["whisperai"]["timeout"], 10800)
                self.assertEqual(bazarr_settings["whisperai"]["response"], 60)
                self.assertTrue(bazarr_settings["whisperai"]["pass_video_name"])
                subtitle_profiles = bazarr.request("/api/system/languages/profiles")
                self.assertEqual(len(subtitle_profiles), 1)
                self.assertEqual(subtitle_profiles[0]["name"], "Portable English")
                self.assertEqual(subtitle_profiles[0]["items"][0]["language"], "en")
                for kind in ("serie", "movie"):
                    self.assertTrue(bazarr_settings["general"][kind + "_default_enabled"])
                    self.assertEqual(bazarr_settings["general"][kind + "_default_profile"],
                                     subtitle_profiles[0]["profileId"])
                self.assertTrue(next(item for item in bazarr.request("/api/system/languages")
                                     if item["code2"] == "en")["enabled"])
                sab = installer.API("http://127.0.0.1:8085")
                config = installer.ready(sab, "/api?mode=get_config&output=json&apikey=" + c["sab_key"])
                self.assertEqual(config["config"]["misc"]["complete_dir"], "/data/usenet/complete")
                c["usenet_server"] = {"host": "nntp-test", "port": 119, "ssl": False,
                                      "username": "test", "password": "test-only", "connections": 1}
                checked = sab.request("/api", "POST", {
                    **c["usenet_server"], "ssl": 0, "mode": "config", "name": "test_server",
                    "output": "json", "apikey": c["sab_key"]}, form=True)
                self.assertIs(checked.get("value", {}).get("result"), True, checked)
                installer.configure_sabnzbd(c)
                installer.configure_access_hosts(c)
                for app in installer.APPS:
                    port, version, _, _ = installer.APPS[app]
                    api = installer.API("http://127.0.0.1:18088", c["keys"][app],
                                        headers={"Host": app + ".starr.home.arpa:18088"})
                    self.assertEqual(api.request(f"/api/{version}/system/status")["appName"].lower(), app)
                proxied = installer.API("http://127.0.0.1:18088", headers={
                    "Host": "qbittorrent.starr.home.arpa:18088", "Origin": "http://qbittorrent.starr.home.arpa:18088",
                    "Referer": "http://qbittorrent.starr.home.arpa:18088/"})
                self.assertIn("listen_port", installer.qbit_login(proxied, c["username"], c["password"]))
                preferences = proxied.request("/api/v2/app/preferences")
                self.assertTrue(preferences["web_ui_csrf_protection_enabled"])
                self.assertTrue(preferences["web_ui_host_header_validation_enabled"])
                self.assertTrue(preferences["web_ui_reverse_proxy_enabled"])
                proxied.headers["Origin"] = "http://untrusted.example"
                with self.assertRaisesRegex(installer.InstallError, "HTTP 401"):
                    installer.qbit_login(proxied, c["username"], c["password"])
                proxied = installer.API("http://127.0.0.1:18088", headers={"Host": "sabnzbd.starr.home.arpa:18088"})
                self.assertIn("config", proxied.request("/api", "POST", {
                    "mode": "get_config", "output": "json", "apikey": c["sab_key"]}, form=True))
                page = installer.API("http://127.0.0.1:18088", headers={"Host": "starr.home.arpa"})
                self.assertIn("icons/radarr.svg", page.request("/", raw=True))
                self.assertIn("<svg", page.request("/icons/radarr.svg", raw=True))
                with self.assertRaisesRegex(installer.InstallError, "HTTP 404"):
                    installer.API("http://127.0.0.1:18088", headers={"Host": "unknown.home.arpa"}).request("/")
                self.assertTrue((root / ".sab-provisioned").exists())
                for app in ("radarr", "sonarr", "lidarr"):
                    port, version, _, _ = installer.APPS[app]
                    api = installer.API(f"http://127.0.0.1:{port}/api/{version}", c["keys"][app])
                    client = next(item for item in api.request("/downloadclient") if item["name"] == "Portable SABnzbd")
                    self.assertTrue(client["enable"])
                    self.assertEqual(client["priority"], 50)
                    client["enable"] = False
                    api.request(f"/downloadclient/{client['id']}", "PUT", client)
                installer.configure_sabnzbd(c)
                self.assertFalse(next(item for item in api.request("/downloadclient")
                                      if item["name"] == "Portable SABnzbd")["enable"])
                installer.compose(c, "run", "--rm", "recyclarr", "sync")
                managed = {}
                for app in ("radarr", "sonarr"):
                    port, version, _, _ = installer.APPS[app]
                    api = installer.API(f"http://127.0.0.1:{port}/api/{version}", c["keys"][app])
                    expected = set(re.findall(r"^  - name: (.+)$", (
                        SOURCE / "recyclarr-configs" / "includes" / app / "quality-profiles.yml").read_text(), re.M))
                    self.assertEqual(len(expected), 30)
                    profiles = api.request("/qualityprofile")
                    naming = api.request("/config/naming")
                    if app == "radarr":
                        self.assertTrue(naming["renameMovies"])
                        self.assertIn("tmdb", naming["movieFolderFormat"].lower())
                    else:
                        self.assertTrue(naming["renameEpisodes"])
                        self.assertIn("tvdb", naming["seriesFolderFormat"].lower())
                    managed[app] = {x["name"]: x["id"] for x in profiles if x["name"] in expected}
                    self.assertEqual(set(managed[app]), expected)
                connections = installer.seerr_connections(c)
                for app, category in (("radarr", "movies"), ("sonarr", "tv")):
                    self.assertEqual(connections[app]["activeProfileName"], "HD - 1080p")
                    self.assertEqual(connections[app]["activeProfileId"], managed[app]["HD - 1080p"])
                    self.assertEqual(connections[app]["activeDirectory"], "/data/media/" + category)
                self.assertEqual(connections["sonarr"]["activeAnimeProfileId"],
                                 managed["sonarr"]["HD - 1080p Anime"])
                installer.compose(c, "run", "--rm", "recyclarr", "sync")
                for app in ("radarr", "sonarr"):
                    port, version, _, _ = installer.APPS[app]
                    api = installer.API(f"http://127.0.0.1:{port}/api/{version}", c["keys"][app])
                    actual = {x["name"]: x["id"] for x in api.request("/qualityprofile") if x["name"] in managed[app]}
                    self.assertEqual(actual, managed[app])
                # A second initialization must preserve databases and edited config files.
                config = root / "config" / "bazarr" / "config" / "config.yaml"
                before = config.read_bytes()
                installer.initialize(c, SOURCE)
                self.assertEqual(config.read_bytes(), before)
            except Exception:
                result = installer.compose(c, "logs", "--tail", "35", "qbittorrent",
                                           capture_output=True, text=True)
                print(result.stdout.replace(c["password"], "<test-password>"))
                path = root / "config" / "qbittorrent" / "qBittorrent" / "qBittorrent.conf"
                print("qBittorrent config keys:", [
                    line.split("=")[0] for line in path.read_text().splitlines()
                    if line.startswith("[") or "WebUI" in line or line.startswith("Password")])
                raise
            finally:
                installer.compose(c, "down", "--volumes", "--remove-orphans")


if __name__ == "__main__":
    unittest.main()
