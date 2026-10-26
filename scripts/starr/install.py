#!/usr/bin/env python3
"""Portable, private-by-default Starr stack installer (Python 3.10+, stdlib only)."""

import argparse
import base64
import configparser
import copy
import getpass
import hashlib
import html
import http.cookiejar
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET


APPS = {
    "radarr": (7878, "v3", "movies", "movieCategory"),
    "sonarr": (8989, "v3", "tv", "tvCategory"),
    "lidarr": (8686, "v1", "music", "musicCategory"),
    "prowlarr": (9696, "v1", None, None),
}
PORTS = {"radarr": 7878, "sonarr": 8989, "lidarr": 8686, "prowlarr": 9696,
         "bazarr": 6767, "qbittorrent": 8080, "sabnzbd": 8085, "slskd": 5030,
         "plex": 32400, "jellyfin": 8096, "seerr": 5055}
OPTIONAL = ("sabnzbd", "soulseek", "plex", "jellyfin", "seerr")
ROOTS = ("movies", "tv", "music")
RECYCLARR_VERSION = "8.8.0"
PROFILE_FILES = (
    "recyclarr-configs/recyclarr.yml", "recyclarr-configs/settings.yml",
    "recyclarr-configs/includes/sonarr/quality-profiles.yml",
    "recyclarr-configs/includes/sonarr/custom-formats.yml",
    "recyclarr-configs/includes/radarr/quality-profiles.yml",
    "recyclarr-configs/includes/radarr/custom-formats.yml",
    "recyclarr-configs/custom-formats/english-anime-audio.json",
)
PROFILE_PROVENANCE = "recyclarr-configs/provenance.json"
APP_LABELS = {name: name.capitalize() for name in PORTS}
APP_LABELS.update(qbittorrent="qBittorrent", sabnzbd="SABnzbd", slskd="slskd", seerr="Seerr")
ICON_FILES = tuple(name + ".svg" for name in PORTS) + ("LICENSE", "NOTICE")


class InstallError(Exception):
    pass


def verify_profile_snapshot(source, contents=None):
    def read(name):
        if contents is not None:
            return contents[name]
        path = Path(source) / name
        if path.is_symlink() or not path.is_file() or Path(source).resolve() not in path.resolve().parents:
            raise ValueError(f"Profile snapshot must contain regular files: {name}")
        return path.read_bytes()

    manifest = json.loads(read(PROFILE_PROVENANCE))
    if (not isinstance(manifest, dict) or
            manifest.get("repository") != "dantebarbieri/homeserver" or
            not isinstance(manifest.get("commit"), str) or
            not re.fullmatch(r"[0-9a-f]{40}", manifest["commit"]) or
            manifest.get("recyclarr_version") != RECYCLARR_VERSION or
            not isinstance(manifest.get("profiles"), dict) or
            set(manifest["profiles"]) != set(PROFILE_FILES)):
        raise ValueError("Invalid profile provenance or Recyclarr version policy.")
    for name in PROFILE_FILES:
        if hashlib.sha256(read(name)).hexdigest() != manifest["profiles"][name]:
            raise ValueError(f"Profile snapshot checksum mismatch: {name}")
    return manifest


def access_hostname(value):
    value = value.lower().removesuffix(".")
    if len(value) > 253 or not re.fullmatch(
            r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)*", value):
        raise InstallError("Access host must be an IPv4 address or DNS hostname, without a scheme, port or path.")
    if re.fullmatch(r"[\d.]+", value):
        if ipaddress.IPv4Address(value).is_unspecified:
            raise InstallError("Use a reachable server address, not 0.0.0.0.")
    return value


def validate_access(c):
    if c.get("access_host"):
        c["access_host"] = access_hostname(c["access_host"])
    if c.get("proxy"):
        domain = access_hostname(c.get("proxy_domain", "starr.home.arpa"))
        if not domain.endswith(".home.arpa") or len(domain) > 230:
            raise InstallError("Use a local proxy domain below home.arpa, such as starr.home.arpa.")
        port = c.get("proxy_port", 80)
        if type(port) is not int or not 1 <= port <= 65535 or port in (*PORTS.values(), 6881, 50300):
            raise InstallError("Proxy port must be 1-65535 and different from the stack's app/peer ports.")
        c.update(proxy_domain=domain, proxy_port=port)


def web_apps(c):
    optional = {name: c[name] for name in ("sabnzbd", "plex", "jellyfin", "seerr")}
    optional["slskd"] = c["soulseek"] and bool(c["soulseek_username"])
    return [name for name in PORTS if optional.get(name, True)]


def access_links(c):
    host = c.get("access_host") or (c["bind"] if c["bind"] != "0.0.0.0" else "")
    suffix = "" if c.get("proxy_port", 80) == 80 else ":" + str(c["proxy_port"])
    return [{"name": name, "label": APP_LABELS[name], "icon": name + ".svg",
             "direct": f"http://{host}:{PORTS[name]}" + ("/web/" if name == "plex" else "/") if host else "",
             "proxy": f"http://{name}.{c['proxy_domain']}{suffix}" + ("/web/" if name == "plex" else "/")
             if c.get("proxy") else ""} for name in web_apps(c)]


def caddy_config(c):
    lines = ["{", "\tadmin off", "\tauto_https off", "\tpersist_config off", "}", ":8080 {",
             '\theader X-Content-Type-Options nosniff', '\theader Referrer-Policy no-referrer',
             f'\t@dashboard host {c["proxy_domain"]}', "\thandle @dashboard {",
             "\t\troot * /srv", "\t\tfile_server", "\t}"]
    for name in web_apps(c):
        upstream = ("gluetun" if c["vpn"] else name) if name == "qbittorrent" else (
            "gluetun-soulseek" if name == "slskd" and c["soulseek_vpn"] else name)
        port = 8080 if name == "sabnzbd" else PORTS[name]
        lines += [f"\t@{name} host {name}.{c['proxy_domain']}", f"\thandle @{name} {{"]
        if name == "qbittorrent":
            # qBittorrent validates Host against its own port; X-Forwarded-Host retains the browser origin.
            lines += [f"\t\treverse_proxy {upstream}:{port} {{", "\t\t\theader_up Host {upstream_hostport}", "\t\t}"]
        else:
            lines.append(f"\t\treverse_proxy {upstream}:{port}")
        lines.append("\t}")
    return "\n".join(lines + ['\trespond "Unknown local app hostname" 404', "}", ""])


def write_access_files(c, source):
    root = Path(c["root"])
    directory = root / "access"
    for name in ICON_FILES:
        target = directory / "icons" / name
        if not target.exists():
            origin = source / "scripts" / "starr" / "site" / "icons" / name
            if not origin.is_file():
                raise InstallError("Access icons are missing; rerun using the downloaded installer bundle or repository checkout.")
            atomic_write(target, origin.read_text(encoding="utf-8"), 0o644)
    cards = []
    for link in access_links(c):
        url = link["proxy"] or link["direct"]
        title = f'<img src="icons/{link["icon"]}" width="40" height="40" alt=""> {link["label"]}'
        cards.append('<article>' + (f'<a href="{html.escape(url, quote=True)}">{title}</a>' if url else title) +
                     (f'<small>{html.escape(link["proxy"])}</small>' if link["proxy"] else "") +
                     (f'<small><a href="{html.escape(link["direct"], quote=True)}">Direct: {html.escape(link["direct"])}</a></small>'
                      if link["direct"] else "<small>Supply --access-host for direct links.</small>") + "</article>")
    dns = ""
    if c.get("proxy"):
        names = [c["proxy_domain"], *(name + "." + c["proxy_domain"] for name in web_apps(c))]
        host = c.get("access_host") or c["bind"]
        address = host if re.fullmatch(r"[\d.]+", host) and host != "0.0.0.0" else "SERVER_LAN_IPV4"
        entry = address + " " + " ".join(names)
        atomic_write(directory / "hosts.txt", "# Map these names on each client, or create equivalent local DNS records.\n" +
                     "# Use 127.0.0.1 only on clients forwarding the proxy port over SSH.\n" + entry + "\n", 0o644)
        dns = "<h2>Local DNS</h2><p>Map these names to the server's LAN IPv4 in local DNS or each client's hosts file. " \
              "Replace SERVER_LAN_IPV4 if shown. A hosts file does not support wildcard names.</p><pre>" + \
              html.escape(entry) + "</pre>"
        path = root / "config" / "caddy" / "Caddyfile"
        atomic_write(path, caddy_config(c), 0o644)
    page = '<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width">' \
           '<meta name="referrer" content="no-referrer"><meta http-equiv="Content-Security-Policy" ' \
           'content="default-src \'none\'; img-src \'self\'; style-src \'unsafe-inline\'; base-uri \'none\'; form-action \'none\'">' \
           '<title>Starr applications</title><style>body{font:16px system-ui;background:#0e131b;color:#ecf0f5;' \
           'max-width:1000px;margin:3rem auto;padding:1rem}a{color:#f1c179}main{display:grid;' \
           'grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:1rem}article{padding:1rem;' \
           'background:#161e29;border:1px solid #405066;border-radius:8px}article>a{display:flex;gap:1rem;' \
           'align-items:center}small{display:block;margin-top:1rem;overflow-wrap:anywhere}pre{white-space:pre-wrap;' \
           'overflow-wrap:anywhere}img{object-fit:contain}</style><h1>Starr applications</h1>' \
           '<p>HTTP access only. Finish app authentication before allowing LAN access. Loopback binds require SSH tunnels. ' \
           'Do not expose these ports to the internet. Links do not check service readiness.</p><main>' + \
           "".join(cards) + "</main>" + dns + \
           '<p>Icons: <a href="icons/NOTICE">Dashboard Icons attribution</a> &middot; <a href="icons/LICENSE">license</a>.</p></html>'
    atomic_write(directory / "index.html", page, 0o644)


def run(args, **kwargs):
    return subprocess.run([str(a) for a in args], check=True, **kwargs)


def docker_preflight():
    host = None if os.environ.get("DOCKER_CONTEXT") else os.environ.get("DOCKER_HOST")
    if not host:
        host = run(["docker", "context", "inspect", "--format",
                    '{{(index .Endpoints "docker").Host}}'],
                   capture_output=True, text=True).stdout.strip()
    if not host.startswith("unix://"):
        raise InstallError("Use a local Docker Engine Unix socket, not a remote Docker context.")
    result = run(["docker", "info", "--format", "{{.OSType}} {{json .SecurityOptions}}"],
                 capture_output=True, text=True).stdout
    if not result.startswith("linux ") or "rootless" in result:
        raise InstallError("A rootful Linux Docker Engine is required.")
    run(["docker", "compose", "version"], stdout=subprocess.DEVNULL)


def atomic_write(path, text, mode=0o600):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(dir=path.parent)
    try:
        os.fchmod(fd, mode) if hasattr(os, "fchmod") else os.chmod(temp, mode)
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(text)
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def write_json(path, value):
    atomic_write(path, json.dumps(value, indent=2) + "\n")


def ask(label, default=None, secret=False):
    suffix = f" [{default}]" if default is not None and not secret else ""
    value = (getpass.getpass if secret else input)(f"{label}{suffix}: ").strip()
    return value or default or ""


def yes(label, default=False):
    while True:
        value = ask(label + " (yes/no)", "yes" if default else "no").lower()
        if value in ("yes", "y", "no", "n"):
            return value in ("yes", "y")
        print("Enter yes or no.")


def absolute_path(value):
    path = Path(value).expanduser()
    if not path.is_absolute() or any(c in str(path) for c in ("\n", "\r", "\0", "%")):
        raise InstallError("Use an absolute path without newlines, NUL or '%' (cron delimiter).")
    path = path.resolve()
    if str(path) in ("/", "/etc", "/usr", "/var", "/home", "/srv", "/opt",
                     "/tmp", "/var/tmp", "/run", "/mnt", "/media", "/root"):
        raise InstallError("Choose a dedicated subdirectory, not a system directory.")
    return path


def validate_paths(root, content):
    if root == content or root in content.parents or content in root.parents:
        raise InstallError("Installation/config and content must be separate, non-nested directories.")


def option(args, name, label, default=None, secret=False):
    value = getattr(args, name, None)
    if value is not None:
        return value
    if args.non_interactive:
        return default if default is not None else ""
    return ask(label, str(default) if default is not None else None, secret)


