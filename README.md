<p align="center">
  <img src="assets/hearth.png" alt="HEARTH banner" width="100%">
</p>

# HEARTH // 炉心監視システム

A sci-fi anime-styled homelab dashboard. It streams live telemetry from every machine on your LAN to your browser, keeps an archive of history, and has a kaomoji navigator who comments on what's happening.

The start page is a **system select** with four slots. The first three are served by the same hub and fed by the same agents:

| Slot | System | What it's for |
|---|---|---|
| 01 | **HEARTH** 炉心 | Watch: live telemetry, archive, alerts, KAGARI (`hearth.html`) |
| 02 | **GATE** 転送門 | Go: a start page for every web UI in the fleet, built automatically (`gate.html`) |
| 03 | **ARSENAL** 工廠 | Fix: image updates, reboots, disk clean-up, backups, maintenance windows, change journal (`arsenal.html`) |
| 04 | **RYUCORE** 竜核 | Think: a link to your [RyuCore](https://github.com/n3tv01d-jp/RyuCore) AI command core. The first click opens a setup dialog for its URL (stored on the hub); after that the card launches it, and the ⚙ button edits or unlinks it |

| Panel | What it shows |
|---|---|
| Fleet strip | One card per machine showing whether it's online, plus CPU, memory, hottest temperature and container count. Click a card to switch the whole dashboard to that machine. |
| 01 CORE 炉心 | CPU load ring, per-core bars, memory, swap, load average, clock speed, CPU temperature |
| 02 NETWORK 通信 | Live RX/TX chart (120 s, hover for values) per interface, totals, TCP/UDP counts |
| 03 THERMAL 熱源 | Every hwmon sensor grouped by chip, with warn/critical thresholds and fans |
| 04 CONTAINERS 艦隊 | Each container's state, health, CPU, memory, network rate, compose project and ports. With control enabled: **restart / stop / start / logs** |
| 05 STORAGE 記憶域 | Filesystem usage, a **days-until-full forecast**, and **S.M.A.R.T. drive health** |
| 06 ARCHIVE 履歴 | Stored history over **1H / 6H / 24H / 7D / 30D** (CPU, memory, temperature, network) plus a 24-hour summary. **Event markers** show when things went wrong; **VS** overlays a second machine |
| 07 NAVIGATOR 案内役 | KAGARI: alerts, recovery celebrations, daily and weekly reports, restart streaks, quiet nights, uptime records, optional sound effects |
| 08 NODES 近隣 | LAN neighbours from ARP (machines in your fleet are tagged) and active TCP peers |
| 09 SYSTEM LOG 記録 | Events from every machine. The log is stored, so it survives restarts. **Click an entry** to jump to its source. |
| 10 BEACONS 監視点 | Ping / TCP / HTTP checks for devices that can't run an agent (router, printer, IoT, web apps), with latency sparklines and 24 h uptime. Only shown when `HEARTH_CHECKS` is set. |

The **☰ menu** in the top-right corner opens the *command deck*: jump to any panel (with live status lights), collapse or expand panels, switch machines, open the 24 h / weekly / 30-day **reports**, export the archive as CSV, check push-alert status and send a test alert, pick a theme, toggle sound, the boot intro and stopped containers, and see what's enabled on the hub.

## Quick start (single machine)

```bash
cp .env.example .env              # then set HEARTH_TOKEN (openssl rand -hex 24)
docker compose pull               # the published image (ghcr.io/n3tv01d-jp/hearth)
docker compose up -d              # → http://<host>:8088
```

`docker compose up -d --build` builds from your checkout instead. HEARTH can only update itself from the published image (see *Updating*).

URL options: `?boot=0` skips the intro, `?theme=sakura` picks a theme, and `?node=nas` opens a specific machine.

## Multiple machines (agents)

One machine runs the **hub** (the dashboard). Every other machine runs a small **agent** that pushes its telemetry to the hub over a WebSocket.

```
 nas  ── agent ──┐
 pi   ── agent ──┼──▶  hub (dashboard + archive)  ◀── browser
 mini ── agent ──┘
```

On each additional machine:

```bash
git clone <this repo> && cd hearth
cp .env.example .env     # same HEARTH_TOKEN as the hub; HEARTH_HUB=http://<hub-lan-ip>:8088
docker compose -f docker-compose.agent.yml pull
docker compose -f docker-compose.agent.yml up -d
```

- **LAN only.** The hub accepts agents only from private LAN addresses (`10/8`, `172.16/12`, `192.168/16`, loopback, link-local, IPv6 ULA) that also present the shared token. Connections from other addresses, including Tailscale's `100.64/10` range, are refused and logged. You can override the allowed ranges with `HEARTH_AGENT_CIDRS`.
- **ARM works.** The published image is built for x86_64, arm64 (Pi 4/5) and armv7 (32-bit Pi OS). If you build it yourself on a Raspberry Pi you get a native ARM image. The build stage compiles anything that has no prebuilt wheel for 32-bit Pi OS.
- **Agents only connect out.** Only the hub needs an open port. An agent's healthcheck port listens on `127.0.0.1` only.
- If an agent stops reporting for 15 s, it's marked **LOST** and the dashboard shows its last known data. When it comes back, KAGARI celebrates.
- An agent is named after its hostname unless you set `HEARTH_NODE`.

## Updating

Every push to `main` makes GitHub Actions build the image and publish it as `ghcr.io/n3tv01d-jp/hearth:latest` (`.github/workflows/image.yml`). Tags like `v3.4.0` also publish `:3.4.0` and `:3.4`. ARSENAL checks the registry like it does for your other containers, and HEARTH's own row says **UPDATE AVAILABLE** when there's a new build.

Press **⬆ UPDATE HEARTH** on that row, for the hub or for any agent. That machine:

1. Pulls the new image.
2. Starts a short-lived helper container (`hearth-updater-…`) that has only the Docker socket.
3. The helper stops HEARTH, creates a copy on the new image with exactly the same settings (env, volumes, devices, network, restart policy, compose labels) and waits up to 150 s for it to report healthy.
4. If it crashes or stays unhealthy, the helper removes it and starts the old container again.

Whichever HEARTH runs afterwards writes the result to the event log, e.g. `HEARTH updated: 3.3.1 → 3.4.0` or `self-update failed, still on 3.3.1: …`. The hub's own page reconnects by itself. Each machine card in ARSENAL shows the HEARTH version it runs.

- It's on by default in the compose files (`HEARTH_SELF_UPDATE=1`), and `HEARTH_ALLOW_CONTROL=1` implies it. Set `HEARTH_SELF_UPDATE=0` to turn the button off on a machine.
- It can only ever pull HEARTH's own image and replace HEARTH's own container.
- `HEARTH_IMAGE` picks the image, for example `ghcr.io/n3tv01d-jp/hearth:3.4` to get only 3.4.x builds.
- A container built locally with `--build` shows **LOCAL BUILD**. UPDATE HEARTH then replaces it with the published image.
- By hand it's still `docker compose pull && docker compose up -d`.

The first time the workflow runs, check that the package's visibility is **Public** on GitHub (your profile → *Packages* → *hearth* → *Package settings*). Otherwise machines can't pull it without logging in.

## Drive health (S.M.A.R.T.)

smartctl needs the drives passed into the container. In the compose file you use (hub or agent), uncomment and adjust:

```yaml
devices:
  - /dev/nvme0      # NVMe: the controller, not nvme0n1 or a partition
  - /dev/sda        # SATA/USB: the whole disk, not sda1
cap_add: [SYS_RAWIO, SYS_ADMIN]
```

List your drives with `lsblk -d -o NAME,SIZE,MODEL`. For an NVMe drive shown as `nvme0n1`, pass `/dev/nvme0`: `smartctl --scan` finds NVMe drives through the controller device and skips namespaces and partitions. HEARTH reads wear level, spare blocks, media errors, reallocated and pending sectors, power-on hours and data written every 10 minutes. It flags problems in the log and through KAGARI. Without the passthrough, the panel says S.M.A.R.T. is unavailable and everything else still works.

The **days-until-full** forecast needs no extra setup. The hub records disk usage every 10 minutes and fits a trend line over the last 7 days. The trend shows after about 6 hours of samples.

## Archive

The hub stores one row per minute per machine in SQLite (`hearth-data` volume, `/data/hearth.db`), along with disk usage samples and the event log. Data older than `HEARTH_RETENTION_DAYS` (default 30) is pruned automatically. Back it up with:

```bash
docker run --rm -v hearth_hearth-data:/data -v "$PWD":/out alpine cp /data/hearth.db /out/
```

## Themes & KAGARI

- Nine themes are available from the swatch button in the header: **HEARTH** 炉心, **REDLINE** 赤警報, **MECHA** 機甲, **SAKURA** 桜, **ECLIPSE** 月蝕, **KURENAI** 紅, **ABYSS** 深海, **AURUM** 黄金 and **SPECTER** 幽霊.
- **One theme for the whole fleet.** The theme you pick is stored on the hub and every open HEARTH page follows it (the dashboard at once, GATE and ARSENAL within a minute), and so do RyuCore, KuroDeck and neosh when they're linked (see *Theme pack*). `?theme=NAME` still overrides it for one page.
- Turn on **SFX** in the Navigator panel for synthesised alerts: an alarm for critical events, a blip for warnings, and a chime when something recovers.
- KAGARI reacts to overheating, silent machines, crashed or unhealthy containers, failing drives, full or filling disks, down beacons, and recoveries. She remembers things too: a container on its 3rd restart today, a quiet (or rough) night, a new uptime record, days without a warning. She reads you the daily and weekly reports.

## Theme pack

The hub is the one source of the look for the HEARTH family. `GET /api/theme` serves the fleet theme, every palette and KAGARI's shared greetings and idle lines, plus a `rev` hash that changes whenever any of it does. `?fmt=text` gives the same as `key=value` lines for shells.

To change colours, add palettes or rewrite KAGARI's shared lines, put a `pack.json` next to the database (or point `HEARTH_PACK` at one). It's re-read within 30 s of a change:

```json
{ "palettes": [
    { "id": "hearth", "a1": "#40f0ff" },
    { "id": "neon", "name": "NEON", "jp": "電光", "a1": "#ff00aa", "a2": "#00ffcc", "a3": "#ffee00" } ],
  "kagari": {
    "greetings": [ { "until": 12, "kao": "(・ω・)ノ", "en": "Morning! It's {time} on the {where}.", "jp": "おはよう" },
                   { "until": 24, "kao": "(´｡• ᵕ •｡`)", "en": "Evening, commander.", "jp": "こんばんは" } ],
    "idle": [ { "kao": "(｀・ω・´)ゞ", "en": "Standing watch!", "jp": "監視中" } ] } }
