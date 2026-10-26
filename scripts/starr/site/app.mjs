const APPS = ["sabnzbd", "soulseek", "plex", "jellyfin", "seerr"];
const WEB_APPS = [
  ["radarr", "Radarr", 7878], ["sonarr", "Sonarr", 8989], ["lidarr", "Lidarr", 8686],
  ["prowlarr", "Prowlarr", 9696], ["bazarr", "Bazarr", 6767], ["qbittorrent", "qBittorrent", 8080],
  ["sabnzbd", "SABnzbd", 8085], ["slskd", "slskd", 5030], ["plex", "Plex", 32400],
  ["jellyfin", "Jellyfin", 8096], ["seerr", "Seerr", 5055],
];
const SYSTEM_PATHS = new Set(["/", "/etc", "/usr", "/var", "/home", "/srv", "/opt",
  "/tmp", "/var/tmp", "/run", "/mnt", "/media", "/root"]);

export function shellQuote(value) {
  return "'" + String(value).replaceAll("'", "'\"'\"'") + "'";
}

function line(value, label) {
  const text = String(value ?? "");
  if (!text.trim() || /[\x00-\x1f\x7f]/.test(text)) {
    throw new Error(`${label} must be a nonempty, single-line value.`);
  }
  return text;
}

function path(value, label, dedicated = false) {
  const text = line(value, label);
  if (!text.startsWith("/") || text.includes("%")) {
    throw new Error(`${label} must be an absolute server path without '%' (a cron delimiter).`);
  }
  const parts = [];
  for (const part of text.split("/")) {
    if (part === "." || part === "..") {
      throw new Error(`${label} must use an explicit path without '.' or '..' segments.`);
    }
    if (part) parts.push(part);
  }
  const normalized = "/" + parts.join("/");
  if (dedicated && SYSTEM_PATHS.has(normalized)) {
    throw new Error(`${label} must be a dedicated subdirectory, not a system directory.`);
  }
  return normalized;
}

function integer(value, label, min, max) {
  const text = line(value, label);
  const number = Number(text);
  if (!/^\d+$/.test(text) || !Number.isSafeInteger(number) || number < min || number > max) {
    throw new Error(`${label} must be a whole number from ${min} to ${max}.`);
  }
  return String(number);
}

function accessHostname(value) {
  const host = line(value, "Server hostname").toLowerCase().replace(/\.$/, "");
  if (host.length > 253 || !/^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)*$/.test(host) ||
      (/^[\d.]+$/.test(host) && (!/^(0|[1-9]\d{0,2})(\.(0|[1-9]\d{0,2})){3}$/.test(host) ||
        host.split(".").some(part => Number(part) > 255) || host === "0.0.0.0"))) {
    throw new Error("Use a server IPv4 address or DNS hostname, without a scheme, port or path.");
  }
  return host;
}

function accessSettings(settings) {
  const host = settings.accessHost ? accessHostname(settings.accessHost)
    : (settings.bind === "0.0.0.0" ? "" : accessHostname(settings.bind));
  let domain = "", suffix = "", port = 80;
  if (settings.proxy) {
    domain = accessHostname(settings.proxyDomain || "starr.home.arpa");
    if (!domain.endsWith(".home.arpa") || domain.length > 230) {
      throw new Error("Use a local proxy domain below home.arpa, such as starr.home.arpa.");
    }
    port = Number(integer(settings.proxyPort ?? "80", "Proxy port", 1, 65535));
    if ([...WEB_APPS.map(app => app[2]), 6881, 50300].includes(port)) {
      throw new Error("Proxy port must differ from the stack's app and peer ports.");
    }
    suffix = port === 80 ? "" : ":" + port;
  }
  return {host, domain, suffix, port};
}