def toggle(args, name, label, default=False):
    value = getattr(args, name, None)
    if value is not None:
        return value
    return default if args.non_interactive else yes(label, default)


def consent(args, label, direct=False):
    if direct and args.allow_direct:
        return True
    if args.non_interactive and direct:
        raise InstallError("Direct peer traffic requires --allow-direct or a VPN profile.")
    if args.yes and not direct:
        return True
    return yes(label)


def password_file(path):
    if path is None:
        return None
    value = Path(path).expanduser().read_text().rstrip("\r\n")
    if not value or "\n" in value or "\r" in value:
        raise InstallError("Password files must contain one nonempty line.")
    return value


def secret_option(args, path, label):
    value = password_file(path)
    return value if value is not None else ("" if args.non_interactive else ask(label, secret=True))


def usenet_server_file(path):
    if not path:
        return None
    server = json.loads(Path(path).expanduser().read_text())
    if not isinstance(server, dict) or set(server) - {"host", "port", "username", "password", "ssl", "connections"}:
        raise InstallError("Usenet JSON accepts host, port, username, password, ssl and connections.")
    for field in ("host", "username", "password"):
        if not isinstance(server.get(field), str) or not server[field] or any(
                ch in server[field] for ch in ("\r", "\n", "\0")):
            raise InstallError(f"Usenet JSON needs a nonempty single-line {field}.")
    server = {"port": 563, "ssl": True, "connections": 20, **server}
    for field, maximum in (("port", 65535), ("connections", 100)):
        if type(server[field]) is not int or not 1 <= server[field] <= maximum:
            raise InstallError(f"Usenet {field} must be an integer from 1 to {maximum}.")
    if not isinstance(server["ssl"], bool):
        raise InstallError("Usenet ssl must be a JSON boolean.")
    return server


def vpn_questions(label, args, prefix="vpn"):
    print(f"\n{label}: 0 = no VPN, 1 = Proton WireGuard, "
          "2 = any provider's WireGuard export, 3 = custom OpenVPN.")
    print("Proton: https://account.proton.me/u/0/vpn/WireGuard -> Linux -> "
          "P2P server; enable NAT-PMP if your plan supports port forwarding.")
    print("Other providers: export a Linux WireGuard or inline-certificate OpenVPN profile. "
          "See https://github.com/qdm12/gluetun-wiki/tree/main/setup/providers")
    mode = getattr(args, prefix)
    choice = {"none": "0", "proton": "1", "wireguard": "2", "openvpn": "3"}.get(mode)
    if choice is None:
        choice = "0" if args.non_interactive else ask("VPN mode", "0")
    if choice not in ("0", "1", "2", "3"):
        raise InstallError("Invalid VPN mode.")
    validate_vpn_flags(args, prefix, ("none", "proton", "wireguard", "openvpn")[int(choice)])
    if choice == "0":
        print(f"WARNING: {label} will use this machine's normal internet connection.")
        if not consent(args, "Confirm direct, non-VPN traffic", direct=True):
            raise InstallError("Direct traffic not approved.")
        return None
    profile = Path(option(args, prefix + "_profile", "Absolute path to downloaded VPN profile")).expanduser().resolve()
    if not profile.is_file():
        raise InstallError("VPN profile does not exist.")
    result = {"mode": choice, "source": str(profile), "forwarding": False, "static_port": 0}
    if choice == "1":
        result["country"] = option(args, prefix + "_country", "Proton server country", "Netherlands")
        result["forwarding"] = toggle(args, prefix + "_port_forwarding",
                                      "Enable Proton NAT-PMP port forwarding", True)
    elif choice == "3":
        result["username"] = option(args, prefix + "_username", "OpenVPN-specific username")
        result["password"] = secret_option(args, getattr(args, prefix + "_password_file"),
                                           "OpenVPN-specific password")
    if not result["forwarding"]:
        result["static_port"] = int(option(args, prefix + "_static_port",
                                           "Provider's static forwarded peer port (0 if none)", 0))
        if not 0 <= result["static_port"] <= 65535:
            raise InstallError("Invalid forwarded port.")
        if not result["static_port"]:
            print("No inbound VPN peer port: downloads can work, but peer reachability is reduced.")
    elif getattr(args, prefix + "_static_port") is not None:
        raise InstallError("A static peer port cannot be combined with dynamic port forwarding.")
    return result


def collect(root, args):
    content = absolute_path(option(args, "content", "Content directory (downloads + library)", "/srv/media"))
    validate_paths(root, content)
    uid = int(option(args, "uid", "Media owner UID (non-root)", os.environ.get("SUDO_UID", "1000")))
    gid = int(option(args, "gid", "Media group GID", os.environ.get("SUDO_GID", "1000")))
    if uid <= 0 or gid < 0:
        raise InstallError("Use a non-root media UID and a valid group.")
    print("Web interfaces default to loopback. Use SSH tunnels for initial account setup.")
    print("Do not expose these ports to the internet. Docker-published ports can bypass UFW.")
    bind = option(args, "bind", "Web UI bind IPv4 address (127.0.0.1 recommended)", "127.0.0.1")
    if ipaddress.ip_address(bind).version != 4:
        raise InstallError("The web bind address must be IPv4.")
    if bind != "127.0.0.1" and not consent(args, "Expose web UIs on this address before account onboarding"):
        raise InstallError("Web UI exposure not approved.")
    config = {"version": 1, "root": str(root), "content": str(content), "uid": uid, "gid": gid,
              "bind": bind, "timezone": option(args, "timezone", "IANA timezone", "Etc/UTC"),
              "username": option(args, "username", "qBittorrent / slskd web username", "media"),
              "password": secrets.token_urlsafe(24),
              "keys": {app: secrets.token_hex(16) for app in APPS},
              "bazarr_key": secrets.token_hex(16), "sab_key": secrets.token_hex(16),
              "slskd_key": secrets.token_hex(24)}
    for service in OPTIONAL:
        config[service] = toggle(args, service, f"Install optional {service}")
    config["access_host"] = option(args, "access_host", "Server hostname/IPv4 for links (blank uses bind address)", "")
    config["proxy"] = toggle(args, "proxy", "Install optional Caddy local HTTP proxy")
    if config["proxy"]:
        config["proxy_domain"] = option(args, "proxy_domain", "Local proxy domain", "starr.home.arpa")
        config["proxy_port"] = int(option(args, "proxy_port", "Proxy HTTP port", 80))
    elif args.proxy_domain is not None or args.proxy_port is not None:
        raise InstallError("Proxy settings require --proxy.")
    validate_access(config)
    for service, names in (("seerr", ("seerr_media_server", "request_profile")),
                           ("plex", ("plex_claim_file",)), ("sabnzbd", ("usenet_server_file",))):
        if not config[service] and any(getattr(args, name, None) is not None for name in names):
            raise InstallError(f"{service} setup options require --{service}.")
    config["seerr_media_server"] = ""
    if config["seerr"]:
        default = "plex" if config["plex"] else ("jellyfin" if config["jellyfin"] else "plex")
        config["seerr_media_server"] = option(
            args, "seerr_media_server", "Seerr media server (plex/jellyfin)", default).lower()
        if config["seerr_media_server"] not in ("plex", "jellyfin"):
            raise InstallError("Seerr media server must be plex or jellyfin.")
        if not config[config["seerr_media_server"]]:
            raise InstallError("Select the media server used by Seerr; external servers are not adopted.")
        config["request_profile"] = option(
            args, "request_profile", "Seerr quality profile name (blank for HD 1080p default)", "")
    config["plex_claim_file"] = option(
        args, "plex_claim_file", "Private file with fresh Plex claim token (blank to defer)", "") if config["plex"] else ""
    config["usenet_server_file"] = option(
        args, "usenet_server_file", "Private Usenet server JSON file (blank to defer)", "") if config["sabnzbd"] else ""
    config["usenet_server"] = usenet_server_file(config["usenet_server_file"])
    config["subtitle_language"] = option(
        args, "subtitle_language", "Bazarr subtitle language (two-letter code, or none to choose later)", "en").lower()
    if config["subtitle_language"] != "none" and not re.fullmatch("[a-z]{2}", config["subtitle_language"]):
        raise InstallError("Use a two-letter Bazarr language code, or 'none'.")
    if not config["soulseek"] and any(name.startswith("soulseek_") for name in cli_options(args)):
        raise InstallError("Soulseek account/VPN options require --soulseek.")
    config["vpn"] = vpn_questions("qBittorrent", args)
    config["soulseek_vpn"] = None
    config["soulseek_username"] = ""
    config["soulseek_password"] = ""
    if config["soulseek"]:
        print("Soulseek credentials can be left blank; slskd and Soularr then stay stopped.")
        config["soulseek_username"] = option(args, "soulseek_username", "Soulseek network username (blank to defer)")
        if not config["soulseek_username"] and args.soulseek_password_file is not None:
            raise InstallError("A Soulseek password file requires a network username.")
        if config["soulseek_username"]:
            config["soulseek_password"] = secret_option(args, args.soulseek_password_file,
                                                       "Soulseek network password")
            if not config["soulseek_password"]:
                raise InstallError("Soulseek password is required when supplying a username.")
        print("Soulseek needs its OWN VPN profile/tunnel for a second inbound peer port. "
              "Do not reuse the same WireGuard key in simultaneous tunnels.")
        config["soulseek_vpn"] = vpn_questions("Soulseek", args, "soulseek_vpn")
        if config["vpn"] and config["soulseek_vpn"]:
            if Path(config["vpn"]["source"]).read_bytes() == Path(
                    config["soulseek_vpn"]["source"]).read_bytes():
                raise InstallError("Use distinct VPN profiles for the two tunnels.")
    config["auto_update"] = toggle(args, "auto_update", "Install daily 04:00 cron image updates")
    config["sync_profiles"] = toggle(args, "sync_profiles", "Apply repository Recyclarr profiles and naming", True)
    return config


def wireguard_environment(path, proton=False):
    parser = configparser.ConfigParser(interpolation=None)
    try:
        with Path(path).open() as stream:
            parser.read_file(stream)
    except configparser.Error:
        raise InstallError("Invalid WireGuard profile; export one Interface and one Peer.") from None
    try:
        private = parser["Interface"]["PrivateKey"]
        address = parser["Interface"]["Address"]
        peer = parser["Peer"]
        public = peer["PublicKey"]
        endpoint = peer["Endpoint"]
    except KeyError as exc:
        raise InstallError(f"Missing WireGuard field: {exc}") from None
    for key in (private, public, peer.get("PresharedKey", private)):
        try:
            valid = len(base64.b64decode(key, validate=True)) == 32
        except ValueError:
            valid = False
        if not valid:
            raise InstallError("Invalid WireGuard key.")
    for item in address.split(","):
        ipaddress.ip_interface(item.strip())
    env = {"VPN_SERVICE_PROVIDER": "protonvpn" if proton else "custom",
           "VPN_TYPE": "wireguard", "WIREGUARD_PRIVATE_KEY": private,
           "WIREGUARD_ADDRESSES": address}
    if not proton:
        host, port = endpoint.rsplit(":", 1)
        host = host.strip("[]")
        try:
            ipaddress.ip_address(host)
        except ValueError:
            raise InstallError("Custom WireGuard Endpoint must be an IP address, not a hostname.") from None
        if not 1 <= int(port) <= 65535:
            raise InstallError("Invalid WireGuard endpoint port.")
        env.update(WIREGUARD_ENDPOINT_IP=host, WIREGUARD_ENDPOINT_PORT=port,
                   WIREGUARD_PUBLIC_KEY=public)
        if peer.get("PresharedKey"):
            env["WIREGUARD_PRESHARED_KEY"] = peer["PresharedKey"]
    return env


