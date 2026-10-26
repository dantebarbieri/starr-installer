# Portable Starr installer

A fresh-machine, interactive Docker setup independent of NixOS, `danteb.com`,
the production reverse proxy, and the original homeserver repository. Requires **Linux,
Python 3.10+, Docker Engine and the Docker Compose v2 plugin**. It does not
install Docker or change your host firewall, router, mounts or GPU drivers.
Use Docker's [distribution-specific instructions](https://docs.docker.com/engine/install/)
first. If you enable image updates, install **and enable** your distribution's
cron daemon (`cron`/`crond`) and its `crontab` command first.
Docker must use a local, rootful Unix-socket Engine; remote contexts and
rootless Docker are rejected rather than installing against the wrong host.

## Install

### Guided setup

After the download service is deployed, type this one-line command on the server:

```sh
curl -fsSL https://starr.danteb.com/i | sudo sh
```

Requires curl and the prerequisites above. Omit `sudo` if already root. Review
the source before running downloaded code as root. `/i` serves the same wrapper
as `/install.sh`, without a redirect. The wrapper starts Python; it does not
install Python or Docker. Setup asks its questions in the terminal, including
storage, applications, VPN and updates. Browser settings do not affect this
short command; use the generated custom command to apply them.

### Configure in your browser

Once deployed, **[starr.danteb.com](https://starr.danteb.com)** is a static setup
guide and command builder. Choose storage/ownership, optional apps, separate VPN
profiles, access settings and maintenance options. It generates a shell-escaped
command for sh, Bash or Python, either saved-first or piped, with guided or
unattended setup. No Docker access or installer execution happens in the browser.
Run the resulting command on your Linux server.

The timezone dropdown defaults to **Automatic**, using this browser's timezone.
The generated command includes the detected IANA name explicitly; this does not
detect or change the server's timezone. Select UTC, a listed region/city, or a
custom IANA name instead if needed. Cron image updates still use the host's local
clock. Python is the default entry point. The sh/Bash wrappers also require
Python 3.10+; they do not install it.

Both generated download styles execute immediately. Downloading to a file avoids
executing a partially downloaded wrapper, but does not pause for review. Use the
source links or download and inspect the files separately before executing.

All configuration stays in the current tab: there is no settings API, analytics,
local storage, share URL or account. Enter **server-side file paths**, never
passwords, API keys or VPN profile contents. The guide links to Docker/TRaSH/VPN
documentation and includes SSH tunneling, first-login and recovery instructions.
Input checks catch common mistakes; the installer remains authoritative for real
filesystem permissions, mounts, VPN profiles and Docker compatibility.

### Terminal entry points

The hosted entry points are **`https://starr.danteb.com/install.sh`** (POSIX sh
or Bash) and **`https://starr.danteb.com/install.py`** (Python). They become
available after the download service and NPM proxy described below are deployed.
Review the source before running downloaded code as root. No clone is needed:

```sh
# Show all options without root privileges or starting Docker services.
curl -fsSL https://starr.danteb.com/install.sh | sh -s -- --help

# Guided setup, with flags overriding individual questions.
curl -fsSL https://starr.danteb.com/install.sh | sudo sh -s -- \
  --content /srv/media --jellyfin --no-plex

# Equivalent Bash and Python entry points.
curl -fsSL https://starr.danteb.com/install.sh | sudo bash -s -- --help
curl -fsSL https://starr.danteb.com/install.py | sudo python3 - --help
```

Downloading before executing avoids executing a partially downloaded wrapper:

```sh
curl -fsSL https://starr.danteb.com/install.sh -o install.sh
less install.sh
sudo sh install.sh
```

Both wrappers download the same version-matched installer/profile archive,
verify its SHA-256 checksum, reject unsafe archive entries and run it from a
temporary directory. Prompts read from the controlling terminal, **not** the
pipe containing the program. Without a terminal, use explicit unattended flags.
The checksum detects corrupted/mismatched downloads; it is not a signature or
protection against a compromised HTTPS origin. The wrapper itself must be trusted.

For fully unattended setup, `--non-interactive --yes` is required. A direct,
non-VPN downloader additionally requires **`--allow-direct`**, never implied by
`--yes`. Example with Proton:

```sh
curl -fsSL https://starr.danteb.com/install.sh | sudo sh -s -- \
  --non-interactive --yes --root /opt/starr --content /srv/media \
  --uid 1000 --gid 1000 --timezone America/Chicago \
  --vpn proton --vpn-profile /root/vpn/torrents.conf \
  --vpn-country Netherlands --vpn-port-forwarding \
  --sabnzbd --jellyfin --seerr --auto-update
```

Or without a VPN, replace the VPN flags with `--vpn none --allow-direct`.
Unspecified optional apps are off in unattended mode; profile sync is on.
Core app accounts, additional indexers and provider onboarding are still required afterward.

| Options | Purpose |
|---|---|
| `-h`, `--help` | List all flags and exit without changing the machine |
| `--root`, `--content`, `--uid`, `--gid`, `--timezone` | Storage and ownership |
| `--bind`, `--username` | Web binding and generated web credential username |
| `--access-host HOST` | Reachable server IPv4/hostname for direct application links; defaults to the bind address |
| `--[no-]proxy` | Optional local Caddy HTTP proxy; off by default |
| `--proxy-domain`, `--proxy-port` | Local domain below `home.arpa` and HTTP port; defaults `starr.home.arpa`, `80` |
| `--subtitle-language CODE` | Bazarr subtitle profile/defaults; `en` by default, `none` to choose later |
| `--[no-]sabnzbd`, `--[no-]soulseek`, `--[no-]plex`, `--[no-]jellyfin`, `--[no-]seerr` | Optional apps |
| `--[no-]auto-update`, `--[no-]sync-profiles` | Cron image updates and daily Recyclarr sync |
| `--vpn {none,proton,wireguard,openvpn}`, `--vpn-profile FILE` | Torrent VPN |
| `--vpn-country`, `--[no-]vpn-port-forwarding`, `--vpn-static-port` | Provider/forwarding settings |
| `--vpn-username`, `--vpn-password-file FILE` | Custom OpenVPN credentials |
| `--soulseek-username`, `--soulseek-password-file FILE` | Soulseek network account |
| `--soulseek-vpn`, `--soulseek-vpn-profile`, etc. | Separate Soulseek tunnel; same VPN flag family |
| `--seerr-media-server {plex,jellyfin}` | Select an installed server; defaults to Plex when both are selected |
| `--request-profile NAME` | Seerr movie/TV quality profile; HD 1080p by default, matching Sonarr Anime profile when available |
| `--plex-claim-file FILE` | Fresh Plex claim token, read from a private file after image pulls |
| `--usenet-server-file FILE` | Private JSON containing Usenet provider settings and credentials |

Keep password/profile files private (`chmod 600`). Passwords are read from
files or a hidden prompt, not passed as command-line values visible in `ps`.
Flags supplied on reruns must match saved setup choices. `--bind`,
`--access-host`, proxy settings, `--[no-]auto-update` and `--[no-]sync-profiles` can change a saved installation;
changing its app selection, media identity or VPN credentials is a separate
manual reconfiguration, not a silent reset.

A repository clone remains an alternative for offline source inspection:

```sh
git clone https://github.com/dantebarbieri/starr-installer.git
sudo python3 starr-installer/scripts/starr/install.py
```

The checkout contains the installer and a hash-verified, commit-pinned public profile snapshot.
This is not a migration of the author's accounts, databases or credentials.
Use only indexers, trackers and content you are authorized to access.

The wizard asks for a dedicated configuration directory (default `/opt/starr`),
a separate content directory (default `/srv/media`), non-root media UID/GID,
timezone, web UI bind address, optional services, VPN profiles and cron updates.
For UID/GID, use `id your-media-user`. The content disk must already be mounted;
make persistent mounts before installation.

| Always installed | Optional |
|---|---|
| Radarr, Sonarr, Lidarr, Prowlarr, FlareSolverr | SABnzbd |
| Bazarr, CPU Whisper-ASR, qBittorrent | slskd + Soularr |
| Recyclarr with this repository's simplified profiles | Plex, Jellyfin, Seerr |

Whisper uses `faster_whisper`, `small`, CPU/int8 for broad compatibility. The
first transcription downloads its model; large jobs can take a long time.
Hardware acceleration is deliberately not assumed. Plex/Jellyfin start with
software transcoding; configure your particular GPU separately.

## What is configured automatically

- Consistent UID/GID, umask `002`, bounded container logs, persistent config,
  restart policies, and one content bind mount at `/data` in acquisition apps.
- Download categories `movies`, `tv`, `music`, their save directories, and Starr
  library roots. Hardlinks enabled; a real hardlink probe rejects unsuitable
  download/library filesystems instead of silently copying.
- Authenticated qBittorrent with a generated password, automatic torrent
  management, and disabled UPnP. No automatic seeding-ratio/time limit or
  torrent deletion: respect your trackers' requirements.
- qBittorrent download clients in all three Starr apps. Optional SABnzbd clients
  are created **disabled**; supplying a Usenet provider file tests NNTP
  authentication and the saved server, then tests and enables all three clients.
- Torrent-first delay profiles: preferred protocol `torrent`, torrent delay
  zero, Usenet delay 60 minutes, highest-quality bypass disabled. Client
  priorities are 1 for qBittorrent and 50 for SABnzbd. Priorities alone do not
  select a protocol; the delay profile handles that. This is not an absolute
  ban on Usenet while a better torrent might eventually appear.
- Prowlarr full-sync application connections, and a FlareSolverr proxy restricted
  to the `flaresolverr` tag. Tag only indexers that actually need it.
- A public Internet Archive torrent indexer, with the browser User-Agent
  workaround used on the homeserver. Its custom definition is derived from
  Prowlarr's installed definition, not downloaded separately, and persists in
  `config/prowlarr/Definitions/Custom/internetarchive-portable.yml`. It has no
  FlareSolverr tag. Prowlarr tests the connection before saving; failures stop
  setup so they can be corrected and retried. If upstream changes the expected
  definition structure or adds headers, setup stops for review rather than
  generating an ambiguous definition. Existing Archive entries (including
  renamed or disabled ones) and subsequent UI/custom-file edits are preserved;
  `.archive-provisioned` prevents reruns from recreating deleted entries.
  Archive hosts mixed-rights material: this does **not** filter results by
  license. Download only public-domain, openly licensed or otherwise authorized
  content; an Archive listing does not establish permission.
- Bazarr's Sonarr/Radarr connections, with Whisper enabled through its API at
  `http://whisperasr:9000`. Uses the live setup's 3-hour request timeout, 60-second
  polling interval and video-name hints; no provider credentials are copied.
- A Bazarr subtitle language profile (**English by default**), enabled language,
  and defaults for new series/movies where no defaults already exist. Choose
  another two-letter Bazarr language code with `--subtitle-language`, or `none`
  to configure profiles manually. Existing providers/profiles/defaults are kept.
- Soularr's slskd/Lidarr connections, matching download paths and FLAC-only
  search filetypes if selected. Without Soulseek credentials, those services
  remain in an inactive Compose profile instead of crash-looping.
- A FLAC/FLAC-24bit-only Lidarr quality profile, with upgrades and audio-tag
  rewriting disabled so music imports do not alter seeding hardlinks. Track
  naming matches the homeserver: artist folders, album/year directories and
  numbered titles, with disc numbers for multi-disc albums.
- The complete [public Recyclarr snapshot](../../recyclarr-configs/provenance.json):
  30 profiles per app, TRaSH custom formats, naming, size limits, and the local
  English-anime-audio provider. Initial sync requires explicit wizard consent.
  The Recyclarr image is pinned to 8.8.0 and subsequently syncs daily.
- Fresh Jellyfin administrator setup using the generated `username`/`password`
  in state.json, and Movies, TV and Music libraries under `/data/media/`.
  Media mounts are read-only; software transcoding is the default.
- Plex Movies, TV and Music libraries after the server is claimed. The installer
  reads that server's token locally; no credentials from the homeserver are used.
- Radarr, Sonarr and Lidarr Connect entries that refresh configured Plex/Jellyfin
  libraries after imports, upgrades and renames. Fresh Jellyfin gets a dedicated
  Starr API key. Existing unmanaged Jellyfin setups require manual Connect setup;
  unclaimed Plex connections are deferred along with its libraries.
- Seerr's Radarr/Sonarr connections, root paths, request profiles, selected media
  server and library synchronization. Both server options are offered; Plex is
  the default when both are installed. Default requests use **HD - 1080p** and
  Sonarr's matching Anime profile when Recyclarr is enabled, or the native
  **HD-1080p** profile without sync. `--request-profile` selects another existing
  name. This does not create a separate 4K server instance or auto-approve users.

### Optional app credentials and deferred setup

For Plex, put a fresh token from <https://plex.tv/claim> in a private one-line
file and supply `--plex-claim-file`. Claim tokens expire in about four minutes:
if initial image pulls take too long, refresh the file and rerun. Alternatively,
claim the server in Plex's UI, then rerun installation. Without a claimed server,
Plex library creation and Seerr's Plex authentication are explicitly **deferred**.
Seerr's initial Starr connections are still preconfigured. The installer does
not silently switch a Plex selection to Jellyfin.

Selecting Jellyfin completes its fresh wizard and permits Seerr to authenticate
with the generated local administrator. Existing completed Jellyfin or Seerr
setups are preserved, not adopted or reset; an existing Jellyfin administrator
must sign in to Seerr themselves.

For SABnzbd, use `--usenet-server-file /root/secrets/usenet.json`:

```json
{
  "host": "news.example.net",
  "port": 563,
  "ssl": true,
  "username": "your-provider-username",
  "password": "your-provider-password",
  "connections": 20
}
```

Use `chmod 600` on the file. Port 563, TLS with certificate/hostname validation,
and 20 connections are the defaults. Follow your provider's connection limit.
Credentials are sent to SABnzbd in a POST body, not URL/command arguments.
Connection/authentication failures stop setup without enabling its Starr
clients. Without credentials, setup is deferred; supply the file and rerun.
Existing provider entries and completed component settings are not overwritten.
Credential-file flags can be corrected while their component is incomplete;
after completion use the app UI. Request-profile selection is fixed once
Seerr's settings have been seeded, so reruns do not replace UI edits.

This follows the
[TRaSH Docker folder layout](https://trash-guides.info/File-and-Folder-Structure/How-to-set-up/Docker/)
and reuses the repository's intentional profile policies, **not** a claim that
every profile is an unchanged official TRaSH preset. Separate filesystems,
network filesystems without hardlinks, and library submounts can defeat atomic
imports even when the folder names look correct. Do not split `/data` into
separate download/library bind mounts inside the Starr containers.

```text
/srv/media/                 # your selected content path -> /data
  torrents/{movies,tv,music,incomplete}
  usenet/incomplete
  usenet/complete/{movies,tv,music}
  soulseek/{downloads,incomplete}
  media/{movies,tv,music}
```

Existing files are not reorganized. Existing content is not recursively
`chown`ed or `chmod`ed: grant your chosen UID/GID access deliberately.
After the first import, verify the downloaded file and library copy share an
inode with `stat`; their link count should be at least two.

## First login and remaining account-specific setup

Web ports bind to **127.0.0.1 by default**, not all interfaces. For a remote
server, keep this default during onboarding and open an SSH tunnel from your
own computer (substitute the SSH username/host and any nonstandard SSH port):

```sh
ssh -N -L 7878:127.0.0.1:7878 -L 8989:127.0.0.1:8989 \
  -L 8686:127.0.0.1:8686 -L 9696:127.0.0.1:9696 \
  -L 8080:127.0.0.1:8080 -L 6767:127.0.0.1:6767 user@server
```

| App | Host web port | Container URL |
|---|---|---|
| Radarr / Sonarr / Lidarr | 7878 / 8989 / 8686 | `http://radarr:7878`, `http://sonarr:8989`, `http://lidarr:8686` |
| Prowlarr / Bazarr | 9696 / 6767 | `http://prowlarr:9696`, `http://bazarr:6767` |
| qBittorrent | 8080 | `http://qbittorrent:8080` or `http://gluetun:8080` with VPN |
| SABnzbd | 8085 | `http://sabnzbd:8080` |
| slskd | 5030 | `http://slskd:5030` or `http://gluetun-soulseek:5030` with VPN |
| Plex / Jellyfin / Seerr | 32400 / 8096 / 5055 | `http://plex:32400`, `http://jellyfin:8096`, `http://seerr:5055` |
| Whisper / FlareSolverr | not published | `http://whisperasr:9000`, `http://flaresolverr:8191` |

Read the generated web credentials/API keys from the root-only
`/opt/starr/state.json`, for example with `sudo less /opt/starr/state.json`.
Treat this file, `compose.json`, `private/`, and all app configs as secrets.
Do not paste their contents into issues or commit them.

1. Create the Radarr/Sonarr/Lidarr/Prowlarr administrator accounts in their
   first-run wizards. The installer keeps their built-in authentication enabled.
2. Internet Archive is preconfigured in Prowlarr. Add other authorized indexers
   and your credentials. No private tracker credentials
   are copied. Check application sync and test a lawful download/import.
3. In Bazarr, configure authentication and any additional subtitle-provider
   accounts. Whisper is already enabled. Check the generated language profile
   (English unless changed), or create one if you selected `none`. CPU
   transcription can be slow.
4. If SABnzbd setup was deferred, supply `--usenet-server-file` and rerun, or
   configure/test its provider and enable **Portable SABnzbd** manually in
   each Starr app. Do not enable an unconfigured client.
5. If Soulseek was deferred, edit `soulseek_username` and `soulseek_password`
   in root-only state, then rerun installation. Soularr watches Lidarr's
   monitored/wanted albums; it does not manufacture indexer results. Set an
   appropriate Lidarr quality/metadata profile before monitoring artists.
6. Fresh Jellyfin libraries and its administrator are already configured.
   If Plex/Seerr setup was deferred, complete the owner's sign-in/claim, then
   rerun installation to finish libraries and connections. Check Seerr's chosen
   request profile and library availability before accepting requests.

To make UIs LAN-accessible after onboarding, change `bind` in state.json to
the server's LAN IPv4 address and rerun. A LAN bind is **not authentication**.
Secure Bazarr/SABnzbd and finish all first-run wizards first. No host ports
are opened for Whisper or FlareSolverr. Non-VPN torrent/Soulseek peer ports
(6881 TCP/UDP, 50300 TCP) are published on all host interfaces; no router rules
are created. No web port should be internet-forwarded. Docker port publishing
can bypass UFW; use Docker-aware firewall rules or a trusted private network.

## VPN options

The VPN applies only to peer downloaders, not your whole machine, Starr
indexer traffic, Plex or Usenet. Gluetun provides its firewall/kill switch;
qBittorrent is additionally bound to `tun0`. A failed tunnel does not trigger
any non-VPN fallback. `/dev/net/tun` must be available on the host, including
when the host is an LXC/VM. Rootless Docker is not supported by this installer.

- **Proton WireGuard:** generate a **Linux** profile at
  [Proton VPN's WireGuard page](https://account.proton.me/u/0/vpn/WireGuard).
  Choose a P2P-capable server and enable **NAT-PMP** if you want forwarding.
  Supply the downloaded file, a server country, and whether forwarding should
  be enabled. A suitable paid plan is normally required. Gluetun selects a
  Proton endpoint; it is not tied to the downloaded profile's single server.
- **Other WireGuard providers:** export a standard `[Interface]`/`[Peer]`
  profile. The installer imports private/public/preshared keys, addresses and
  endpoint into Gluetun's `custom` provider. The endpoint must be a literal
  IP address. DNS, AllowedIPs, PostUp/PostDown and arbitrary wg-quick commands
  are not imported; Gluetun owns full-tunnel routing, DNS and firewall.
- **OpenVPN:** supply a self-contained `.ovpn` with inline certificates/keys
  plus the provider's OpenVPN credentials (often different from web login).
  Executable hooks/plugins and external certificate file paths are rejected.
  Custom OpenVPN exports are provider-specific; unsupported ones fail visibly
  in Gluetun rather than starting the downloader directly.

Proton dynamic port forwarding gets a persistent helper that sets and verifies
the download client's peer port. Other providers can supply a **static**
forwarded port; forwarding is not supported by every VPN provider.
Dynamic PIA/other-provider integration is not currently implemented. See the
[Gluetun provider docs](https://github.com/qdm12/gluetun-wiki/tree/main/setup/providers)
and [port-forwarding docs](https://github.com/qdm12/gluetun-wiki/blob/main/setup/advanced/vpn-port-forwarding.md).

Soulseek gets a **separate** tunnel/profile and peer-port helper. One Proton
forwarded port cannot serve both apps. Do not use the same WireGuard identity
in simultaneous tunnels; export a second profile. Direct connections require
explicit confirmation and can reduce privacy or peer reachability. Before
adding downloads, check Gluetun's public-IP/health logs and verify peer traffic
stops when its tunnel is unavailable. The installer cannot prove an arbitrary
provider's external port mapping or inspect your ISP/router firewall.

## Local names, application links and optional Caddy

The setup website generates app links with self-hosted application logos from
your selections, server hostname/IP and known ports. The installer prints the
same destinations and writes `access/index.html`, including its icons and their
license, under the installation directory. No app credentials are included.
Plex links open `/web/`. Deferred Soulseek does not get a dead link.

`--access-host server.local` sets link destinations only: the hostname must
already resolve on the client, and it does not change the bind address. With
`--bind 0.0.0.0`, supply a reachable `--access-host`; `0.0.0.0` is not a client
destination. Loopback binds remain private and require SSH forwarding.

For optional hostname-based routing, add:

```sh
--proxy --proxy-domain starr.home.arpa --proxy-port 8088
```

The Caddy landing page is `http://starr.home.arpa:8088/`; applications use names
such as `http://radarr.starr.home.arpa:8088/`. Port 80 is the default and omits
the port in links. Choose a free port if another proxy/web server occupies it.
The Caddy container runs as the selected non-root UID, with read-only mounts
and no Docker socket. It routes to internal app addresses, including VPN
gateways, supports WebSockets, and rejects unknown Host names.

This is **local HTTP**, not automatic HTTPS or authentication. No certificates,
DNS records, firewall rules or router settings are created. The proxy uses the
same `--bind` as the direct app ports; direct fallback links remain available.
Keep loopback during onboarding and finish app authentication before LAN access.
Do not forward these ports from the internet. Use a separately configured,
authenticated HTTPS gateway if remote access is needed.

Map the exact names from `access/hosts.txt` to the server's LAN IPv4 in local
DNS or each client's hosts file. Hosts files cannot use wildcards:

```text
192.168.1.100 starr.home.arpa radarr.starr.home.arpa sonarr.starr.home.arpa
```

Include the remaining selected apps from the generated file. If a hostname was
supplied instead of an IP, replace `SERVER_LAN_IPV4` in the example. Client hosts
files are `/etc/hosts` on Linux/macOS and
`C:\Windows\System32\drivers\etc\hosts` on Windows; editing requires administrator
access. Router DNS changes can reboot the router or interrupt the network;
schedule them during a low-usage window instead of assuming they are safe.

For onboarding with a loopback bind and proxy port 8088:

```sh
ssh -N -L 8088:127.0.0.1:8088 user@server
```

Map the generated names to `127.0.0.1` on that client while using the tunnel.
Direct fallback links need their own forwarded ports. For LAN use, set `--bind`
to the server's LAN IPv4 and use that address in DNS/hosts entries.

Reruns can enable/disable Caddy or change its domain, port and link host.
`--no-proxy` removes only the installer-managed Caddy container, not app data.
Generated `access/index.html`, `access/hosts.txt` and `config/caddy/Caddyfile`
are regenerated; do not put custom configuration there. Icon files remain
available for reruns without the original download. Changing the bind address
does not change an explicitly saved `--access-host`; update both when needed.

qBittorrent retains Host-header and CSRF protection. The installer configures
reverse-proxy support, trusts the current Caddy container IP rather than the
whole Docker subnet, and refreshes that IP during managed image updates.
Existing trusted proxies/hostname entries are preserved. SABnzbd's hostname
allowlist is extended for its selected names without disabling its checks.

## Reruns, updates and recovery

```sh
sudo python3 starr-installer/scripts/starr/install.py --root /opt/starr
sudo python3 /opt/starr/bin/starr.py update --root /opt/starr
sudo docker compose -f /opt/starr/compose.json ps
sudo docker compose -f /opt/starr/compose.json logs --tail 100 gluetun
```

Reruns reuse saved answers and generated credentials, create missing directories,
and seed configs **only if absent**. API setup is create-if-missing and
checkpointed only after success; partial failures can be rerun without duplicate
download clients, roots or Prowlarr connections. Successfully provisioned stacks
are not reset to defaults. Changing app API keys/passwords requires reconciling
the dependent credentials manually, not deleting state.json to regenerate them.
This is an installer for fresh dedicated directories, not an adoption tool for
existing Starr databases.

Bazarr API setup has its own completion marker. A failed setup can be retried,
including correcting `--subtitle-language` before it completes. After completion,
change subtitle preferences in Bazarr; reruns preserve those edits. Rerunning an
older installer state enables Whisper without imposing a new language profile
unless `--subtitle-language` is supplied.

The generated Compose project has a path-derived name, no fixed container
names, no monorepo includes and no dependency on the original clone at runtime.
Ports must still be free; stop or reconfigure other apps occupying them first.
The downloaded bundle or original checkout is needed only for first-time Recyclarr seeding. Profiles
are copied as a complete snapshot and local YAML edits are preserved on rerun;
Git pulls do **not** silently replace the installed profile policy. When enabled,
Recyclarr sync still updates its managed application profiles, custom formats,
naming and size limits from that YAML policy.

Optional root cron updates run at **04:00 host-local time** (not necessarily the
wizard's container timezone). The job uses a lock, logs to `update.log`, preserves
unrelated crontab entries, pulls images before stopping anything, and recreates
VPN namespace consumers after their gateways to avoid stale network namespaces.
It does not run `down`, prune images/volumes, fetch or execute new Git code,
or update the installer itself. There can be brief downtime. A failed
recreation is a visible error in the log; automatic rollback is not provided.
Recyclarr remains on its pinned image version. Keep config/database backups
before enabling unattended upgrades; RAID is not a backup.

To disable future cron updates, rerun installation with `--root /opt/starr
--no-auto-update`, or set `auto_update` to `false` in state.json and rerun.
This removes only this installation's managed cron entry.
Alternatively remove its `# portable-starr:` line using `sudo crontab -e`.
Merely stopping a container does not disable its updater.

No cleanup command removes media or config data. To stop the stack without
deleting it, use `sudo docker compose -f /opt/starr/compose.json down`.

## Hosting at starr.danteb.com

The self-contained `ghcr.io/dantebarbieri/starr-installer:latest` image runs as
UID/GID **65534:65534**, internally on port **8080**, with `/healthz`. All public
files are bundled at `/source` (`SOURCE_ROOT=/source`). Use a read-only rootfs;
no mounts, writable volumes, Docker socket, Git fetches or credentials are needed.
Set `PUBLIC_URL=https://starr.danteb.com`. Pin an image digest for reproducibility.
Recreate the container to update its immutable bundled installer/guide/profiles.
The [repository README](https://github.com/dantebarbieri/starr-installer#download-hosting-image) is the stable
hosting contract and includes a local inspection command.

Create an Nginx Proxy Manager proxy host with domain `starr.danteb.com`, scheme
**http**, forward hostname **starr**, port **8080**, and a valid TLS certificate.
Enable Force SSL and Block Common Exploits. WebSockets are not needed.
**Do not add Authelia or an access list**: this is intentionally public download
content, not an unprotected administration application. Configure your own DNS
and proxy network; the image does not change router/host firewall settings.

The public surface is strictly allowlisted: `/`, `/styles.css`, `/app.mjs`,
`/healthz`, `/i`, `/install.sh`, `/install.py`, `/release.json`, and checksum-addressed
release archives. It is
**not** a directory browser. `.env`, private Recyclarr `secrets.yml`, app configs,
Git metadata and installer state are never included or served. Release bundles
contain only the installer, this guide, public profile/settings files and the
local custom-format JSON. No user answers or credentials are sent to this host.

The guide is plain HTML/CSS/JavaScript under `scripts/starr/site/`, with no build
step, frontend dependencies or external fonts/scripts. CSP allows only same-origin
scripts/styles and blocks network connections and form submissions. Its assets
are bundled in the hosting image, not loaded from a runtime checkout mount.

Entry points use `Cache-Control: no-store`; archive URLs are immutable. The last
eight snapshots remain in memory so requests cannot mix versions during local
source development. Production sources are immutable. Restarting the host clears that
cache: a previously saved wrapper can get a clear 404 and must be downloaded
again. Source changes detected during a snapshot return 503 instead of publishing
that snapshot. The profile provenance and hashes must also verify before publication.
See [profile maintenance and release policy](https://github.com/dantebarbieri/starr-installer#public-profile-policy).

## Development checks

```sh
uv run --no-project python -B -m unittest discover -s scripts/starr/tests -v

# Disposable LOCAL Linux Docker test; pulls images, uses the standard web ports,
# configures empty apps twice, syncs Recyclarr, then removes its own containers.
# Never point Docker at production for this test.
sudo env STARR_DOCKER_TESTS=1 python3 -B -m unittest discover \
  -s scripts/starr/tests -p test_install.py -k DockerIntegration -v

# Also pull/start Whisper, Plex, Jellyfin and Seerr; run the entire CLI twice.
sudo env STARR_DOCKER_TESTS=1 STARR_DOCKER_OPTIONAL=1 python3 -B -m unittest discover \
  -s scripts/starr/tests -p test_install.py -k complete_cli -v

# Actual download container, then a fresh piped unattended install twice.
sudo env STARR_DOCKER_TESTS=1 STARR_DOCKER_OPTIONAL=1 python3 -B -m unittest discover \
  -s scripts/starr/tests -p test_downloads.py -k DockerDownloadTests -v
```

JavaScript command-builder checks use Node.js when available locally. CI requires Node
and passes its executable to the root-run suite using `STARR_NODE`. These checks
compare generated flags against the real Python argument parser, exercise all
VPN combinations, and roundtrip literal shell arguments on Linux.

The integration test does not have real VPN, tracker, Usenet, Soulseek or Plex
credentials. Those external services and GPU acceleration need operator
acceptance checks; simulated credentials cannot establish their functionality.