export function buildAccessLinks(settings) {
  const {host, domain, suffix} = accessSettings(settings);
  const apps = WEB_APPS.filter(([name]) => name === "slskd"
    ? settings.apps.soulseek && settings.soulseekAccount?.username
    : !APPS.includes(name) || settings.apps[name]);
  return {
    links: apps.map(([name, label, port]) => {
      const path = name === "plex" ? "/web/" : "/";
      return {name, label, icon: `/icons/${name}.svg`,
        direct: host ? `http://${host}:${port}${path}` : "",
        proxy: domain ? `http://${name}.${domain}${suffix}${path}` : ""};
    }),
    dashboard: domain ? `http://${domain}${suffix}/` : "",
    hostsEntry: domain ? `${/^[\d.]+$/.test(host) ? host : "SERVER_LAN_IPV4"} ${[
      domain, ...apps.map(([name]) => name + "." + domain)].join(" ")}` : "",
  };
}

function timezoneValue(settings) {
  let timezone = settings.timezone;
  if (timezone === "automatic") {
    timezone = Intl.DateTimeFormat().resolvedOptions().timeZone;
    if (!timezone) throw new Error("Browser timezone detection is unavailable. Select a timezone.");
  } else if (timezone === "custom") {
    timezone = settings.customTimezone;
  }
  timezone = line(timezone, "Timezone");
  try { new Intl.DateTimeFormat("en", { timeZone: timezone }); }
  catch { throw new Error("Use a valid IANA timezone, such as Etc/UTC or America/Chicago."); }
  return timezone;
}

