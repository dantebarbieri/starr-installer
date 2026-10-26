import copy
import json
import os
import shutil
import subprocess
import sys
import unittest

from test_install import SOURCE, installer, settings as installer_settings


NODE = os.environ.get("STARR_NODE") or shutil.which("node")
if os.environ.get("STARR_REQUIRE_NODE") == "1" and not NODE:
    raise RuntimeError("Node.js is required for command-builder checks.")


def settings():
    tunnel = {"mode": "none", "profile": "", "country": "Netherlands",
              "forwarding": True, "port": "0", "username": "", "passwordFile": ""}
    return {"root": "/opt/starr", "content": "/srv/media", "uid": "1000", "gid": "1000",
            "timezone": "Etc/UTC", "bind": "127.0.0.1", "username": "media", "subtitleLanguage": "en",
            "apps": dict.fromkeys(("sabnzbd", "soulseek", "plex", "jellyfin", "seerr"), False),
            "autoUpdate": False, "syncProfiles": True, "setupMode": "guided",
            "interpreter": "python", "delivery": "download", "allowDirect": False,
            "vpn": copy.deepcopy(tunnel), "soulseekVpn": copy.deepcopy(tunnel),
            "soulseekAccount": {"username": "", "passwordFile": ""}}


@unittest.skipUnless(NODE, "JavaScript command generation requires Node.js")
class SiteTests(unittest.TestCase):
    def build(self, cases, timezone=None):
        result = subprocess.run([NODE, "--input-type=module", "-e", """
import { readFileSync } from 'node:fs';
import { buildCommand, buildAccessLinks } from './scripts/starr/site/app.mjs';
const input = JSON.parse(readFileSync(0, 'utf8'));
if (input.timezone) process.env.TZ = input.timezone;
const results = input.cases.map(settings => {
  try { return {...buildCommand(settings, 'https://starr.danteb.com'), access: buildAccessLinks(settings)}; }
  catch (error) { return { error: error.message }; }
});
console.log(JSON.stringify(results));
"""], input=json.dumps({"cases": cases, "timezone": timezone}), cwd=SOURCE,
                                text=True, capture_output=True, check=True, timeout=30)
        return json.loads(result.stdout)

    def test_automatic_timezone_is_pinned_and_manual_selection_overrides_it(self):
        cases = [settings() for _ in range(3)]
        cases[0]["timezone"] = "automatic"
        cases[1]["timezone"] = "Etc/UTC"
        cases[2].update(timezone="custom", customTimezone="Asia/Tokyo")
        for case, built, expected in zip(cases, self.build(cases, timezone="America/New_York"),
                                         ("America/New_York", "Etc/UTC", "Asia/Tokyo")):
            with self.subTest(timezone=case["timezone"]):
                self.assertNotIn("error", built)
                parser = installer.argument_parser()
                args = parser.parse_args(built["args"])
                self.assertEqual(args.timezone, expected)
                self.assertIn(f"--timezone='{expected}'", built["command"])
                self.assertIn("sudo python3 ./starr-install.py", built["command"])

    def test_icon_access_links_match_installer_and_proxy_choices(self):
        direct = settings()
        direct.update(accessHost="SERVER.local.")
        proxied = settings()
        proxied.update(proxy=True, proxyDomain="media.home.arpa", proxyPort="8088", accessHost="192.168.1.100")
        proxied["apps"] = dict.fromkeys(proxied["apps"], True)
        proxied["soulseekAccount"]["username"] = "test"
        unspecified = settings()
        unspecified["bind"] = "0.0.0.0"
        for case, built in zip((direct, proxied, unspecified), self.build([direct, proxied, unspecified])):
            self.assertNotIn("error", built)
            parser = installer.argument_parser()
            args = parser.parse_args(built["args"])
            installer.validate_options(parser, args)
            c = installer_settings()
            c.update(case["apps"])
            c.update(bind=case["bind"], proxy=bool(case.get("proxy")), access_host=case.get("accessHost", ""),
                     proxy_domain=case.get("proxyDomain", "starr.home.arpa"),
                     proxy_port=int(case.get("proxyPort", 80)), soulseek_username=case["soulseekAccount"]["username"])
            installer.validate_access(c)
            expected = installer.access_links(c)
            self.assertEqual([{**item, "icon": "/icons/" + item["icon"]} for item in expected], built["access"]["links"])
        self.assertEqual(self.build([proxied])[0]["access"]["dashboard"], "http://media.home.arpa:8088/")
        self.assertTrue(self.build([proxied])[0]["access"]["hostsEntry"].startswith("192.168.1.100 media.home.arpa "))
        for field, value in (("accessHost", "http://bad"), ("accessHost", "999.2.3.4"),
                             ("proxyDomain", "public.example"), ("proxyPort", "8080"), ("proxyPort", "0")):
            bad = copy.deepcopy(proxied)
            bad[field] = value
            self.assertIn("error", self.build([bad])[0])

    def test_all_vpn_combinations_match_real_installer_parser(self):
        cases = []
        for mode in ("guided", "unattended"):
            for torrents in ("none", "proton", "wireguard", "openvpn"):
                for soulseek in ("none", "proton", "wireguard", "openvpn"):
                    case = settings()
                    case.update(setupMode=mode, allowDirect=True, autoUpdate=True)
                    case["apps"] = dict.fromkeys(case["apps"], True)
                    case["vpn"].update(mode=torrents, profile="/root/vpn/torrents.conf")
                    case["soulseekVpn"].update(mode=soulseek, profile="/root/vpn/music.conf")
                    cases.append(case)
        for case, built in zip(cases, self.build(cases)):
            with self.subTest(mode=case["setupMode"], torrents=case["vpn"]["mode"],
                              soulseek=case["soulseekVpn"]["mode"]):
                self.assertNotIn("error", built)
                parser = installer.argument_parser()
                args = parser.parse_args(built["args"])
                installer.validate_options(parser, args)
                self.assertEqual(args.vpn, case["vpn"]["mode"])
                self.assertEqual(args.soulseek_vpn, case["soulseekVpn"]["mode"])
                self.assertTrue(args.sabnzbd)
                self.assertTrue(args.sync_profiles)
                self.assertTrue(args.auto_update)
                self.assertEqual(args.subtitle_language, "en")

    def test_subtitle_language_and_manual_setup_match_installer_options(self):
        cases = [settings() for _ in range(3)]
        cases[0]["subtitleLanguage"] = "es"
        cases[1]["subtitleLanguage"] = "none"
        cases[2].update(subtitleLanguage="custom", customSubtitleLanguage="SV")
        for built, expected in zip(self.build(cases), ("es", "none", "sv")):
            parser = installer.argument_parser()
            args = parser.parse_args(built["args"])
            installer.validate_options(parser, args)
            self.assertEqual(args.subtitle_language, expected)

    def test_optional_application_settings_use_private_files_and_plex_default(self):
        both = settings()
        both["apps"].update(plex=True, jellyfin=True, seerr=True, sabnzbd=True)
        both.update(plexClaimFile="/root/secrets/plex-claim", usenetServerFile="/root/secrets/usenet.json",
                    requestProfile="HD - 1080p")
        jellyfin = copy.deepcopy(both)
        jellyfin["seerrMediaServer"] = "jellyfin"
        for case, selected in ((both, "plex"), (jellyfin, "jellyfin")):
            built = self.build([case])[0]
            self.assertNotIn("error", built)
            parser = installer.argument_parser()
            args = parser.parse_args(built["args"])
            installer.validate_options(parser, args)
            self.assertEqual(args.seerr_media_server, selected)
            self.assertEqual(args.plex_claim_file, both["plexClaimFile"])
            self.assertEqual(args.usenet_server_file, both["usenetServerFile"])
            self.assertEqual(args.request_profile, "HD - 1080p")
        missing = settings()
        missing["apps"]["seerr"] = True
        self.assertIn("error", self.build([missing])[0])

    def test_account_files_static_forwarding_and_disabled_flags(self):
        case = settings()
        case.update(setupMode="unattended", syncProfiles=False)
        case["apps"]["soulseek"] = True
        case["vpn"].update(mode="proton", forwarding=False, port="55555", profile="/root/torrents.conf")
        case["soulseekVpn"].update(mode="openvpn", profile="/root/music.ovpn", port="55556",
                                  username="provider-user", passwordFile="/root/secrets/vpn")
        case["soulseekAccount"].update(username="music-user", passwordFile="/root/secrets/soulseek")
        built = self.build([case])[0]
        self.assertNotIn("error", built)
        parser = installer.argument_parser()
        args = parser.parse_args(built["args"])
        installer.validate_options(parser, args)
        self.assertFalse(args.vpn_port_forwarding)
        self.assertEqual(args.vpn_static_port, 55555)
        self.assertEqual(args.soulseek_vpn_password_file, "/root/secrets/vpn")
        self.assertFalse(args.sabnzbd)
        self.assertFalse(args.sync_profiles)
        self.assertFalse(args.allow_direct)

    def test_invalid_settings_block_command_instead_of_silent_defaults(self):
        cases = []
        for key, value in (("root", "/"), ("content", "/opt/starr/media"), ("uid", "0"),
                           ("uid", "1.5"), ("gid", "-1"), ("bind", "999.0.0.1"),
                           ("bind", "127.00.0.1"), ("timezone", "not-a-timezone"),
                           ("content", "/srv/media\nmalformed"), ("root", "/opt/100%"),
                           ("content", "/srv/symlink/../media"),
                           ("subtitleLanguage", "english"),
                           ("setupMode", "ignored"), ("interpreter", "zsh")):
            case = settings()
            case[key] = value
            cases.append(case)
        case = settings()
        case["setupMode"] = "unattended"
        cases.append(case)
        for custom in ("", "not-a-timezone", "UTC\nunexpected"):
            case = settings()
            case.update(timezone="custom", customTimezone=custom)
            cases.append(case)
        case = settings()
        case["vpn"].update(mode="wireguard", profile="relative.conf")
        cases.append(case)
        case = settings()
        case.update(setupMode="unattended", allowDirect=True)
        case["apps"]["soulseek"] = True
        case["soulseekAccount"]["username"] = "missing-password"
        cases.append(case)
        for built in self.build(cases):
            self.assertIn("error", built)
            self.assertNotIn("command", built)

    @unittest.skipUnless(sys.platform == "linux", "Shell argv roundtrip runs on Linux")
    def test_every_entry_point_preserves_literal_shell_values(self):
        cases = []
        for interpreter in ("sh", "bash", "python"):
            for delivery in ("download", "pipe"):
                case = settings()
                case.update(interpreter=interpreter, delivery=delivery,
                            content="/srv/media's $(printf injected); `printf changed`")
                cases.append(case)
        for case, built in zip(cases, self.build(cases)):
            with self.subTest(interpreter=case["interpreter"], delivery=case["delivery"]):
                self.assertNotIn("error", built)
                # Replace network/root commands with shell functions, then compare exact argv.
                program = "curl() { :; }\nsudo() { printf '%s\\0' \"$@\"; }\n" + built["command"]
                result = subprocess.run(["sh", "-c", program], capture_output=True, check=True, timeout=10)
                actual = result.stdout.decode().rstrip("\0").split("\0")
                python = case["interpreter"] == "python"
                prefix = ["python3" if python else case["interpreter"]]
                if case["delivery"] == "download":
                    prefix += ["./starr-install.py" if python else "./starr-install.sh"]
                else:
                    prefix += ["-"] if python else ["-s", "--"]
                self.assertEqual(actual, [*prefix, *built["args"]])


if __name__ == "__main__":
    unittest.main()
