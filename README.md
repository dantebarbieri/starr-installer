# Starr installer

Guided Docker installation for a fresh Linux server: Starr apps, download clients,
optional media servers, private access, VPN profiles and repeat-safe API setup.
Requires Python 3.10+, local rootful Docker Engine and Compose v2; it does not
install prerequisites or change your firewall.

```sh
curl -fsSL https://starr.danteb.com/i | sudo sh
```

Review [the installer](scripts/starr/install.py) before running downloaded code as
root. The [static browser guide](https://starr.danteb.com) builds commands locally:
no analytics, credential uploads, account or settings API. Server-side credential
file paths are supported; never enter secrets into the guide.

See the **[full installation, flags and recovery guide](scripts/starr/README.md)**.
Core administrator accounts and provider/indexer onboarding remain explicit.
Plex, Usenet and Soulseek account-bound setup is deferred until you supply your
own credentials. Nothing imports the author's private configuration.

## Download hosting image

`ghcr.io/dantebarbieri/starr-installer:latest` bundles the server, bootstrap,
installer, guide, offline icons/licenses and pinned public profiles. No runtime
checkout mounts, writable volumes, Docker socket or GitHub credentials are needed.

| Contract | Value |
|---|---|
| HTTP | Port `8080`, `/healthz` readiness |
| User | UID/GID `65534:65534` |
| Source | Immutable image contents at `/source`; `SOURCE_ROOT=/source` |
| Public origin | `PUBLIC_URL=https://starr.danteb.com`; HTTPS origin required |
| Filesystem | Read-only rootfs supported; no writable volumes |
| Platforms | `linux/amd64`, `linux/arm64` (integration checks run on amd64) |

For local inspection (loopback HTTP is allowed only for tests):

```sh
docker run --rm --read-only --cap-drop ALL --security-opt no-new-privileges:true \
  -p 127.0.0.1:8088:8080 -e PUBLIC_URL=http://127.0.0.1:8088 \
  ghcr.io/dantebarbieri/starr-installer:latest
```

Behind Nginx Proxy Manager, connect the host to the proxy's Docker network and
forward `starr.danteb.com` to the container's name on HTTP port `8080`. Set
`PUBLIC_URL=https://starr.danteb.com`, use a TLS certificate and Force SSL.
Do not add an authentication/access-list gate: these are public downloads, not
app administration endpoints. Deploying this repository does not deploy the
production proxy or change DNS/firewall rules.

Only explicitly allowlisted assets and checksum-addressed bundles are served;
there is no directory browser. Bootstrap verifies the bundle SHA-256 and rejects
unsafe archives. Hashes detect corruption/mixed releases, not a compromised
HTTPS origin. Entry points are never cached; archives are immutable. A saved
wrapper from an older host image may need downloading again after a host restart.
Use a digest reference for reproducible hosting; `latest` tracks passing main
builds. Recreate the container to update bundled files.

## Public profile policy

Homeserver remains the authoritative policy source. Releases contain exactly
seven public files pinned to a full commit, recorded with SHA-256 hashes and
Recyclarr **8.8.0** in [provenance.json](recyclarr-configs/provenance.json).
The initial extraction is also recorded in [migration-source.json](migration-source.json).
That handoff recorded Windows checkout bytes; the released pin is refreshed from
the same commit's upstream blobs, with exact bytes preserved by `.gitattributes`.
The host verifies the snapshot before serving a release; the installer verifies
it before first seeding. Installation does **not** fetch mutable homeserver main.
Existing local profile edits are retained on reruns; installed provenance is
saved beside those profiles.

To verify or deliberately refresh the pin:

```sh
python3 scripts/starr/profiles.py verify
python3 scripts/starr/profiles.py refresh --commit FULL_40_CHARACTER_HOMESERVER_SHA
git diff -- recyclarr-configs
```

The refresh command fetches only the fixed public allowlist from that commit,
never `secrets.yml`. Review the diff, run the checks and release it normally.
This records content hashes, not a signature or proof of upstream authorship.

## Checks and releases

Required branch-protection candidates (exact GitHub job contexts):

- `Fast tests (Python 3.10)`
- `Fast tests (Python 3.14)`
- `Fast tests (Python current)`
- `Full Docker integration`

The `current` job resolves the latest stable Python using `3.x`; Python 3.14
remains an explicit compatibility check and the hosting image runtime.
All branch pushes and pull requests run these checks. Node command-builder
checks are mandatory. Full integration builds/runs the hosting image with a
read-only rootfs, then performs disposable installs/reruns against fresh pulled
application images, with both `STARR_DOCKER_TESTS=1` and
`STARR_DOCKER_OPTIONAL=1`. It requires a local rootful Linux Docker host with
free Starr ports, several GiB of downloads and enough disk/RAM for the optional
media apps; never run it against production or concurrently with another suite.
Cold CI runs have a 45-minute budget and reclaim unused hosted-runner SDK space.
The same fresh-image suite runs every Monday and on manual dispatch to catch
mutable upstream image changes. Schedule/manual/PR/feature-branch runs never publish.

Only passing trusted `main` pushes publish `latest` and `sha-<full-commit>` to
GHCR. A version push such as `v1.0.0` must point to a commit on main and pass all
checks; it publishes the version and SHA image tags plus a GitHub release with
the profile provenance, installer bundle and checksums. Version pushes do not
move `latest`. Pin the returned image digest when immutability is required;
image tags are labels and should not be force-republished.

After first publication, the package owner must ensure the GHCR package is
**Public** in its package settings. A public source repository alone does not
guarantee anonymous pulls. Verify an unauthenticated pull before deployment.

For local checks, use the commands in the [development guide](scripts/starr/README.md#development-checks).
Application logos retain their upstream [license](scripts/starr/site/icons/LICENSE)
and [source notice](scripts/starr/site/icons/NOTICE).