export function buildCommand(settings, origin) {
  const url = new URL(origin);
  if ((url.protocol !== "https:" && !(url.protocol === "http:" &&
      ["localhost", "127.0.0.1", "[::1]"].includes(url.hostname))) ||
      url.username || url.password || url.pathname !== "/" || url.search || url.hash) {
    throw new Error("Installer downloads require an HTTPS origin (or local preview).");
  }
  const root = path(settings.root, "Installation directory", true);
  const content = path(settings.content, "Content directory", true);
  if (root === content || root.startsWith(content + "/") || content.startsWith(root + "/")) {
    throw new Error("Installation and content directories must be separate, non-nested paths.");
  }
  if (!["guided", "unattended"].includes(settings.setupMode) ||
      !["sh", "bash", "python"].includes(settings.interpreter) ||
      !["download", "pipe"].includes(settings.delivery)) {
    throw new Error("Choose a supported setup mode, interpreter and download style.");
  }
  const args = [];
  const add = (flag, value) => args.push(value === undefined ? flag : `${flag}=${value}`);
  add("--root", root);
  add("--content", content);
  add("--uid", integer(settings.uid, "UID", 1, 4294967294));
  add("--gid", integer(settings.gid, "GID", 0, 4294967294));
  add("--timezone", timezoneValue(settings));
  const bind = line(settings.bind, "Web bind address");
  if (!/^(0|[1-9]\d{0,2})(\.(0|[1-9]\d{0,2})){3}$/.test(bind) ||
      bind.split(".").some(part => Number(part) > 255)) {
    throw new Error("Web bind address must be an IPv4 address; loopback (127.0.0.1) is recommended.");
  }
  add("--bind", bind);
  const access = accessSettings(settings);
  if (settings.accessHost) add("--access-host", access.host);
  add(settings.proxy ? "--proxy" : "--no-proxy");
  if (settings.proxy) {
    add("--proxy-domain", access.domain);
    add("--proxy-port", access.port);
  }
  add("--username", line(settings.username, "Web username"));
  const subtitleLanguage = line(settings.subtitleLanguage === "custom"
    ? settings.customSubtitleLanguage : settings.subtitleLanguage, "Subtitle language").toLowerCase();
  if (subtitleLanguage !== "none" && !/^[a-z]{2}$/.test(subtitleLanguage)) {
    throw new Error("Use a two-letter Bazarr subtitle language code, or choose language/profile later.");
  }
  add("--subtitle-language", subtitleLanguage);
  for (const app of APPS) add(`--${settings.apps[app] ? "" : "no-"}${app}`);
  if (settings.apps.seerr) {
    const selected = !settings.seerrMediaServer || settings.seerrMediaServer === "automatic"
      ? (settings.apps.plex ? "plex" : "jellyfin") : settings.seerrMediaServer;
    if (!["plex", "jellyfin"].includes(selected) || !settings.apps[selected]) {
      throw new Error("Select the Plex or Jellyfin server that Seerr will use.");
    }
    add("--seerr-media-server", selected);
    if (settings.requestProfile) {
      if (!settings.syncProfiles) throw new Error("Enable profile sync to use a repository quality profile, or choose automatic HD 1080p.");
      add("--request-profile", line(settings.requestProfile, "Seerr quality profile"));
    }
  }
  if (settings.apps.plex && settings.plexClaimFile) {
    add("--plex-claim-file", path(settings.plexClaimFile, "Plex claim-token file"));
  }
  if (settings.apps.sabnzbd && settings.usenetServerFile) {
    add("--usenet-server-file", path(settings.usenetServerFile, "Usenet provider file"));
  }
  add(settings.autoUpdate ? "--auto-update" : "--no-auto-update");
  add(settings.syncProfiles ? "--sync-profiles" : "--no-sync-profiles");
  function vpn(config, prefix) {
    if (!["none", "proton", "wireguard", "openvpn"].includes(config.mode)) {
      throw new Error("Choose a supported VPN format.");
    }
    add(`--${prefix}`, config.mode);
    if (config.mode === "none") return;
    add(`--${prefix}-profile`, path(config.profile, "VPN profile"));
    if (config.mode === "proton") {
      add(`--${prefix}-country`, line(config.country, "Proton country"));
      add(`--${config.forwarding ? "" : "no-"}${prefix}-port-forwarding`);
    }
    if (config.mode !== "proton" || !config.forwarding) {
      add(`--${prefix}-static-port`, integer(config.port, "Forwarded port", 0, 65535));
    }
    if (config.mode === "openvpn") {
      if (config.username) add(`--${prefix}-username`, line(config.username, "OpenVPN username"));
      if (config.passwordFile) add(`--${prefix}-password-file`, path(config.passwordFile, "OpenVPN password file"));
    }
  }
  vpn(settings.vpn, "vpn");
  if (settings.apps.soulseek) {
    const account = settings.soulseekAccount;
    if (account.passwordFile && !account.username) {
      throw new Error("A Soulseek password file requires a network username, or leave both blank to defer.");
    }
    if (account.username) {
      add("--soulseek-username", line(account.username, "Soulseek username"));
      if (account.passwordFile) add("--soulseek-password-file", path(account.passwordFile, "Soulseek password file"));
      else if (settings.setupMode === "unattended") {
        throw new Error("Unattended Soulseek account setup requires a private server-side password file.");
      }
    }
    vpn(settings.soulseekVpn, "soulseek-vpn");
  }
  if (settings.setupMode === "unattended") {
    add("--non-interactive");
    add("--yes");
    if (settings.vpn.mode === "none" || (settings.apps.soulseek && settings.soulseekVpn.mode === "none")) {
      if (!settings.allowDirect) throw new Error("Approve direct peer traffic below, or configure a VPN for each downloader.");
      add("--allow-direct");
    }
  }
  const python = settings.interpreter === "python";
  const filename = python ? "starr-install.py" : "starr-install.sh";
  const download = `${url.origin}/install.${python ? "py" : "sh"}`;
  const interpreter = python ? "python3" : settings.interpreter;
  const runner = settings.delivery === "download"
    ? `curl -fsSL ${shellQuote(download)} -o ${filename} &&\nsudo ${interpreter} ./${filename}`
    : `curl -fsSL ${shellQuote(download)} | sudo ${interpreter}${python ? " -" : " -s --"}`;
  const rendered = args.map(arg => {
    const split = arg.indexOf("=");
    return split < 0 ? arg : arg.slice(0, split + 1) + shellQuote(arg.slice(split + 1));
  });
  return { args, command: runner + " \\\n  " + rendered.join(" \\\n  ") };
}