```

A palette with a built-in id changes only the colours you give. A new id (`a-z0-9_`, up to 32 of them) needs `a1`–`a3`; its backgrounds and text colours are derived from `a1` unless you set `surface bg bg2 panel text dim faint`. Greetings are picked by hour (`until` is exclusive); `{time}` and `{where}` (network / shell / deck) are filled in by each client. Anything invalid is skipped with a warning in the log.

## RyuCore 竜核

Link [RyuCore](../RyuCore) from the start page and HEARTH gets **竜 ASK** buttons on warnings and critical events in the log, on the daily/weekly report and on each ARSENAL machine card. They open RyuCore in a new tab with a ready-made agent prompt about that event, report or machine. Nothing is sent until you press TRANSMIT there.

Going the other way, start RyuCore with `RYU_HEARTH_URL` (shown in the link dialog) and it adds HEARTH as an MCP server, so its agents can read the fleet, metrics, events, reports, insights, maintenance state and journal.

### MCP server

`POST /mcp` is a [Model Context Protocol](https://modelcontextprotocol.io) server (Streamable HTTP, stateless). Any MCP client can use it.

| `HEARTH_MCP` | Tools |
|---|---|
| `read` (default) | `fleet_status` `node_snapshot` `metric_history` `events` `report` `insights` `maintenance_overview` `journal` (+ `container_logs` when container control is on) |
| `control` | Also `open_maintenance` and `add_journal_note`, plus `container_action`, `stack_action` and `update_container`, which are marked destructive so a client like RyuCore asks you before running them. Needs both `HEARTH_MCP_TOKEN` and `HEARTH_ALLOW_CONTROL=1`, otherwise it stays `read` |
| `off` | `/mcp` returns 404 |

With `HEARTH_MCP_TOKEN` set, clients must send `Authorization: Bearer <token>`. Requests must be `Content-Type: application/json`, so a web page can't post to it cross-site. Every write made through MCP is written to the event log.

## Push alerts

KAGARI can reach you when the dashboard isn't open. Set `HEARTH_NOTIFY` on the hub to one or more URLs, separated by commas:

```bash
HEARTH_NOTIFY=https://ntfy.sh/my-secret-topic                               # ntfy
HEARTH_NOTIFY=https://gotify.lan/message?token=AbC123                       # Gotify
HEARTH_NOTIFY=https://discord.com/api/webhooks/123/xyz                      # Discord
HEARTH_NOTIFY=https://hooks.slack.com/services/T000/B000/xyz                # Slack
HEARTH_NOTIFY=webhook+https://n8n.lan/webhook/hearth                        # any JSON webhook
```

The service is worked out from the URL. To force it, add a `ntfy+`, `gotify+`, `discord+`, `slack+` or `webhook+` prefix (useful for a self-hosted ntfy on a host without "ntfy" in its name). `user:pass@` in a URL is sent as basic auth.

Warnings, criticals and recoveries are sent from every machine in the fleet. Identical alerts (same machine, source and message, ignoring numbers) are held back for 5 minutes, and at most 10 go out per minute, so a flapping container can't flood your phone. The command deck shows each target's status and has a **SEND TEST ALERT** button.

## Beacons

Watch things that can't run an agent. Set `HEARTH_CHECKS` on the hub:

```bash
HEARTH_CHECKS="router=192.168.1.1, printer=tcp:192.168.1.50:631, homeassistant=http://192.168.1.20:8123"
```

| Form | Check |
|---|---|
| `name=HOST` or `name=ping:HOST` | ICMP ping |
| `name=tcp:HOST:PORT` | TCP connect |
| `name=http(s)://…` | HTTP GET. Any response below 500 counts as up, and self-signed certificates are accepted. |