def prepare_vpn(config):
    root = Path(config["root"])
    for name, vpn in (("gluetun", config["vpn"]), ("gluetun-soulseek", config["soulseek_vpn"])):
        if vpn is None:
            continue
        target = root / "private" / (name + ".conf")
        if "source" in vpn:
            source = Path(vpn["source"])
            atomic_write(target, source.read_text())
            del vpn["source"]
        if not target.exists():
            raise InstallError(f"Missing saved VPN profile: {target}")
        if vpn["mode"] in ("1", "2"):
            vpn["environment"] = wireguard_environment(target, vpn["mode"] == "1")
        else:
            text = target.read_text()
            # OpenVPN configuration can execute commands as root inside Gluetun.
            if re.search(r"(?mi)^\s*(up|down|route-up|ipchange|plugin|script-security|"
                         r"client-connect|client-disconnect|tls-verify|auth-user-pass-verify)\b", text):
                raise InstallError("OpenVPN executable hooks/plugins are not accepted.")
            for certificate in ("ca", "cert", "key", "tls-auth", "tls-crypt"):
                if re.search(rf"(?mi)^\s*{certificate}\s+\S+", text):
                    raise InstallError("Use a self-contained OpenVPN export with inline certificates/keys.")
            vpn["environment"] = {"VPN_SERVICE_PROVIDER": "custom", "VPN_TYPE": "openvpn",
                                  "OPENVPN_CUSTOM_CONFIG": "/gluetun/custom.conf",
                                  "OPENVPN_USER": vpn["username"],
                                  "OPENVPN_PASSWORD": vpn["password"]}
    tunnels = [v["environment"].get("WIREGUARD_PRIVATE_KEY")
               for v in (config["vpn"], config["soulseek_vpn"]) if v]
    if len(tunnels) == 2 and tunnels[0] and tunnels[0] == tunnels[1]:
        raise InstallError("qBittorrent and Soulseek need distinct WireGuard private keys.")


def bind_volume(source, target, read_only=False):
    return {"type": "bind", "source": str(source), "target": target,
            "read_only": read_only, "bind": {"create_host_path": False}}


def compose_config(c):
    root, content = Path(c["root"]), Path(c["content"])
    services = {}

    def service(name, image, port=None, target_port=None, media=False, user=False):
        item = {"image": image, "restart": "unless-stopped",
                "logging": {"driver": "json-file", "options": {"max-size": "10m", "max-file": "3"}},
                "environment": {"TZ": c["timezone"]},
                "volumes": [bind_volume(root / "config" / name, "/config")]}
        if user:
            item["user"] = f'{c["uid"]}:{c["gid"]}'
        else:
            item["environment"].update(PUID=str(c["uid"]), PGID=str(c["gid"]), UMASK="002")
        if media:
            item["volumes"].append(bind_volume(content, "/data"))
        if port:
            item["ports"] = [f'{c["bind"]}:{port}:{target_port or port}']
        services[name] = item
        return item

    for app, (port, _, _, _) in APPS.items():
        service(app, f"ghcr.io/hotio/{app}:latest", port, media=app != "prowlarr")
    service("bazarr", "ghcr.io/hotio/bazarr:release", 6767, media=True)
    whisper = service("whisperasr", "onerahmet/openai-whisper-asr-webservice:latest")
    whisper["environment"].update(ASR_ENGINE="faster_whisper", ASR_MODEL="small",
                                  ASR_DEVICE="cpu", ASR_QUANTIZATION="int8")
    whisper["volumes"] = [bind_volume(root / "config" / "whisperasr", "/root/.cache")]
    service("flaresolverr", "ghcr.io/flaresolverr/flaresolverr:latest")["volumes"] = []
    qb = service("qbittorrent", "lscr.io/linuxserver/qbittorrent:latest", 8080, media=True)
    qb["environment"].update(WEBUI_PORT="8080", TORRENTING_PORT="6881")
    qb["healthcheck"] = {"test": ["CMD", "curl", "-fsS", "http://127.0.0.1:8080/"],
                         "interval": "15s", "timeout": "5s", "retries": 8, "start_period": "30s"}
    if not c["vpn"]:
        qb["ports"] += ["6881:6881/tcp", "6881:6881/udp"]
    if c["sabnzbd"]:
        service("sabnzbd", "ghcr.io/hotio/sabnzbd:latest", 8085, 8080, media=True)
    recyclarr = service("recyclarr", f"ghcr.io/recyclarr/recyclarr:{RECYCLARR_VERSION}", user=True)
    recyclarr["environment"].update(CRON_SCHEDULE="@daily")
    if not c["sync_profiles"]:
        recyclarr["profiles"] = ["profiles-manual"]
    if c["soulseek"]:
        slskd = service("slskd", "slskd/slskd:latest", 5030, media=True, user=True)
        slskd["volumes"][0] = bind_volume(root / "config" / "slskd", "/app")
        slskd["volumes"].append(bind_volume(content / "media" / "music", "/music", True))
        slskd["environment"].update(
            SLSKD_UMASK="002", SLSKD_SLSK_USERNAME=c["soulseek_username"],
            SLSKD_SLSK_PASSWORD=c["soulseek_password"], SLSKD_USERNAME=c["username"],
            SLSKD_PASSWORD=c["password"], SLSKD_API_KEY=c["slskd_key"],
            SLSKD_REMOTE_CONFIGURATION="true", SLSKD_SHARED_DIR="[Music]/music",
            SLSKD_DOWNLOADS_DIR="/data/soulseek/downloads",
            SLSKD_INCOMPLETE_DIR="/data/soulseek/incomplete")
        if c["soulseek_vpn"] and c["soulseek_vpn"]["static_port"]:
            slskd["environment"]["SLSKD_SLSK_LISTEN_PORT"] = str(c["soulseek_vpn"]["static_port"])
        soularr = service("soularr", "mrusse08/soularr:latest", user=True)
        soularr["volumes"] = [bind_volume(root / "config" / "soularr", "/data"),
                              bind_volume(content / "soulseek" / "downloads", "/downloads")]
        soularr["environment"].update(SCRIPT_INTERVAL="300")
        if not c["soulseek_username"]:
            slskd["profiles"] = ["soulseek-pending"]
            soularr["profiles"] = ["soulseek-pending"]
        if not c["soulseek_vpn"]:
            slskd["ports"].append("50300:50300/tcp")
    if c["plex"]:
        plex = service("plex", "lscr.io/linuxserver/plex:latest", 32400)
        plex["volumes"].append(bind_volume(content / "media", "/data/media", True))
        plex["environment"]["VERSION"] = "docker"
        if c.get("plex_claim"):
            plex["environment"]["PLEX_CLAIM"] = c["plex_claim"]
    if c["jellyfin"]:
        jf = service("jellyfin", "jellyfin/jellyfin:latest", 8096, user=True)
        jf["volumes"].append(bind_volume(content / "media", "/data/media", True))
        jf["volumes"].append(bind_volume(root / "config" / "jellyfin-cache", "/cache"))
    if c["seerr"]:
        seerr = service("seerr", "ghcr.io/hotio/seerr:latest", 5055)
        seerr["environment"]["PORT"] = "5055"

    for client, name, vpn in (("qbittorrent", "gluetun", c["vpn"]),
                              ("slskd", "gluetun-soulseek", c["soulseek_vpn"])):
        if not vpn:
            continue
        gateway = service(name, "qmcgaw/gluetun:latest")
        gateway["cap_add"] = ["NET_ADMIN"]
        gateway["devices"] = ["/dev/net/tun:/dev/net/tun"]
        gateway["volumes"] = [bind_volume(root / "config" / name, "/gluetun"),
                               bind_volume(root / "config" / (name + "-port"), "/tmp/gluetun")]
        if vpn["mode"] == "3":
            gateway["volumes"].append(bind_volume(root / "private" / (name + ".conf"),
                                                   "/gluetun/custom.conf", True))
        gateway["environment"].update(vpn["environment"])
        gateway["environment"]["FIREWALL_INPUT_PORTS"] = "8080" if client == "qbittorrent" else "5030"
        if vpn["static_port"]:
            gateway["environment"]["FIREWALL_VPN_INPUT_PORTS"] = str(vpn["static_port"])
        if vpn["mode"] == "1":
            gateway["environment"].update(SERVER_COUNTRIES=vpn["country"],
                                          PORT_FORWARD_ONLY="on" if vpn["forwarding"] else "off",
                                          VPN_PORT_FORWARDING="on" if vpn["forwarding"] else "off")
        gateway["ports"] = services[client].pop("ports")
        services[client]["network_mode"] = "service:" + name
        services[client]["depends_on"] = {name: {"condition": "service_healthy"}}
        if "profiles" in services[client]:
            gateway["profiles"] = services[client]["profiles"]
        if vpn["forwarding"]:
            helper = service(name + "-port-sync", "python:3.12-alpine", user=True)
            helper["network_mode"] = "service:" + name
            helper["depends_on"] = {name: {"condition": "service_healthy"}}
            helper["volumes"] = [
                bind_volume(root / "bin" / "starr.py", "/starr.py", True),
                bind_volume(root / "private" / "port-sync.json", "/credentials.json", True),
                bind_volume(root / "config" / (name + "-port"), "/port", True)]
            helper["command"] = ["python", "/starr.py", "port-sync", "--client", client]
            helper["read_only"] = True
            helper["cap_drop"] = ["ALL"]
            if "profiles" in services[client]:
                helper["profiles"] = services[client]["profiles"]
    if c.get("proxy"):
        proxy = service("caddy", "caddy:2-alpine", user=True)
        proxy["ports"] = [f'{c["bind"]}:{c.get("proxy_port", 80)}:8080']
        proxy["volumes"] = [bind_volume(root / "config" / "caddy" / "Caddyfile", "/etc/caddy/Caddyfile", True),
                            bind_volume(root / "access", "/srv", True)]
        proxy["read_only"] = True
        proxy["cap_drop"] = ["ALL"]
        # The official binary has this file capability; excluding it prevents exec.
        proxy["cap_add"] = ["NET_BIND_SERVICE"]
        proxy["tmpfs"] = [f"/{name}:uid={c['uid']},gid={c['gid']},mode=0700" for name in ("data", "config")]
        proxy["labels"] = {"starr.access-config": hashlib.sha256(caddy_config(c).encode()).hexdigest()}
        proxy["healthcheck"] = {
            "test": ["CMD", "wget", "-q", "--spider", "--header", "Host: " + c["proxy_domain"], "http://127.0.0.1:8080/"],
            "interval": "10s", "timeout": "5s", "retries": 6}
    # Compose interpolates '$' even in JSON. Escape credentials and paths, not just env values.
    project = "starr-" + hashlib.sha256(c["root"].encode()).hexdigest()[:10]
    return json.loads(json.dumps({"name": project, "services": services}).replace("$", "$$"))


def owned_directory(path, c):
    path = Path(path)
    if not path.exists():
        path.mkdir(parents=True)
        os.chown(path, c["uid"], c["gid"])
        path.chmod(0o2775)
    if path.is_symlink() or not path.is_dir():
        raise InstallError(f"Expected a real directory: {path}")


def seed(path, text, c):
    if not path.exists():
        atomic_write(path, text)
        os.chown(path, c["uid"], c["gid"])