function initialize() {
  const form = document.querySelector("#settings");
  for (const name of ["SD - 480p", "SD - 576p", "HD - 720p", "HD - 1080p", "4K UHD - 2160p",
    "At most 480p", "At most 576p", "At most 720p", "At most 1080p", "At most 2160p"]) {
    for (const suffix of ["", " Anime", " Anime (Dub)"]) {
      const option = document.createElement("option");
      option.value = option.textContent = name + suffix;
      form.elements.namedItem("requestProfile").append(option);
    }
  }
  const timezone = form.elements.namedItem("timezone");
  const detected = Intl.DateTimeFormat().resolvedOptions().timeZone;
  timezone.options[0].textContent = detected ? `Automatic (${detected})` : "Automatic (unavailable)";
  const customOption = timezone.querySelector('[value="custom"]');
  const groups = new Map();
  const zones = typeof Intl.supportedValuesOf === "function" ? Intl.supportedValuesOf("timeZone") : [];
  for (const zone of zones) {
    if (zone === "UTC" || zone === "Etc/UTC") continue;
    const region = zone.split("/")[0];
    if (!groups.has(region)) {
      const group = document.createElement("optgroup");
      group.label = region;
      groups.set(region, group);
      timezone.insertBefore(group, customOption);
    }
    const option = document.createElement("option");
    option.value = zone;
    option.textContent = zone.replaceAll("_", " ");
    groups.get(region).append(option);
  }
  const template = document.querySelector("#vpn-template");
  for (const container of document.querySelectorAll("[data-vpn]")) {
    container.append(template.content.cloneNode(true));
    for (const field of container.querySelectorAll("[data-field]")) {
      field.name = container.dataset.vpn + "-" + field.dataset.field;
    }
  }
  const value = name => form.elements.namedItem(name).value;
  const checked = name => form.elements.namedItem(name).checked;
  const readVpn = prefix => Object.fromEntries(["mode", "profile", "country", "forwarding", "port",
    "username", "passwordFile"].map(key => [key, key === "forwarding" ? checked(prefix + "-" + key) : value(prefix + "-" + key)]));
  const output = document.querySelector("#command");
  const error = document.querySelector("#command-error");
  const copy = document.querySelector("#copy-command");
  const status = document.querySelector("#copy-status");
  let current = "";
  let copyRevision = 0;
  function update() {
    copyRevision++;
    status.textContent = "";
    const settings = Object.fromEntries(["root", "content", "uid", "gid", "timezone", "customTimezone", "bind", "username",
      "subtitleLanguage", "customSubtitleLanguage",
      "seerrMediaServer", "requestProfile", "plexClaimFile", "usenetServerFile",
      "accessHost", "proxyDomain", "proxyPort",
      "setupMode", "interpreter", "delivery"].map(name => [name, value(name)]));
    document.querySelector("#custom-timezone").hidden = settings.timezone !== "custom";
    form.elements.namedItem("customTimezone").required = settings.timezone === "custom";
    document.querySelector("#custom-subtitle-language").hidden = settings.subtitleLanguage !== "custom";
    form.elements.namedItem("customSubtitleLanguage").required = settings.subtitleLanguage === "custom";
    settings.apps = Object.fromEntries(APPS.map(name => [name, checked(name)]));
    document.querySelector("#media-settings").hidden = !settings.apps.plex && !settings.apps.jellyfin && !settings.apps.seerr;
    document.querySelector("#seerr-settings").hidden = !settings.apps.seerr;
    document.querySelector("#request-profile-settings").hidden = !settings.apps.seerr;
    document.querySelector("#plex-claim-settings").hidden = !settings.apps.plex;
    document.querySelector("#usenet-settings").hidden = !settings.apps.sabnzbd;
    settings.autoUpdate = checked("autoUpdate");
    settings.proxy = checked("proxy");
    document.querySelector("#proxy-settings").hidden = !settings.proxy;
    settings.syncProfiles = checked("syncProfiles");
    settings.allowDirect = checked("allowDirect");
    settings.vpn = readVpn("vpn");
    settings.soulseekVpn = readVpn("soulseek-vpn");
    settings.soulseekAccount = { username: value("soulseekUsername"), passwordFile: value("soulseekPasswordFile") };
    document.querySelector("#soulseek-settings").hidden = !settings.apps.soulseek;
    for (const [prefix, config] of [["vpn", settings.vpn], ["soulseek-vpn", settings.soulseekVpn]]) {
      const container = document.querySelector(`[data-vpn="${prefix}"]`);
      const visible = { profile: config.mode !== "none", proton: config.mode === "proton",
        openvpn: config.mode === "openvpn", static: config.mode !== "proton" || !config.forwarding };
      for (const panel of container.querySelectorAll("[data-visible]")) {
        panel.hidden = !visible[panel.dataset.visible];
      }
    }
    document.querySelector("#direct-consent").hidden = settings.setupMode !== "unattended" ||
      (settings.vpn.mode !== "none" && (!settings.apps.soulseek || settings.soulseekVpn.mode !== "none"));
    document.querySelector("#content-preview").textContent = settings.content || "(choose a content directory)";
    try {
      current = buildCommand(settings, window.location.origin).command;
      const access = buildAccessLinks(settings);
      const links = document.querySelector("#app-links");
      links.replaceChildren();
      for (const app of access.links) {
        const card = document.createElement("li");
        const icon = document.createElement("img");
        icon.src = app.icon;
        icon.alt = "";
        icon.width = icon.height = 36;
        const main = document.createElement(app.proxy || app.direct ? "a" : "span");
        main.textContent = app.label;
        if (main.tagName === "A") {
          main.href = app.proxy || app.direct;
          main.target = "_blank";
          main.rel = "noopener noreferrer";
        }
        const details = document.createElement("div");
        details.append(main);
        if (app.proxy) {
          const address = document.createElement("span");
          address.textContent = app.proxy;
          address.className = "proxy-address";
          details.append(address);
        }
        if (app.direct) {
          const direct = document.createElement("a");
          direct.href = app.direct;
          direct.textContent = (app.proxy ? "Direct: " : "") + app.direct;
          direct.className = "direct-link";
          direct.target = "_blank";
          direct.rel = "noopener noreferrer";
          details.append(direct);
        } else {
          const hint = document.createElement("small");
          hint.textContent = "Enter a server hostname/IP for direct links.";
          details.append(hint);
        }
        card.append(icon, details);
        links.append(card);
      }
      document.querySelector("#access-links").hidden = false;
      document.querySelector("#local-dns").hidden = !access.dashboard;
      document.querySelector("#hosts-entry").textContent = access.hostsEntry;
      const dashboard = document.querySelector("#dashboard-link");
      dashboard.href = access.dashboard || "#";
      dashboard.textContent = access.dashboard;
      output.textContent = current;
      error.hidden = true;
      error.textContent = "";
      copy.disabled = false;
    } catch (problem) {
      document.querySelector("#access-links").hidden = true;
      current = "";
      output.textContent = "Resolve the highlighted configuration message to generate your command.";
      error.textContent = problem.message;
      error.hidden = false;
      copy.disabled = true;
    }
  }
  form.addEventListener("input", update);
  form.addEventListener("change", update);
  form.addEventListener("reset", () => queueMicrotask(update));
  form.addEventListener("submit", event => event.preventDefault());
  copy.addEventListener("click", async () => {
    if (!current) return;
    const revision = copyRevision;
    try {
      if (!navigator.clipboard) throw new Error("Clipboard unavailable");
      await navigator.clipboard.writeText(current);
      if (revision === copyRevision) status.textContent = "Command copied.";
    } catch {
      if (revision !== copyRevision) return;
      const selection = window.getSelection();
      if (selection) {
        document.querySelector("#command-output").focus({ preventScroll: true });
        const range = document.createRange();
        range.selectNodeContents(output);
        selection.removeAllRanges();
        selection.addRange(range);
        status.textContent = "Clipboard access failed. Command selected; copy it with your keyboard.";
      } else {
        status.textContent = "Clipboard access failed. Select and copy the command above manually.";
      }
    }
  });
  update();
}

if (typeof document !== "undefined") initialize();