Beacons are probed every 30 s and marked **DOWN** after two failures in a row. Down and back-up events go to the log, KAGARI and push alerts. Results are archived, so the panel and reports show uptime. Beacon IPs are tagged in the NODES list.

## Container control

Off by default. Set `HEARTH_ALLOW_CONTROL=1` on the **hub** to get ↻ RESTART, ■ STOP, ▶ START and ≡ LOGS buttons on container cards. To control containers on an agent machine, also set `HEARTH_ALLOW_CONTROL=1` on **that agent**. Each machine decides for itself, and the hub forwards requests over the agent's existing link (no extra ports).

- Stop and restart ask for confirmation. Every action is written to the system log along with the address it came from.
- HEARTH refuses to stop or restart its own container.
- Requests need an `X-Hearth: 1` header, which browsers won't send cross-site, so another website can't trigger them.
- **The dashboard has no login.** With control on, anyone who can open it can stop your containers. Keep it on a trusted LAN or behind an authenticating reverse proxy.

## GATE 転送門 — service portal

GATE builds a launcher for every web UI in the fleet, with no setup:

- **Containers with published ports** on any machine become tiles that link to `http://<machine>:<port>`. GATE guesses which port is the web UI (443 / 80 / common app ports first; it ignores 22, 53, 5432 and other non-web ports) and shows the remaining ports as small chips.
- **Domains from your reverse proxy**: Traefik `Host(`…`)` router rules and caddy-docker-proxy `caddy` labels become `https://` links.
- **HTTP beacons** from `HEARTH_CHECKS`, and **bookmarks** from `HEARTH_GATE_LINKS` or added on the page with **＋ LINK**.