def initialize(c, source):
    root, content = Path(c["root"]), Path(c["content"])
    target = root / "config" / "recyclarr"
    if not (target / ".portable-seeded").exists():
        try:
            verify_profile_snapshot(source)
        except (OSError, ValueError, KeyError, TypeError) as error:
            raise InstallError(f"Cannot seed unverified profile snapshot: {error}") from error
    owned_directory(content, c)
    for part in ("media", "torrents", "usenet", "usenet/complete", "usenet/incomplete",
                 "soulseek", "soulseek/downloads", "soulseek/incomplete"):
        owned_directory(content / part, c)
    for category in ROOTS:
        for parent in ("media", "torrents", "usenet/complete"):
            owned_directory(content / parent / category, c)
    # Refuse split mounts before starting downloads: same path prefix is not sufficient.
    probe = content / "torrents" / (".starr-probe-" + secrets.token_hex(8))
    destination = content / "media" / probe.name
    try:
        probe.write_text("hardlink test\n")
        os.link(probe, destination)
        paths = [content / parent / x for parent in ("media", "torrents", "usenet/complete")
                 for x in ROOTS] + [content / "usenet/incomplete", content / "soulseek/downloads"]
        for path in paths:
            if path.stat().st_dev != content.stat().st_dev:
                raise InstallError(f"Separate filesystem at {path}; hardlinks would fail.")
    except OSError as exc:
        raise InstallError(f"Content filesystem must support hardlinks across downloads/library: {exc}") from None
    finally:
        probe.unlink(missing_ok=True)
        destination.unlink(missing_ok=True)
    services = compose_config(c)["services"]
    for name in services:
        owned_directory(root / "config" / name, c)
    for name in ("jellyfin-cache", "gluetun-port", "gluetun-soulseek-port"):
        owned_directory(root / "config" / name, c)
    for app in APPS:
        seed(root / "config" / app / "config.xml",
             f"<Config><ApiKey>{c['keys'][app]}</ApiKey><AuthenticationMethod>Forms</AuthenticationMethod>"
             "<AuthenticationRequired>Enabled</AuthenticationRequired>"
             "<BindAddress>*</BindAddress></Config>\n", c)
    password = c["password"]
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha512", password.encode(), salt, 100000)
    pbkdf = f'@ByteArray({base64.b64encode(salt).decode()}:{base64.b64encode(digest).decode()})'
    seed(root / "config" / "qbittorrent" / "qBittorrent" / "qBittorrent.conf",
         "[LegalNotice]\nAccepted=true\n[Preferences]\n"
         f"WebUI\\Username={c['username']}\nWebUI\\Password_PBKDF2={pbkdf}\n"
         "WebUI\\Address=*\nWebUI\\LocalHostAuth=true\nWebUI\\CSRFProtection=true\n"
         # qBittorrent 5.3 moved WebUI settings out of the Preferences group.
         f"[WebUI]\nUsername={c['username']}\nPassword_PBKDF2={pbkdf}\n"
         "Address=*\nLocalHostAuth=true\nCSRFProtection=true\n"
         "[BitTorrent]\nSession\\DefaultSavePath=/data/torrents\n"
         "Session\\TempPath=/data/torrents/incomplete\nSession\\TempPathEnabled=true\n"
         "Session\\UseAlternativeGlobalSpeedLimit=false\n", c)
    # seed() creates nested parents; make qBittorrent's parent writable to its process.
    os.chown(root / "config" / "qbittorrent" / "qBittorrent", c["uid"], c["gid"])
    owned_directory(content / "torrents" / "incomplete", c)
    seed(root / "config" / "bazarr" / "config" / "config.yaml",
         json.dumps({"general": {"use_sonarr": True, "use_radarr": True},
                     "auth": {"apikey": c["bazarr_key"]},
                     "sonarr": {"ip": "sonarr", "port": 8989, "apikey": c["keys"]["sonarr"]},
                     "radarr": {"ip": "radarr", "port": 7878, "apikey": c["keys"]["radarr"]}}) + "\n", c)
    os.chown(root / "config" / "bazarr" / "config", c["uid"], c["gid"])
    if c["sabnzbd"]:
        seed(root / "config" / "sabnzbd" / "sabnzbd.ini",
             f"[misc]\napi_key = {c['sab_key']}\nhost = 0.0.0.0\nport = 8080\n"
             "host_whitelist = sabnzbd\n"
             "download_dir = /data/usenet/incomplete\ncomplete_dir = /data/usenet/complete\n"
             "permissions = 775\n"
             "[categories]\n" + "".join(
                 f"[[{x}]]\nname = {x}\ndir = {x}\npriority = -100\npp = 3\nscript = Default\n"
                 for x in ROOTS), c)
    if c["soulseek"]:
        seed(root / "config" / "soularr" / "config.ini",
             f"[Lidarr]\napi_key = {c['keys']['lidarr']}\nhost_url = http://lidarr:8686\n"
             "download_dir = /data/soulseek/downloads\ndisable_sync = False\n"
             f"[Slskd]\napi_key = {c['slskd_key']}\nhost_url = http://"
             f"{'gluetun-soulseek' if c['soulseek_vpn'] else 'slskd'}:5030\n"
             "url_base = /\ndownload_dir = /downloads\n"
             "[Search Settings]\nallowed_filetypes = flac 24/192,flac 16/44.1,flac\n"
             "search_type = incrementing_page\nnumber_of_albums_to_grab = 10\n"
             "minimum_search_interval = 5\nsearch_source = missing\n", c)
    if not (target / ".portable-seeded").exists():
        for name in (*PROFILE_FILES, PROFILE_PROVENANCE):
            destination = target / Path(name).relative_to("recyclarr-configs")
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source / name, destination)
        for path in [target] + list(target.rglob("*")):
            os.chown(path, c["uid"], c["gid"])
        seed(target / ".portable-seeded", "Repository profiles copied; reruns preserve local edits.\n", c)
    seed(target / "secrets.yml", "\n".join(
        f"{app}_url: http://{app}:{APPS[app][0]}\n{app}_apikey: {c['keys'][app]}"
        for app in ("sonarr", "radarr")) + "\n", c)
    write_json(root / "private" / "port-sync.json", {
        "username": c["username"], "password": c["password"], "slskd_key": c["slskd_key"]})
    os.chown(root / "private" / "port-sync.json", c["uid"], c["gid"])
    write_access_files(c, source)
    write_json(root / "compose.json", compose_config(c))
    atomic_write(root / "bin" / "starr.py", Path(__file__).read_text(), 0o755)


class API:
    def __init__(self, base, key=None, headers=None):
        self.base = base
        self.key = key
        self.headers = headers or {}
        self.opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}),
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))

    def request(self, path, method="GET", data=None, form=False, raw=False):
        headers = {"Referer": self.base + "/", **self.headers}
        if self.key:
            headers["X-Api-Key"] = self.key
        body = None
        if data is not None:
            body = (urllib.parse.urlencode(data, doseq=True) if form else json.dumps(data)).encode()
            headers["Content-Type"] = "application/x-www-form-urlencoded" if form else "application/json"
        req = urllib.request.Request(self.base + path, data=body, headers=headers, method=method)
        try:
            with self.opener.open(req, timeout=30) as response:
                value = response.read().decode()
        except urllib.error.HTTPError as exc:
            # API validation responses can echo secrets; never print the body or query.
            exc.close()
            raise InstallError(f"HTTP {exc.code} from {self.base}{path.split('?')[0]}") from None
        if raw:
            return value
        return json.loads(value) if value else None


def ready(api, path, timeout=240):
    deadline = time.monotonic() + timeout
    while True:
        try:
            return api.request(path)
        except (urllib.error.URLError, TimeoutError, ConnectionError, InstallError):
            if time.monotonic() >= deadline:
                raise InstallError(f"Timed out waiting for {api.base}; inspect Compose logs.") from None
            time.sleep(3)


def set_fields(model, values):
    fields = {f["name"]: f for f in model["fields"]}
    missing = values.keys() - fields.keys()
    if missing:
        raise InstallError(f"API schema changed; missing fields: {', '.join(sorted(missing))}")
    for name, value in values.items():
        fields[name]["value"] = value
    return model


def ensure_model(api, endpoint, implementation, name, fields, **values):
    existing = next((x for x in api.request(endpoint) if x["name"] == name), None)
    if existing:
        if existing["implementation"] != implementation:
            raise InstallError(f"{name} exists with a different implementation; refusing to overwrite.")
        # Preserve UI edits after the first successful creation.
        return existing
    schema = next((x for x in api.request(endpoint + "/schema")
                   if x["implementation"] == implementation), None)
    if schema is None:
        raise InstallError(f"Missing {implementation} schema at {endpoint}.")
    model = copy.deepcopy(schema)
    model.pop("id", None)
    model.update(name=name, **values)
    set_fields(model, fields)
    return api.request(endpoint, "POST", model)


def local_host(c):
    return "127.0.0.1" if c["bind"] == "0.0.0.0" else c["bind"]


def qbit_login(api, username, password):
    api.request("/api/v2/auth/login", "POST",
                {"username": username, "password": password}, form=True, raw=True)
    # qBittorrent 5.3 no longer returns the legacy "Ok." response body.
    preferences = api.request("/api/v2/app/preferences")
    if not isinstance(preferences, dict) or "listen_port" not in preferences:
        raise InstallError("qBittorrent did not return authenticated preferences.")
    return preferences


def configure_qbit(c):
    api = API(f"http://{local_host(c)}:8080")
    deadline = time.monotonic() + 240
    while True:
        try:
            qbit_login(api, c["username"], c["password"])
            break
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            if time.monotonic() >= deadline:
                raise InstallError("qBittorrent did not become ready; check VPN and Compose logs.") from None
            time.sleep(3)
    preferences = {"save_path": "/data/torrents", "temp_path": "/data/torrents/incomplete",
                   "temp_path_enabled": True, "auto_tmm_enabled": True,
                   "max_ratio_enabled": False, "max_seeding_time_enabled": False,
                   "upnp": False}
    if c["vpn"]:
        preferences["current_network_interface"] = "tun0"
        if c["vpn"]["static_port"]:
            preferences["listen_port"] = c["vpn"]["static_port"]
    api.request("/api/v2/app/setPreferences", "POST",
                {"json": json.dumps(preferences)}, form=True, raw=True)
    categories = api.request("/api/v2/torrents/categories")
    for name in ROOTS:
        if name not in categories:
            api.request("/api/v2/torrents/createCategory", "POST",
                        {"category": name, "savePath": f"/data/torrents/{name}"}, form=True, raw=True)


def flac_profile(api):
    profiles = api.request("/qualityprofile")
    existing = next((p for p in profiles if p["name"] == "FLAC Only"), None)
    if existing:
        return existing["id"]
    if not profiles:
        raise InstallError("Lidarr has no default quality profiles.")
    model = copy.deepcopy(profiles[0])
    model.pop("id", None)

    def leaves(items):
        for item in items:
            if item.get("items"):
                yield from leaves(item["items"])
            else:
                yield item

    model["items"] = list(leaves(model["items"]))
    allowed = []
    for item in model["items"]:
        item["allowed"] = item["quality"]["name"] in ("FLAC", "FLAC 24bit")
        if item["allowed"]:
            allowed.append(item["quality"]["id"])
    if len(allowed) != 2:
        raise InstallError("Lidarr's FLAC quality schema changed.")
    model.update(name="FLAC Only", upgradeAllowed=False, cutoff=allowed[-1])
    return api.request("/qualityprofile", "POST", model)["id"]


def configure_bazarr(c):
    marker = Path(c["root"]) / ".bazarr-provisioned"
    if marker.exists():
        return
    api = API(f"http://{local_host(c)}:6767", c["bazarr_key"])
    current = ready(api, "/api/system/settings")
    if not isinstance(current, dict) or not isinstance(current.get("general"), dict):
        raise InstallError("Bazarr settings schema changed.")
    desired = {"whisperai": {"endpoint": "http://whisperasr:9000", "timeout": 10800,
                             "response": 60, "pass_video_name": True, "loglevel": "INFO"}}
    for section, values in desired.items():
        if not isinstance(current.get(section), dict) or values.keys() - current[section].keys():
            raise InstallError(f"Bazarr {section} settings schema changed.")
    general = current.get("general", {})
    providers = general.get("enabled_providers")
    if not isinstance(providers, list) or not all(isinstance(item, str) for item in providers):
        raise InstallError("Bazarr enabled-provider settings schema changed.")
    desired["general"] = {"enabled_providers": [*providers, "whisperai"] if "whisperai" not in providers else providers}
    data = {}
    language = None
    profile = None
    code = c.get("subtitle_language", "none")
    if code != "none":
        languages = api.request("/api/system/languages")
        language = next((item for item in languages if item["code2"] == code), None)
        if language is None:
            raise InstallError(f"Bazarr does not support subtitle language '{code}'. "
                               "Rerun with a supported --subtitle-language code.")
        profiles = api.request("/api/system/languages/profiles")
        name = "Portable " + language["name"]
        profile = next((item for item in profiles if item["name"] == name), None)
        if profile is not None and not any(item["language"] == code for item in profile["items"]):
            raise InstallError(f"Bazarr profile '{name}' exists with a different language; refusing to overwrite.")
        if profile is None:
            profile = {"profileId": max((item["profileId"] for item in profiles), default=0) + 1,
                       "name": name, "cutoff": None, "mustContain": [], "mustNotContain": [],
                       "originalFormat": 0, "tag": None,
                       "items": [{"id": 1, "language": code, "audio_exclude": "False",
                                  "audio_only_include": "False", "hi": "False", "forced": "False"}]}
            # Bazarr replaces the whole profile collection; retain every existing profile.
            data["languages-profiles"] = json.dumps([*profiles, profile])
        data["languages-enabled"] = [item["code2"] for item in languages if item["enabled"] or item["code2"] == code]
        for kind in ("serie", "movie"):
            enabled, default = kind + "_default_enabled", kind + "_default_profile"
            if enabled not in general or default not in general:
                raise InstallError("Bazarr default language-profile settings schema changed.")
            if not general[enabled] and general[default] in ("", None):
                desired["general"].update({enabled: True, default: profile["profileId"]})
    for section, values in desired.items():
        for key, value in values.items():
            data[f"settings-{section}-{key}"] = str(value).lower() if isinstance(value, bool) else value
    api.request("/api/system/settings", "POST", data, form=True, raw=True)
    actual = api.request("/api/system/settings")
    for section, values in desired.items():
        for key, value in values.items():
            if actual.get(section, {}).get(key) != value:
                raise InstallError(f"Bazarr did not accept {section}.{key}.")
    if language is not None:
        if not any(item["code2"] == code and item["enabled"] for item in api.request("/api/system/languages")):
            raise InstallError("Bazarr did not enable the selected subtitle language.")
        saved = next((item for item in api.request("/api/system/languages/profiles")
                      if item["profileId"] == profile["profileId"]), None)
        if saved is None or saved["name"] != profile["name"] or saved["items"] != profile["items"]:
            raise InstallError("Bazarr did not retain the subtitle language profile.")
    atomic_write(marker, "Bazarr provider and language setup completed; reruns preserve UI edits.\n")


def configure_internet_archive(c, api=None):
    root = Path(c["root"])
    marker = root / ".archive-provisioned"
    if marker.exists():
        return
    api = api or API(f"http://{local_host(c)}:9696/api/v1", c["keys"]["prowlarr"])
    ready(api, "/system/status")

    def definition(model):
        return next((field.get("value") for field in model.get("fields", [])
                     if field["name"] == "definitionFile"), "")

    if any(isinstance(definition(item), str) and definition(item).startswith("internetarchive")
           for item in api.request("/indexer")):
        atomic_write(marker, "Existing Internet Archive indexer preserved.\n")
        return
    source = root / "config" / "prowlarr" / "Definitions" / "internetarchive.yml"
    target = source.parent / "Custom" / "internetarchive-portable.yml"
    deadline = time.monotonic() + 240
    while not source.is_file():
        if time.monotonic() >= deadline:
            raise InstallError("Prowlarr's Internet Archive definition is missing; inspect its logs and rerun install.")
        time.sleep(3)
    if not target.exists():
        text = source.read_text()
        if re.search(r"^  headers:", text, re.M):
            raise InstallError("Internet Archive's upstream headers changed; review the User-Agent workaround.")
        for pattern, replacement in (
                (r"^id: internetarchive$", "id: internetarchive-portable"),
                (r"^name: Internet Archive$", "name: Internet Archive (portable)"),
                (r"^search:$", 'search:\n  headers:\n    User-Agent: '
                 '["Mozilla/5.0 (X11; Linux x86_64; rv:128.0) Gecko/20100101 Firefox/128.0"]')):
            text, count = re.subn(pattern, replacement, text, flags=re.M)
            if count != 1:
                raise InstallError("Internet Archive's definition structure changed; review the User-Agent workaround.")
        owned_directory(target.parent, c)
        seed(target, text, c)
    while True:
        schema = next((item for item in api.request("/indexer/schema")
                       if definition(item) == "internetarchive-portable"), None)
        if schema is not None:
            break
        if time.monotonic() >= deadline:
            raise InstallError("Prowlarr did not load the custom Internet Archive definition; inspect its logs.")
        time.sleep(3)
    profiles = api.request("/appprofile")
    if not profiles:
        raise InstallError("Prowlarr has no application sync profiles.")
    profile = next((item for item in profiles if item["name"] == "Standard"), profiles[0])
    model = copy.deepcopy(schema)
    model.pop("id", None)
    model.update(name="Internet Archive (portable)", enable=True, priority=25,
                 appProfileId=profile["id"], tags=[])
    # Normal creation runs Prowlarr's connection test; do not force-save a broken indexer.
    created = api.request("/indexer", "POST", model)
    saved = api.request(f"/indexer/{created['id']}")
    if (definition(saved) != "internetarchive-portable" or not saved.get("enable")
            or saved.get("tags") != []):
        raise InstallError("Prowlarr did not retain the unproxied Internet Archive indexer.")
    atomic_write(marker, "Internet Archive setup completed; reruns preserve UI edits.\n")


def torrent_first(profile, usenet):
    if "preferredProtocol" in profile:
        profile.update(preferredProtocol="torrent", torrentDelay=0, usenetDelay=60,
                       enableTorrent=True, enableUsenet=usenet)
    elif isinstance(profile.get("items"), list):
        protocols = {item["protocol"]: item for item in profile["items"]}
        if not {"TorrentDownloadProtocol", "UsenetDownloadProtocol"} <= protocols.keys():
            raise InstallError("Lidarr's delay-profile protocol schema changed.")
        torrent = protocols["TorrentDownloadProtocol"]
        torrent.update(allowed=True, delay=0)
        protocols["UsenetDownloadProtocol"].update(allowed=usenet, delay=60)
        profile["items"] = [torrent, *(item for item in profile["items"] if item is not torrent)]
    else:
        raise InstallError("Unrecognized Starr delay-profile schema.")
    profile["bypassIfHighestQuality"] = False
    return profile


def provision(c):
    root = Path(c["root"])
    configure_qbit(c)
    clients = {}
    for app, (port, version, category, category_field) in APPS.items():
        # Read the persisted key rather than replacing the application's configuration.
        key = ET.parse(root / "config" / app / "config.xml").getroot().findtext("ApiKey")
        if key != c["keys"][app]:
            raise InstallError(f"{app} API key differs from installer state; reconcile credentials manually.")
        api = API(f"http://{local_host(c)}:{port}/api/{version}", key)
        ready(api, "/system/status")
        clients[app] = api
        if app == "prowlarr":
            continue
        roots = api.request("/rootfolder")
        path = f"/data/media/{category}"
        music_profile = flac_profile(api) if app == "lidarr" else None
        if not any(x["path"] == path for x in roots):
            model = {"path": path}
            if app == "lidarr":
                metadata = api.request("/metadataprofile")
                if not metadata:
                    raise InstallError("Lidarr has no default metadata profiles.")
                model.update(name="Music", defaultQualityProfileId=music_profile,
                             defaultMetadataProfileId=metadata[0]["id"],
                             defaultMonitorOption="none", defaultNewItemMonitorOption="none")
            api.request("/rootfolder", "POST", model)
        management = api.request("/config/mediamanagement")
        management["copyUsingHardlinks"] = True
        api.request("/config/mediamanagement", "PUT", management)
        if app == "lidarr":
            # Never mutate the shared inode of a seeding torrent.
            metadata = api.request("/config/metadataprovider")
            metadata["writeAudioTags"] = "no"
            api.request("/config/metadataprovider", "PUT", metadata)
            naming = api.request("/config/naming")
            naming.update(renameTracks=True, artistFolderFormat="{Artist Name}",
                          standardTrackFormat="{Album Title} ({Release Year})/{track:00} - {Track Title}",
                          multiDiscTrackFormat="{Album Title} ({Release Year})/{medium:0}-{track:00} - {Track Title}")
            api.request("/config/naming", "PUT", naming)
        ensure_model(api, "/downloadclient", "QBittorrent", "Portable qBittorrent",
                     {"host": "gluetun" if c["vpn"] else "qbittorrent", "port": 8080,
                      "username": c["username"], "password": c["password"], category_field: category},
                     enable=True, priority=1, removeCompletedDownloads=False,
                     removeFailedDownloads=True, tags=[])
        if c["sabnzbd"]:
            ensure_model(api, "/downloadclient", "Sabnzbd", "Portable SABnzbd",
                         {"host": "sabnzbd", "port": 8080, "apiKey": c["sab_key"],
                          category_field: category}, enable=False, priority=50,
                         removeCompletedDownloads=True, removeFailedDownloads=True, tags=[])
        profiles = api.request("/delayprofile")
        default = next((x for x in profiles if not x.get("tags")), None)
        if default:
            torrent_first(default, c["sabnzbd"])
            api.request(f"/delayprofile/{default['id']}", "PUT", default)
    prowlarr = clients["prowlarr"]
    for app in ("radarr", "sonarr", "lidarr"):
        ensure_model(prowlarr, "/applications", app.capitalize(), "Portable " + app.capitalize(),
                     {"prowlarrUrl": "http://prowlarr:9696",
                      "baseUrl": f"http://{app}:{APPS[app][0]}", "apiKey": c["keys"][app]},
                     syncLevel="fullSync", tags=[])
    # Tag the proxy: untagged indexers must not all be forced through FlareSolverr.
    tags = prowlarr.request("/tag")
    tag = next((x for x in tags if x["label"] == "flaresolverr"), None)
    if not tag:
        tag = prowlarr.request("/tag", "POST", {"label": "flaresolverr"})
    ensure_model(prowlarr, "/indexerproxy", "FlareSolverr", "Portable FlareSolverr",
                 {"host": "http://flaresolverr:8191", "requestTimeout": 60},
                 tags=[tag["id"]])
    configure_internet_archive(c, prowlarr)
    configure_bazarr(c)


def configure_sabnzbd(c):
    marker = Path(c["root"]) / ".sab-provisioned"
    if not c["sabnzbd"] or marker.exists():
        return
    server = c.get("usenet_server")
    if not server:
        print("SABnzbd provider setup DEFERRED: supply --usenet-server-file; Starr clients remain disabled.")
        return
    api = API(f"http://{local_host(c)}:8085")
    ready(api, "/api?mode=version&output=json")

    def call(mode, **values):
        result = api.request("/api", "POST", {"apikey": c["sab_key"], "output": "json",
                                             "mode": mode, **values}, form=True)
        if not isinstance(result, dict) or result.get("status") is False or "error" in result:
            raise InstallError("SABnzbd rejected provider setup; inspect its logs (credentials not displayed).")
        return result

    settings = {**server, "ssl": int(server["ssl"]), "ssl_verify": 3}
    result = call("config", name="test_server", **settings).get("value")
    if not isinstance(result, dict) or result.get("result") is not True:
        raise InstallError("SABnzbd's Usenet connection test failed; check credentials, TLS and connectivity.")
    servers = call("get_config", section="servers")["config"].get("servers", [])
    existing = next((item for item in servers if item["name"] == "Portable Usenet"), None)
    if existing is None:
        call("set_config", section="servers", name="Portable Usenet", displayname="Portable Usenet",
             enable=1, priority=0, **settings)
    elif existing["host"] != server["host"] or existing["username"] != server["username"]:
        raise InstallError("Portable Usenet exists with different credentials; refusing to overwrite.")
    saved = next((item for item in call("get_config", section="servers")["config"].get("servers", [])
                  if item["name"] == "Portable Usenet"), None)
    if saved is None or saved["host"] != server["host"] or not saved["enable"]:
        raise InstallError("SABnzbd did not retain an enabled Usenet server.")
    result = call("config", name="test_server", server=saved["name"],
                  **{name: saved[name] for name in ("host", "port", "username", "password", "ssl", "connections", "ssl_verify")}).get("value")
    if not isinstance(result, dict) or result.get("result") is not True:
        raise InstallError("SABnzbd's saved Usenet server failed its connection test; Starr clients remain disabled.")
    for app in ("radarr", "sonarr", "lidarr"):
        port, version, _, _ = APPS[app]
        client = API(f"http://{local_host(c)}:{port}/api/{version}", c["keys"][app])
        model = next((item for item in client.request("/downloadclient")
                      if item["name"] == "Portable SABnzbd" and item["implementation"] == "Sabnzbd"), None)
        if model is None:
            raise InstallError(f"{app} is missing its Portable SABnzbd client.")
        model["enable"] = True
        client.request("/downloadclient/test", "POST", model)
        client.request(f"/downloadclient/{model['id']}", "PUT", model)
        if not client.request(f"/downloadclient/{model['id']}")["enable"]:
            raise InstallError(f"{app} did not enable its SABnzbd client.")
    atomic_write(marker, "Usenet tested and Starr clients enabled; reruns preserve UI edits.\n")