Each tile shows a live status light (running, unhealthy, stopped, machine offline), the site's own **favicon** (fetched by the hub, so the page loads nothing cross-origin), the machine it runs on, and a **certificate badge** for HTTPS services that expire within 14 days. A stopped container keeps its tile because GATE remembers its last ports. Certificate warnings go to the log, KAGARI and push alerts.

**Search:** press `/` or just start typing. Matching is fuzzy (`jlf` finds jellyfin) and ranks services you open often higher. `Enter` opens the selection in a new tab and `Shift+Enter` opens it in the current one. Arrow keys move between tiles, `E` edits, `P` pins to **Favourites**, `N` adds a bookmark, and `?` lists every key.

**Customise** a tile from its `⋯` button (name, URL, group, icon, pin, hide; these are stored on the hub, so everyone sees them) or with container labels:

```yaml
labels:
  hearth.gate.name: Jellyfin
  hearth.gate.group: Media
  hearth.gate.port: "8096"        # needed for containers on host networking (they publish no ports)
  hearth.gate.scheme: https
  hearth.gate.path: /web
  hearth.gate.url: https://jelly.example.com   # or set the whole link
  hearth.gate.icon: 🎬            # emoji, or an image URL
  hearth.gate.hide: "true"
```

Links point at the agent's LAN IP, or at the address you opened the hub with for the hub's own containers. Use `HEARTH_GATE_HOSTS=nas=nas.lan` to link by name instead.