def configure_access_hosts(c):
    direct_names = set()
    host = c.get("access_host")
    if host and not re.fullmatch(r"[\d.]+", host):
        direct_names.add(host)
    names = set(direct_names)
    if c.get("proxy"):
        names.add("qbittorrent." + c["proxy_domain"])
        names.add("gluetun" if c["vpn"] else "qbittorrent")
    owned_proxies = set(c.get("proxy_trusted_ips", []))
    if not names and not owned_proxies:
        return
    api = API(f"http://{local_host(c)}:8080")
    preferences = qbit_login(api, c["username"], c["password"])
    domains = preferences.get("web_ui_domain_list")
    if not isinstance(domains, str):
        raise InstallError("qBittorrent did not return its hostname allowlist.")
    allowed = {item.strip() for item in domains.split(";") if item.strip()}
    changes = {}
    if "*" not in allowed and not names <= allowed:
        changes["web_ui_domain_list"] = ";".join(sorted(allowed | names))
    if c.get("proxy") or owned_proxies:
        trusted = preferences.get("web_ui_reverse_proxies_list")
        if not isinstance(trusted, str):
            raise InstallError("qBittorrent did not return its trusted proxy list.")
        others = {item.strip() for item in trusted.split(";") if item.strip()} - owned_proxies
        current = set()
        if c.get("proxy"):
            container = compose(c, "ps", "--quiet", "caddy", capture_output=True, text=True).stdout.strip()
            if not container:
                raise InstallError("Caddy is not running; cannot configure qBittorrent's trusted proxy.")
            networks = json.loads(run(["docker", "inspect", "--format", "{{json .NetworkSettings.Networks}}", container],
                                      capture_output=True, text=True).stdout)
            current = {str(ipaddress.IPv4Address(network["IPAddress"])) for network in networks.values() if network.get("IPAddress")}
            if not current:
                raise InstallError("Caddy has no Docker IPv4 address.")
            changes["web_ui_reverse_proxy_enabled"] = True
        changes["web_ui_reverse_proxies_list"] = ";".join(sorted(others | current))
        c["proxy_trusted_ips"] = sorted(current - others)
    if changes:
        api.request("/api/v2/app/setPreferences", "POST", {"json": json.dumps(changes)}, form=True, raw=True)
        actual = api.request("/api/v2/app/preferences")
        if any(actual.get(name) != value for name, value in changes.items()):
            raise InstallError("qBittorrent did not retain its proxy/access settings.")
        if "*" not in allowed and not names <= set(actual["web_ui_domain_list"].split(";")):
            raise InstallError("qBittorrent did not retain its access hostnames.")
    if c["sabnzbd"]:
        names = direct_names | ({"sabnzbd." + c["proxy_domain"]} if c.get("proxy") else set())
        api = API(f"http://{local_host(c)}:8085")
        ready(api, "/api?mode=version&output=json")
        fields = {"apikey": c["sab_key"], "output": "json", "section": "misc", "keyword": "host_whitelist"}
        result = api.request("/api", "POST", {**fields, "mode": "get_config"}, form=True)
        allowed = result.get("config", {}).get("misc", {}).get("host_whitelist")
        if not isinstance(allowed, list) or any(not isinstance(name, str) for name in allowed):
            raise InstallError("SABnzbd did not return its hostname allowlist.")
        if not names <= set(allowed):
            expected = set(allowed) | names
            api.request("/api", "POST", {**fields, "mode": "set_config", "value": ", ".join(sorted(expected))}, form=True)
            result = api.request("/api", "POST", {**fields, "mode": "get_config"}, form=True)
            if not expected <= set(result.get("config", {}).get("misc", {}).get("host_whitelist", [])):
                raise InstallError("SABnzbd did not retain its access hostnames.")


def jellyfin_login(c):
    api = API(f"http://{local_host(c)}:8096", headers={
        "Authorization": 'MediaBrowser Client="Starr installer", Device="Installer", DeviceId="starr-installer", Version="1"'})
    session = api.request("/Users/AuthenticateByName", "POST",
                          {"Username": c["username"], "Pw": c["password"]})
    if not session.get("AccessToken") or not session.get("User", {}).get("Policy", {}).get("IsAdministrator"):
        raise InstallError("Jellyfin did not return an authenticated administrator.")
    api.headers["Authorization"] += ', Token="' + session["AccessToken"] + '"'
    return api


def configure_jellyfin(c):
    root = Path(c["root"])
    marker, managed = root / ".jellyfin-provisioned", root / ".jellyfin-managed"
    if not c["jellyfin"] or marker.exists():
        return
    api = API(f"http://{local_host(c)}:8096")
    info = ready(api, "/System/Info/Public")
    if info["StartupWizardCompleted"] and not managed.exists():
        atomic_write(marker, "Existing Jellyfin setup preserved; use its existing administrator account.\n")
        return
    if not info["StartupWizardCompleted"]:
        user = api.request("/Startup/User")
        if user["Name"] != c["username"] or not managed.exists():
            atomic_write(managed, "Installer owns the initial Jellyfin administrator; credentials are in state.json.\n")
            api.request("/Startup/User", "POST", {"Name": c["username"], "Password": c["password"]})
    api = jellyfin_login(c)
    libraries = api.request("/Library/VirtualFolders")
    for name, category, kind in (("Movies", "movies", "movies"), ("TV", "tv", "tvshows"), ("Music", "music", "music")):
        path = f"/data/media/{category}"
        if any(path in item.get("Locations", []) for item in libraries):
            continue
        if any(item["Name"] == name for item in libraries):
            raise InstallError(f"Jellyfin library {name} exists at a different path; refusing to overwrite.")
        query = urllib.parse.urlencode({"name": name, "collectionType": kind, "refreshLibrary": "false"})
        api.request("/Library/VirtualFolders?" + query, "POST", {
            "LibraryOptions": {"PathInfos": [{"Path": path}], "EnableRealtimeMonitor": True,
                               "SaveLocalMetadata": False}})
    actual = api.request("/Library/VirtualFolders")
    if any(not any(f"/data/media/{category}" in item.get("Locations", []) for item in actual) for category in ROOTS):
        raise InstallError("Jellyfin did not retain the configured libraries.")
    if not info["StartupWizardCompleted"]:
        api.request("/Startup/RemoteAccess", "POST", {"EnableRemoteAccess": True, "EnableAutomaticPortMapping": False})
        api.request("/Startup/Complete", "POST")
    if not api.request("/System/Info/Public")["StartupWizardCompleted"]:
        raise InstallError("Jellyfin did not complete its startup wizard.")
    atomic_write(marker, "Jellyfin administrator and libraries configured; reruns preserve UI edits.\n")


def plex_token(c):
    preferences = Path(c["root"]) / "config" / "plex" / "Library" / "Application Support" / "Plex Media Server" / "Preferences.xml"
    return ET.parse(preferences).getroot().get("PlexOnlineToken", "") if preferences.exists() else ""


def configure_plex(c):
    marker = Path(c["root"]) / ".plex-provisioned"
    if not c["plex"] or marker.exists():
        return
    token = plex_token(c)
    if not token:
        if c.get("plex_claim"):
            deadline = time.monotonic() + 120
            while not token and time.monotonic() < deadline:
                time.sleep(3)
                token = plex_token(c)
            if not token:
                raise InstallError("Plex claim did not complete. Generate a fresh claim token and rerun with --plex-claim-file.")
        else:
            print("Plex setup DEFERRED: claim it in the UI or supply --plex-claim-file, then rerun install.")
            return
    api = API(f"http://{local_host(c)}:32400", headers={"X-Plex-Token": token, "Accept": "application/json"})
    ready(api, "/")
    libraries = api.request("/library/sections")["MediaContainer"].get("Directory", [])
    for name, category, kind, agent, scanner in (
            ("Movies", "movies", "movie", "tv.plex.agents.movie", "Plex Movie"),
            ("TV", "tv", "show", "tv.plex.agents.series", "Plex TV Series"),
            ("Music", "music", "artist", "tv.plex.agents.music", "Plex Music")):
        path = f"/data/media/{category}"
        if any(any(location["path"] == path for location in item.get("Location", [])) for item in libraries):
            continue
        if any(item["title"] == name for item in libraries):
            raise InstallError(f"Plex library {name} exists at a different path; refusing to overwrite.")
        query = urllib.parse.urlencode({"name": name, "type": kind, "agent": agent,
                                        "scanner": scanner, "language": "en-US", "location": path})
        api.request("/library/sections?" + query, "POST", raw=True)
    actual = api.request("/library/sections")["MediaContainer"].get("Directory", [])
    if any(not any(any(location["path"] == f"/data/media/{category}" for location in item.get("Location", []))
                       for item in actual) for category in ROOTS):
        raise InstallError("Plex did not retain the configured libraries.")
    atomic_write(marker, "Plex libraries configured; reruns preserve UI edits.\n")


def configure_media_connections(c):
    root = Path(c["root"])
    for server in ("plex", "jellyfin"):
        marker = root / f".{server}-connections-provisioned"
        if not c[server] or marker.exists():
            continue
        if server == "plex":
            token = plex_token(c)
            if not token:
                continue
            implementation, port, credentials = "PlexServer", 32400, {"authToken": token}
        else:
            if not (root / ".jellyfin-managed").exists():
                print("Jellyfin library-refresh connections DEFERRED: configure Starr Connect with the existing administrator.")
                continue
            api = jellyfin_login(c)
            keys = api.request("/Auth/Keys")["Items"]
            key = next((item for item in keys if item["AppName"] == "Portable Starr"
                        and item.get("AccessToken") and not item.get("DateRevoked")), None)
            if key is None:
                api.request("/Auth/Keys?app=Portable%20Starr", "POST")
                key = next((item for item in api.request("/Auth/Keys")["Items"]
                            if item["AppName"] == "Portable Starr" and item.get("AccessToken")
                            and not item.get("DateRevoked")), None)
            if key is None:
                raise InstallError("Jellyfin did not retain its Starr integration API key.")
            implementation, port, credentials = "MediaBrowser", 8096, {"apiKey": key["AccessToken"], "notify": False}
        for app in ("radarr", "sonarr", "lidarr"):
            app_port, version, _, _ = APPS[app]
            api = API(f"http://{local_host(c)}:{app_port}/api/{version}", c["keys"][app])
            imported = "onReleaseImport" if app == "lidarr" else "onDownload"
            ensure_model(api, "/notification", implementation, "Portable " + server.capitalize(),
                         {"host": server, "port": port, "useSsl": False, "urlBase": "",
                          "updateLibrary": True, **credentials},
                         **{imported: True, "onUpgrade": True, "onRename": True, "tags": []})
        atomic_write(marker, "Starr library-refresh connections configured; reruns preserve UI edits.\n")


def seerr_connections(c):
    result = {}
    for app, category in (("radarr", "movies"), ("sonarr", "tv")):
        port = APPS[app][0]
        api = API(f"http://{local_host(c)}:{port}/api/v3", c["keys"][app])
        profiles = api.request("/qualityprofile")
        name = c.get("request_profile") or ("HD - 1080p" if c["sync_profiles"] else "HD-1080p")
        profile = next((item for item in profiles if item["name"] == name), None)
        if profile is None:
            raise InstallError(f"{app} has no '{name}' profile; apply Recyclarr or supply --request-profile.")
        model = {"name": "Portable " + app.capitalize(), "hostname": app, "port": port,
                 "apiKey": c["keys"][app], "useSsl": False, "baseUrl": "",
                 "activeProfileId": profile["id"], "activeProfileName": name,
                 "activeDirectory": f"/data/media/{category}", "tags": [], "is4k": False,
                 "isDefault": True, "syncEnabled": True, "preventSearch": False,
                 "tagRequests": False, "overrideRule": []}
        if app == "radarr":
            model["minimumAvailability"] = "released"
        else:
            model.update(seriesType="standard", animeSeriesType="anime",
                         enableSeasonFolders=True, monitorNewItems="all")
            anime = next((item for item in profiles if item["name"] == name + " Anime"), None)
            if anime:
                model.update(activeAnimeProfileId=anime["id"], activeAnimeProfileName=anime["name"],
                             activeAnimeDirectory=f"/data/media/{category}", animeTags=[])
        result[app] = model
    return result


def configure_seerr(c):
    root = Path(c["root"])
    marker = root / ".seerr-provisioned"
    if not c["seerr"]:
        return
    if marker.exists():
        compose(c, "up", "-d", "seerr")
        return
    path = root / "config" / "seerr" / "settings.json"
    if not path.exists():
        connections = seerr_connections(c)
        seed(path, json.dumps({"main": {"apiKey": secrets.token_hex(32), "mediaServerType": 4},
                               **{app: [dict(model, id=0)] for app, model in connections.items()}}) + "\n", c)
    compose(c, "up", "-d", "seerr")
    api = API(f"http://{local_host(c)}:5055/api/v1")
    ready(api, "/status?checkUpdateAvailable=false")
    settings = json.loads(path.read_text())
    api.key = settings["main"]["apiKey"]
    # API keys act as the first administrator, which must first sign in with a media-server account.
    selected = c.get("seerr_media_server") or ("plex" if c["plex"] else "jellyfin")
    public = api.request("/settings/public")
    if public.get("initialized"):
        atomic_write(marker, "Existing completed Seerr setup preserved.\n")
        return
    if public.get("mediaServerType") == 4:
        if selected == "plex":
            token = plex_token(c) if c["plex"] else ""
            if not token:
                print("Seerr media-server setup DEFERRED: Plex sign-in is required; Starr connections are preconfigured.")
                return
            api.request("/auth/plex", "POST", {"authToken": token})
        else:
            if not (root / ".jellyfin-managed").exists():
                print("Seerr media-server setup DEFERRED: sign in with your existing Jellyfin administrator, then rerun.")
                return
            api.request("/auth/jellyfin", "POST", {"username": c["username"], "password": c["password"],
                        "hostname": "jellyfin", "port": 8096, "useSsl": False, "urlBase": "", "serverType": 2})
    actual_type = api.request("/settings/public")["mediaServerType"]
    if actual_type != (1 if selected == "plex" else 2):
        raise InstallError("Seerr already uses a different media server; refusing to switch it.")
    api.request("/auth/me")
    for app, model in seerr_connections(c).items():
        existing = api.request("/settings/" + app)
        if not existing:
            api.request("/settings/" + app, "POST", model)
        for item in api.request("/settings/" + app):
            tested = api.request("/settings/" + app + "/test", "POST", item)
            if not any(profile["id"] == item["activeProfileId"] for profile in tested["profiles"]) or not any(
                    folder["path"] == item["activeDirectory"] for folder in tested["rootFolders"]):
                raise InstallError(f"Seerr's {app} connection has an invalid profile or library root.")
    if selected == "plex":
        if not api.request("/settings/plex")["ip"]:
            api.request("/settings/plex", "POST", {"ip": "plex", "port": 32400, "useSsl": False})
        server = API(f"http://{local_host(c)}:32400", headers={
            "X-Plex-Token": plex_token(c), "Accept": "application/json"})
        folders = server.request("/library/sections")["MediaContainer"].get("Directory", [])
        library_ids = {str(item["key"]) for item in folders if any(
            location["path"] in ("/data/media/movies", "/data/media/tv") for location in item.get("Location", []))}
    else:
        server_settings = api.request("/settings/jellyfin")
        server = API(f"http://{local_host(c)}:8096", headers={"Authorization":
            'MediaBrowser Client="Starr installer", Device="Installer", DeviceId="starr-installer", Version="1", '
            f'Token="{server_settings["apiKey"]}"'})
        library_ids = {str(item["ItemId"]) for item in server.request("/Library/VirtualFolders") if any(
            path in ("/data/media/movies", "/data/media/tv") for path in item.get("Locations", []))}
    if len(library_ids) != 2:
        raise InstallError("Seerr requires distinct movie and TV libraries at the configured paths.")
    libraries = api.request(f"/settings/{selected}/library/sync", "POST")
    for library in libraries:
        if str(library["id"]) in library_ids and not library["enabled"]:
            api.request(f"/settings/{selected}/library/{library['id']}", "PUT", {"enabled": True})
    enabled = api.request(f"/settings/{selected}/library")
    if not library_ids <= {str(item["id"]) for item in enabled if item["enabled"]}:
        raise InstallError("Seerr did not enable the movie and TV libraries.")
    api.request("/settings/initialize", "POST")
    if not api.request("/settings/public")["initialized"]:
        raise InstallError("Seerr did not complete setup.")
    atomic_write(marker, "Seerr connections and libraries configured; reruns preserve UI edits.\n")


def compose(c, *args, **kwargs):
    return run(["docker", "compose", "--project-directory", c["root"],
                "-f", Path(c["root"]) / "compose.json", *args], **kwargs)


def active_services(c):
    return [name for name, service in compose_config(c)["services"].items()
            if not service.get("profiles")]


def start_services(c, names):
    services = compose_config(c)["services"]
    consumers = [name for name in names if services[name].get("network_mode", "").startswith("service:")]
    # Stop namespace consumers first, then explicitly recreate after the gateways.
    # This avoids stale service:gluetun namespaces after an image update.
    if consumers:
        compose(c, "stop", *consumers)
    compose(c, "up", "-d", "--wait", "--wait-timeout", "240",
            *[name for name in names if name not in consumers])
    if consumers:
        compose(c, "up", "-d", "--force-recreate", "--wait", "--wait-timeout", "240", *consumers)


def update(c):
    # Pull before stopping anything; a registry outage must not take the stack down.
    names = active_services(c)
    managed_proxy = bool(c.get("proxy") or c.get("proxy_trusted_ips"))
    compose(c, "pull", *names)
    start_services(c, names)
    configure_access_hosts(c)
    if managed_proxy:
        write_json(Path(c["root"]) / "state.json", c)


def install_cron(c):
    if not shutil.which("crontab"):
        if not c["auto_update"]:
            return
        raise InstallError("crontab is missing. Install/enable your distro's cron daemon, then rerun.")
    marker = "# portable-starr:" + hashlib.sha256(c["root"].encode()).hexdigest()[:12]
    result = subprocess.run(["crontab", "-l"], capture_output=True, text=True,
                            env={**os.environ, "LC_ALL": "C"})
    if result.returncode and not (result.returncode == 1 and "no crontab" in result.stderr.lower()):
        raise InstallError("Cannot read root crontab; existing jobs were not changed.")
    root = Path(c["root"])
    command = shlex.join([sys.executable, str(root / "bin" / "starr.py"), "update",
                          "--root", str(root)])
    line = f"0 4 * * * {command} >> {shlex.quote(str(root / 'update.log'))} 2>&1 {marker}"
    lines = [x for x in result.stdout.splitlines() if marker not in x]
    if not c["auto_update"]:
        if lines != result.stdout.splitlines():
            run(["crontab", "-"], input="\n".join(lines) + "\n", text=True)
        return
    # Do not depend on the minimal PATH supplied by cron.
    job = "PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:/snap/bin " + command
    line = line.replace(command, job, 1)
    run(["crontab", "-"], input="\n".join(lines + [line]) + "\n", text=True)


def port_sync(client):
    credentials = json.loads(Path("/credentials.json").read_text())
    api = API("http://127.0.0.1:" + ("8080" if client == "qbittorrent" else "5030"),
              credentials["slskd_key"] if client == "slskd" else None)
    while True:
        try:
            text = Path("/port/forwarded_port").read_text().strip()
            port = int(text)
            if not 1 <= port <= 65535:
                raise InstallError("Invalid forwarded port.")
            if client == "qbittorrent":
                qbit_login(api, credentials["username"], credentials["password"])
                api.request("/api/v2/app/setPreferences", "POST",
                            {"json": json.dumps({"listen_port": port})}, form=True, raw=True)
                if api.request("/api/v2/app/preferences")["listen_port"] != port:
                    raise InstallError("qBittorrent did not accept forwarded port.")
            else:
                api.request("/api/v0/options", "PATCH", {"soulseek": {"listenPort": port}}, raw=True)
                if api.request("/api/v0/options")["soulseek"]["listenPort"] != port:
                    raise InstallError("slskd did not accept forwarded port.")
            print(f"{client}: synchronized forwarded peer port {port}", flush=True)
        except (OSError, ValueError, InstallError) as exc:
            print(f"ERROR: {client} port synchronization: {exc}", file=sys.stderr, flush=True)
        time.sleep(30)


def summary(c):
    print("\nStack provisioned. No private indexer credentials, paid accounts, libraries or existing media were copied.")
    for link in access_links(c):
        print(f"  {link['label']}: {link['proxy'] or link['direct'] or 'supply --access-host for a direct URL'}")
        if link["proxy"] and link["direct"]:
            print(f"    direct: {link['direct']}")
    print(f"Icon link page: {c['root']}/access/index.html")
    if c.get("proxy"):
        suffix = "" if c["proxy_port"] == 80 else ":" + str(c["proxy_port"])
        print(f"Local dashboard: http://{c['proxy_domain']}{suffix}/")
        print(f"Configure client DNS/hosts entries from {c['root']}/access/hosts.txt; Caddy does not provide DNS.")
        print("Local proxy uses HTTP, not automatic HTTPS. Keep it private; app authentication is still required.")
    print(f"\nPrivate credentials: {c['root']}/state.json (root-only).")
    print("Create the Starr admin accounts before exposing web UIs.")
    print("Prowlarr: Internet Archive configured without FlareSolverr; existing entries/edits are preserved.")
    print("Archive contains mixed-rights content: download only public-domain, openly licensed or otherwise authorized items.")
    print("Add other authorized indexers; tag only challenged indexers 'flaresolverr'.")
    print("Bazarr: Whisper enabled at http://whisperasr:9000; CPU transcription can be slow.")
    print("Bazarr subtitle profile: " + (
        c["subtitle_language"] + " (defaults assigned where not already configured)."
        if c.get("subtitle_language", "none") != "none" else "choose languages/profiles in the UI."))
    print("Configure Bazarr authentication and any additional subtitle-provider accounts.")
    if c["sabnzbd"]:
        print("SABnzbd: " + ("provider tested; clients enabled in Radarr/Sonarr/Lidarr."
              if (Path(c["root"]) / ".sab-provisioned").exists() else
              "provider setup DEFERRED; supply --usenet-server-file and rerun. Clients remain disabled."))
    if c["soulseek"]:
        print("Soulseek: " + ("started with your supplied account; Soularr polls Lidarr."
                             if c["soulseek_username"] else
                             "DEFERRED. Add network username/password to root-only state.json, "
                             "then rerun install to start slskd + Soularr."))
    for app in ("plex", "jellyfin", "seerr"):
        if c[app]:
            print(app + ": " + ("setup completed or existing setup preserved."
                  if (Path(c["root"]) / f".{app}-provisioned").exists() else
                  "account setup DEFERRED; complete sign-in and rerun install."))
    if (Path(c["root"]) / ".jellyfin-managed").exists():
        print("Jellyfin administrator: username/password from state.json; Seerr uses that account when Jellyfin is selected.")
    print("Keep torrent ratio/time limits aligned with your tracker's rules; no automatic torrent deletion.")
    print("Recyclarr sync: " + ("enabled daily." if c["sync_profiles"] else "not applied; review before syncing."))
    print("Image auto-update: " + ("daily at 04:00 via root cron; ensure cron/crond is enabled."
                                   if c["auto_update"] else "off."))