**MACHINES · wake-on-LAN:** the MAC of each agent's primary interface is learned while it's online. When the machine goes offline, **⏻ WAKE** sends a magic packet from the hub. You can add machines without an agent too: `HEARTH_WOL=desktop=aa:bb:cc:dd:ee:ff` (append `@192.168.1.255` for a specific broadcast address). If a beacon has the same name, it supplies the machine's online status.

## ARSENAL 工廠 — maintenance bay

ARSENAL runs on every machine (in the hub and in each agent) and reports in the same snapshot, so it needs no extra ports.

| Panel | What it does |
|---|---|
| 01 UPDATES 更新 | Compares each container's image with its registry every 6 h (`HEARTH_UPDATE_INTERVAL`), using anonymous token auth (Docker Hub, GHCR, lscr.io, Quay and other v2 registries). Lists **update available** and **new image pulled, needs recreate**. **⬆ UPDATE** pulls the image and recreates the container with exactly the same settings (env, labels, volumes including anonymous ones, ports, networks and aliases). If any step fails, the old container is put back. Local builds, pinned digests and HEARTH itself are skipped. |
| 02 MACHINES 整備 | **Reboot required** (Debian/Ubuntu's flag file, or a newer kernel installed than the one running, on any distro), pending OS updates where the OS exposes a count (Ubuntu), when packages last changed, and Docker disk usage. **CLEAN UP** prunes dangling images, all unused images, stopped containers or the build cache. Volumes are never pruned. |
| 03 BACKUPS 保管庫 | Files: `HEARTH_BACKUPS=photos=/mnt/backup/restic@26h, dumps=/srv/dumps/*.sql.gz@2d`, set on the machine that holds them. restic and borg repositories are recognised; anything else uses the newest file's age. Jobs that run elsewhere use **heartbeats**: `HEARTH_HEARTBEATS=offsite=26h` on the hub, then `curl -fsS http://hub:8088/api/ping/offsite` at the end of the job (`…/fail` on errors; LAN addresses only). Late or failed backups alert through the log, KAGARI and push. |
| 04 MAINTENANCE 保守 | Opens a window for one machine or the whole fleet (30 min to 8 h). While it's open, **push alerts are muted**, HEARTH shows a banner and KAGARI stops sounding alarms. Everything is still logged. |
| 05 STACKS 編成 | Starts, stops or restarts every container of a compose project (HEARTH's own container is skipped). |
| 06 JOURNAL 航海日誌 | A change log ("swapped the NAS PSU", "Postgres 15 → 16"). Notes are kept forever and show as **violet markers on HEARTH's archive charts**, so you can match a change to the graphs. A maintenance window's reason is logged here automatically. |

Checks, maintenance windows, heartbeats and the journal always work. **Update, clean-up and stack actions need `HEARTH_ALLOW_CONTROL=1`** on the hub and on each machine you want to act on, the same opt-in as container control. Every action is written to the system log along with the address it came from.

## Prometheus

`/metrics` exposes everything HEARTH sees in Prometheus text format: per-node CPU, memory, load, temperatures, fans, network, disks and days-until-full, containers, S.M.A.R.T. and beacons, plus event counters. Add the hub as a scrape target:

```yaml
scrape_configs:
  - job_name: hearth
    static_configs: [{ targets: ["192.168.1.10:8088"] }]
```

GATE and ARSENAL add `hearth_container_update_available`, `hearth_image_updates_available`, `hearth_reboot_required`, `hearth_os_updates_pending`, `hearth_backup_ok`, `hearth_backup_last_success_timestamp_seconds`, `hearth_heartbeat_ok`, `hearth_maintenance_active` and `hearth_cert_expiry_timestamp_seconds`.

Turn it off with `HEARTH_METRICS=0`.

## Keyboard

| Key | Action |
|---|---|
| `1` … `9`, `0` | Jump to panel 01 … 10 |
| `[` `]` | Previous / next machine |
| `T` / `Shift+T` | Next / previous theme |
| `R` | Cycle the archive range |
| `/` | Filter containers |
| `M` | Mute / unmute SFX |
| `W` | Weekly report |
| `S` | Open / close the command deck |
| `?` | Shortcut help |
| `Esc` | Close whatever is open |

## Configuration

| Var | Default | Mode | |
|---|---|---|---|
| `HEARTH_MODE` | `hub` | both | `hub` or `agent` |
| `HEARTH_TOKEN` | — | both | Shared secret. Leave it empty on the hub to disable agents. |
| `HEARTH_HUB` | — | agent | The hub's address, e.g. `http://192.168.1.10:8088` |
| `HEARTH_NODE` | hostname | both | Display name / node id |
| `HEARTH_PORT` | `8088` / `8089` | both | Listen port (hub / agent) |
| `HEARTH_BIND` | `0.0.0.0` / `127.0.0.1` | both | Listen address |
| `HEARTH_RETENTION_DAYS` | `30` | hub | How long the archive keeps data |
| `HEARTH_LOST_AFTER` | `15` | hub | Seconds of silence before a machine is marked LOST |
| `HEARTH_AGENT_CIDRS` | LAN ranges | hub | Comma-separated CIDRs allowed to connect as agents |
| `HEARTH_SMART_INTERVAL` | `600` | both | Seconds between S.M.A.R.T. scans |
| `HEARTH_INTERVAL` | `1` | both | Seconds between samples |
| `HEARTH_IFACE_EXCLUDE` | `lo,veth` | both | Interface name prefixes to hide |
| `HEARTH_ALLOW_CONTROL` | `0` | both | `1` enables container start / stop / restart / logs on that machine |
| `HEARTH_SELF_UPDATE` | `1` in compose | both | ARSENAL's UPDATE HEARTH for that machine (see *Updating*) |
| `HEARTH_IMAGE` | `ghcr.io/n3tv01d-jp/hearth:latest` | both | The image compose runs, and that HEARTH updates itself from |
| `HEARTH_NOTIFY` | — | hub | Push alert URLs (see *Push alerts*) |
| `HEARTH_NOTIFY_LEVEL` | `warn` | hub | Lowest level to push: `info`, `warn` or `crit` |
| `HEARTH_NOTIFY_RECOVERY` | `1` | hub | Also push recoveries |
| `HEARTH_NOTIFY_COOLDOWN` | `300` | hub | Seconds an identical alert is held back |
| `HEARTH_NOTIFY_BURST` | `10` | hub | Most alerts sent per minute |
| `HEARTH_CHECKS` | — | hub | Beacons (see *Beacons*) |
| `HEARTH_CHECK_INTERVAL` | `30` | hub | Seconds between beacon probes |
| `HEARTH_CHECK_TIMEOUT` | `3` | hub | Probe timeout in seconds |
| `HEARTH_METRICS` | `1` | hub | `0` disables `/metrics` |
| `HEARTH_GATE_LINKS` | — | hub | GATE bookmarks: `name=url, …` |
| `HEARTH_GATE_HOSTS` | — | hub | Host names for GATE links: `node=host, …` |
| `HEARTH_WOL` | — | hub | Wake-on-LAN targets without an agent: `name=mac[@broadcast], …` |
| `HEARTH_CERT_WARN_DAYS` | `14` | hub | Warn when a GATE service's certificate expires within this many days |
| `HEARTH_UPDATE_INTERVAL` | `21600` | both | Seconds between image update checks (minimum 600) |
| `HEARTH_BACKUPS` | — | both | Backups on that machine: `name=/path[@max-age], …` |
| `HEARTH_HEARTBEATS` | — | hub | Heartbeat checks: `name=max-age, …` |
| `HEARTH_MCP` | `read` | hub | MCP server: `off`, `read` or `control` (see *MCP server*) |
| `HEARTH_MCP_TOKEN` | — | hub | Bearer token for `/mcp`. Required for `control` |
| `HEARTH_PACK` | `pack.json` next to the DB | hub | Theme pack file (see *Theme pack*) |

## How it sees the host

| Compose setting | Why |
|---|---|
| `network_mode: host`, `pid: host` | Shows the host's interfaces, TCP table, ARP neighbours and CPU |
| `/var/run/docker.sock:ro` | Container list, live stats and events. HEARTH only makes read calls unless `HEARTH_ALLOW_CONTROL=1`. (`:ro` doesn't restrict the Docker API itself; the opt-in flag does.) |
| `/:/hostfs:ro` | Filesystem usage and the OS name |
| `security_opt: label=disable` | Lets the container use docker.sock on SELinux hosts (Fedora/RHEL) |

## Security notes

- The **dashboard itself has no login**, so keep it on your LAN or put it behind a reverse proxy with auth. It works under a sub-path. This matters more if you turn on container control, because then anyone who can open HEARTH or ARSENAL can stop, update or prune containers.
- GATE's edits, maintenance windows and journal notes don't need control, but they are visible to and editable by everyone who can open the pages.
- ARSENAL makes outbound HTTPS requests to the registries of the images you run (one manifest lookup per image per check). GATE's hub fetches favicons and TLS certificates from the services it links to.
- With self-update on, anyone who can open HEARTH can make a machine pull the newest published HEARTH image and restart on it. They can't choose a different image, and a release that doesn't start is rolled back. The helper container gets the Docker socket, like HEARTH itself.
- `/mcp` is read-only by default. Without `HEARTH_MCP_TOKEN` anyone on the LAN who can open HEARTH can also read it over MCP (the same data as the dashboard). Set a token if that's a concern, and always for `control`.
- Anyone who can open HEARTH can change the fleet theme, which changes it on linked RyuCore, KuroDeck and neosh too. Only colours and KAGARI's lines travel this way.
- Never commit `.env`: it holds the agent token. The `.gitignore` already excludes it.

## Endpoints (hub)

`/` system select · `/hearth.html` · `/gate.html` · `/arsenal.html` · `/ws` browser stream · `/agent` agent stream · `/api/fleet` · `/api/snapshot?node=` · `/api/history?node=&range=1h|6h|24h|7d|30d` · `/api/summary?node=&hours=24` · `/api/events?node=&range=` · `/api/report?node=&days=1-30` · `/api/insights?node=` · `/api/beacons` · `/api/info` · `/api/gate` · `/api/gate/icon?id=` · `/api/arsenal` · `/api/ryucore` · `/api/theme[?fmt=text]` · `/api/status` (fleet, last hour's warnings and recoveries, maintenance, theme ref) · `/mcp` · `/api/journal?node=` · `/api/ping/<name>[/fail]` · `/metrics` · `/healthz`

POST (need the `X-Hearth: 1` header): `/api/container` `{node, id, action: start|stop|restart|logs}` · `/api/notify/test` · `/api/gate/item` `{id?, name, url, group, icon, pinned, hidden}` · `/api/gate/item/delete` `{id}` · `/api/gate/wake` `{id}` · `/api/arsenal/action` `{node, action: check|update|prune|stack|selfupdate, id?, arg?}` · `/api/maint` `{node?, minutes, reason}` · `/api/maint/stop` `{id}` · `/api/journal` `{text, node?}` · `/api/journal/delete` `{id}` · `/api/ryucore` `{url, newtab}` (empty url unlinks) · `/api/theme` `{theme}`