def argument_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", nargs="?", choices=("install", "update", "port-sync"), default="install")
    parser.add_argument("--root", help="Persistent installation directory (default: /opt/starr)")
    parser.add_argument("--client", choices=("qbittorrent", "slskd"))
    parser.add_argument("-y", "--yes", action="store_true", help="Approve setup changes (not direct peer traffic)")
    parser.add_argument("--non-interactive", "--noninteractive", action="store_true",
                        help="Use supplied options/defaults, never prompt; requires --yes")
    parser.add_argument("--allow-direct", action="store_true", help="Explicitly allow peer traffic without a VPN")
    parser.add_argument("--content", help="Content path, one filesystem for downloads and library (default: /srv/media)")
    parser.add_argument("--uid", type=int, help="Non-root media UID (default: invoking sudo user, or 1000)")
    parser.add_argument("--gid", type=int, help="Media GID (default: invoking sudo user, or 1000)")
    parser.add_argument("--timezone", help="IANA timezone (default: Etc/UTC)")
    parser.add_argument("--bind", help="Web UI bind IPv4 (default: 127.0.0.1)")
    parser.add_argument("--access-host", help="Reachable server IPv4/hostname for direct links; defaults to bind address")
    parser.add_argument("--proxy", action=argparse.BooleanOptionalAction, default=None,
                        help="Optional Caddy local HTTP proxy (off by default)")
    parser.add_argument("--proxy-domain", help="Local hostname suffix below home.arpa (default: starr.home.arpa)")
    parser.add_argument("--proxy-port", type=int, help="Proxy HTTP port (default: 80; use 8088 for unprivileged SSH forwarding)")
    parser.add_argument("--username", help="qBittorrent/slskd and fresh Jellyfin administrator username (default: media)")
    parser.add_argument("--subtitle-language", type=str.lower,
                        help="Bazarr two-letter language code, or none to choose later (default: en)")
    for name in (*OPTIONAL, "auto_update", "sync_profiles"):
        parser.add_argument("--" + name.replace("_", "-"), action=argparse.BooleanOptionalAction,
                            default=None, help=f"Enable/disable {name.replace('_', ' ')}")
    parser.add_argument("--soulseek-username", help="Soulseek network username; omit to defer")
    parser.add_argument("--soulseek-password-file", help="Read Soulseek network password from a private file")
    parser.add_argument("--seerr-media-server", choices=("plex", "jellyfin"),
                        help="Selected installed media server for Seerr (default: Plex when both are installed)")
    parser.add_argument("--request-profile", help="Seerr quality profile name (default: HD - 1080p, or native HD-1080p without sync)")
    parser.add_argument("--plex-claim-file", help="Private file containing a fresh claim token from https://plex.tv/claim")
    parser.add_argument("--usenet-server-file", help="Private JSON containing Usenet host, username, password, port, ssl, connections")
    for prefix, label in (("vpn", "qBittorrent"), ("soulseek-vpn", "Soulseek")):
        group = parser.add_argument_group(label + " VPN")
        group.add_argument("--" + prefix, choices=("none", "proton", "wireguard", "openvpn"),
                           help="VPN mode (default in noninteractive mode: none)")
        group.add_argument("--" + prefix + "-profile", help="Local exported WireGuard/OpenVPN profile")
        group.add_argument("--" + prefix + "-country", help="Proton country (default: Netherlands)")
        group.add_argument("--" + prefix + "-port-forwarding", action=argparse.BooleanOptionalAction,
                           default=None, help="Proton NAT-PMP (default: enabled)")
        group.add_argument("--" + prefix + "-static-port", type=int, help="Other providers' forwarded port (default: 0)")
        group.add_argument("--" + prefix + "-username", help="OpenVPN-specific username")
        group.add_argument("--" + prefix + "-password-file", help="Private file containing OpenVPN password")
    return parser


def cli_options(args):
    controls = {"command", "root", "client", "yes", "non_interactive", "allow_direct"}
    return {k: v for k, v in vars(args).items() if k not in controls and v is not None}


def validate_vpn_flags(args, prefix, mode):
    supplied = {name: value for name, value in cli_options(args).items() if name.startswith(prefix + "_")}
    flag = "--" + prefix.replace("_", "-")
    if mode == "none" and supplied:
        raise InstallError(f"{flag}=none cannot use VPN-specific options.")
    if mode != "proton" and any(prefix + suffix in supplied for suffix in ("_country", "_port_forwarding")):
        raise InstallError(f"{flag}-country and dynamic port forwarding apply only to Proton.")
    if mode != "openvpn" and any(prefix + suffix in supplied for suffix in ("_username", "_password_file")):
        raise InstallError(f"{flag}-username and password files apply only to OpenVPN.")
    if getattr(args, prefix + "_port_forwarding") is True and prefix + "_static_port" in supplied:
        raise InstallError("A static peer port cannot be combined with dynamic port forwarding.")


def validate_options(parser, args):
    if args.non_interactive and not args.yes:
        parser.error("--non-interactive requires --yes; add --allow-direct if not using a VPN")
    if args.client and args.command != "port-sync":
        parser.error("--client applies only to port-sync")
    if args.command != "install" and cli_options(args):
        parser.error("Setup options apply to install, not update/port-sync")
    if (args.proxy is False or (args.non_interactive and args.proxy is not True)) and (
            args.proxy_domain is not None or args.proxy_port is not None):
        parser.error("--proxy-domain and --proxy-port require --proxy")
    try:
        validate_access({"access_host": args.access_host, "proxy": args.proxy,
                         "proxy_domain": args.proxy_domain or "starr.home.arpa",
                         "proxy_port": args.proxy_port if args.proxy_port is not None else 80})
    except (InstallError, ValueError) as error:
        parser.error(str(error))
    if args.subtitle_language is not None and args.subtitle_language != "none" and not re.fullmatch(
            "[a-z]{2}", args.subtitle_language):
        parser.error("--subtitle-language must be a two-letter Bazarr code, or none")
    for app, names in (("seerr", ("seerr_media_server", "request_profile")),
                       ("plex", ("plex_claim_file",)), ("sabnzbd", ("usenet_server_file",))):
        if (getattr(args, app) is False or (args.non_interactive and getattr(args, app) is not True)) and any(
                getattr(args, name) is not None for name in names):
            parser.error(f"{app} setup options require --{app}")
    if args.seerr and args.non_interactive:
        selected = args.seerr_media_server or ("plex" if args.plex else "jellyfin")
        if getattr(args, selected) is not True:
            parser.error("Seerr requires its selected --plex or --jellyfin media server")
    for prefix in ("vpn", "soulseek_vpn"):
        mode = getattr(args, prefix)
        supplied = {name: value for name, value in cli_options(args).items() if name.startswith(prefix + "_")}
        if mode is not None:
            try:
                validate_vpn_flags(args, prefix, mode)
            except InstallError as error:
                parser.error(str(error))
        if args.non_interactive and supplied and mode is None:
            parser.error("--" + prefix.replace("_", "-") + " is required with VPN-specific options")
        if args.non_interactive and mode not in (None, "none") and not getattr(args, prefix + "_profile"):
            parser.error("--" + prefix.replace("_", "-") + "-profile is required")
    if args.soulseek is False or (args.non_interactive and args.soulseek is not True):
        if any(name.startswith("soulseek_") for name in cli_options(args)):
            parser.error("Soulseek account/VPN options require --soulseek")


def saved_options(c, args):
    for name, value in cli_options(args).items():
        pending = {"subtitle_language": "bazarr", "usenet_server_file": "sab", "plex_claim_file": "plex",
                   "request_profile": "seerr", "seerr_media_server": "seerr"}
        if (name in pending and not (Path(c["root"]) / f".{pending[name]}-provisioned").exists()
                and (name != "request_profile" or not (Path(c["root"]) / "config" / "seerr" / "settings.json").exists())):
            c[name] = value
            c.setdefault("cli_options", {})[name] = value
        elif name in ("bind", "auto_update", "sync_profiles", "access_host", "proxy", "proxy_domain", "proxy_port"):
            if name == "bind":
                if ipaddress.ip_address(value).version != 4:
                    raise InstallError("The web bind address must be IPv4.")
                if value != c["bind"] and value != "127.0.0.1":
                    if not consent(args, "Expose web UIs on the selected address"):
                        raise InstallError("Web UI exposure not approved.")
            c[name] = value
        elif c.get("cli_options", {}).get(name) != value and c.get(name) != value:
            if name in ("vpn", "soulseek_vpn") and value == "none" and c[name] is None:
                continue
            raise InstallError(f"--{name.replace('_', '-')} differs from saved setup. "
                               "Reruns preserve choices; reconfiguration of app/VPN credentials is manual.")


def main(argv=None):
    parser = argument_parser()
    args = parser.parse_args(argv)
    validate_options(parser, args)
    if args.command == "port-sync":
        if not args.client:
            parser.error("port-sync requires --client")
        port_sync(args.client)
        return
    if sys.platform != "linux" or os.geteuid() != 0:
        raise InstallError("Run on Linux with sudo/root. Docker Engine + Compose v2 and Python 3.10+ are required.")
    root = absolute_path(args.root or ("/opt/starr" if args.non_interactive else
                                      ask("Installation/config directory", "/opt/starr")))
    if root.exists() and not (root / "state.json").exists():
        if any(path.name != ".lock" for path in root.iterdir()):
            raise InstallError("Choose an empty installation directory; existing installs are not adopted.")
    root.mkdir(parents=True, exist_ok=True)
    root.chmod(0o700)
    # A single lock also serializes cron, manual update, and installer reruns.
    import fcntl
    with (root / ".lock").open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise InstallError("Another installer/update is already running.") from None
        docker_preflight()
        state = root / "state.json"
        if state.exists():
            c = json.loads(state.read_text())
            if c["version"] != 1 or c["root"] != str(root):
                raise InstallError("Unsupported or relocated installer state.")
            saved_options(c, args)
            print("Reusing saved choices; preserving app databases and existing API records.")
        elif args.command == "update":
            raise InstallError("No saved installation at this path.")
        else:
            if any(root.iterdir()) and any(p.name != ".lock" for p in root.iterdir()):
                raise InstallError("Choose an empty installation directory; existing installs are not adopted.")
            c = collect(root, args)
            c["cli_options"] = cli_options(args)
        if not c.get("proxy") and (args.proxy_domain is not None or args.proxy_port is not None):
            raise InstallError("Proxy domain/port settings require --proxy.")
        validate_access(c)
        validate_paths(root, absolute_path(c["content"]))
        if args.command == "install" and c["seerr"]:
            selected = c.get("seerr_media_server") or ("plex" if c["plex"] else "jellyfin")
            if not c.get(selected):
                raise InstallError("Seerr requires its selected Plex or Jellyfin server to be installed.")
        if args.command == "update":
            update(c)
            return
        write_json(state, c)
        prepare_vpn(c)
        write_json(state, c)
        if (c["vpn"] or c["soulseek_vpn"]) and not Path("/dev/net/tun").exists():
            raise InstallError("VPN requires /dev/net/tun; enable TUN on the host/VM first.")
        if c["auto_update"] and not shutil.which("crontab"):
            raise InstallError("Install/enable your distribution's cron package, or set auto_update=false in state.json.")
        previous = root / "compose.json"
        if not c.get("proxy") and previous.exists() and "caddy" in json.loads(previous.read_text())["services"]:
            compose(c, "rm", "--stop", "--force", "caddy")
        initialize(c, Path(__file__).resolve().parents[2])
        compose(c, "config", "--quiet")
        if not c["sync_profiles"]:
            compose(c, "stop", "recyclarr")
        names = active_services(c)
        # Do not let the scheduled Recyclarr process race first-time API provisioning.
        compose(c, "pull", *names)
        if c["plex"] and c.get("plex_claim_file") and not (root / ".plex-provisioned").exists() and not plex_token(c):
            c["plex_claim"] = password_file(c["plex_claim_file"])
            if not c["plex_claim"].startswith("claim-"):
                raise InstallError("Plex claim file must contain a claim token, not an account token.")
            write_json(root / "compose.json", compose_config(c))
        start_services(c, [x for x in names if x not in ("recyclarr", "soularr", "seerr")])
        marker = root / ".provisioned"
        if not marker.exists():
            provision(c)
            atomic_write(marker, "API provisioning completed.\n")
        else:
            configure_internet_archive(c)
            configure_bazarr(c)
        if c["sabnzbd"] and not (root / ".sab-provisioned").exists():
            c["usenet_server"] = usenet_server_file(c.get("usenet_server_file")) or c.get("usenet_server")
        write_json(state, {name: value for name, value in c.items() if name != "plex_claim"})
        configure_sabnzbd(c)
        configure_access_hosts(c)
        write_json(state, {name: value for name, value in c.items() if name != "plex_claim"})
        configure_jellyfin(c)
        configure_plex(c)
        configure_media_connections(c)
        if c["sync_profiles"]:
            compose(c, "run", "--rm", "recyclarr", "sync")
            compose(c, "up", "-d", "recyclarr")
        configure_seerr(c)
        c.pop("plex_claim", None)
        write_json(root / "compose.json", compose_config(c))
        if c["soulseek"] and c["soulseek_username"]:
            compose(c, "up", "-d", "soularr")
        install_cron(c)
        summary(c)


if __name__ == "__main__":
    try:
        main()
    except (InstallError, OSError, ValueError, ET.ParseError, subprocess.CalledProcessError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        sys.exit(1)
