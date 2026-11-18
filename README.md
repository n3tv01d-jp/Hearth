<p align="center">
  <img src="assets/hearth.png" alt="HEARTH banner" width="100%">
</p>

# HEARTH // 炉心監視システム

A sci-fi anime-styled homelab dashboard. It streams live telemetry from every machine on your LAN to your browser, keeps an archive of history, and has a kaomoji navigator who comments on what's happening.

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
docker compose up -d --build      # → http://<host>:8088
```

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
docker compose -f docker-compose.agent.yml up -d --build
```

- **LAN only.** The hub accepts agents only from private LAN addresses (`10/8`, `172.16/12`, `192.168/16`, loopback, link-local, IPv6 ULA) that also present the shared token. Connections from other addresses, including Tailscale's `100.64/10` range, are refused and logged. You can override the allowed ranges with `HEARTH_AGENT_CIDRS`.
- **ARM works.** The base image is multi-arch, and building on a Raspberry Pi produces a native ARM image. The build stage compiles anything that has no prebuilt wheel for 32-bit Pi OS.
- **Agents only connect out.** Only the hub needs an open port. An agent's healthcheck port listens on `127.0.0.1` only.
- If an agent stops reporting for 15 s, it's marked **LOST** and the dashboard shows its last known data. When it comes back, KAGARI celebrates.
- An agent is named after its hostname unless you set `HEARTH_NODE`.

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

- Nine themes are available from the swatch button in the header: **HEARTH** 炉心, **REDLINE** 赤警報, **MECHA** 機甲, **SAKURA** 桜, **ECLIPSE** 月蝕, **KURENAI** 紅, **ABYSS** 深海, **AURUM** 黄金 and **SPECTER** 幽霊. Your choice is remembered per browser.
- Turn on **SFX** in the Navigator panel for synthesised alerts: an alarm for critical events, a blip for warnings, and a chime when something recovers.
- KAGARI reacts to overheating, silent machines, crashed or unhealthy containers, failing drives, full or filling disks, down beacons, and recoveries. She remembers things too: a container on its 3rd restart today, a quiet (or rough) night, a new uptime record, days without a warning. She reads you the daily and weekly reports.

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

## Prometheus

`/metrics` exposes everything HEARTH sees in Prometheus text format: per-node CPU, memory, load, temperatures, fans, network, disks and days-until-full, containers, S.M.A.R.T. and beacons, plus event counters. Add the hub as a scrape target:

```yaml
scrape_configs:
  - job_name: hearth
    static_configs: [{ targets: ["192.168.1.10:8088"] }]
```

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
| `HEARTH_NOTIFY` | — | hub | Push alert URLs (see *Push alerts*) |
| `HEARTH_NOTIFY_LEVEL` | `warn` | hub | Lowest level to push: `info`, `warn` or `crit` |
| `HEARTH_NOTIFY_RECOVERY` | `1` | hub | Also push recoveries |
| `HEARTH_NOTIFY_COOLDOWN` | `300` | hub | Seconds an identical alert is held back |
| `HEARTH_NOTIFY_BURST` | `10` | hub | Most alerts sent per minute |
| `HEARTH_CHECKS` | — | hub | Beacons (see *Beacons*) |
| `HEARTH_CHECK_INTERVAL` | `30` | hub | Seconds between beacon probes |
| `HEARTH_CHECK_TIMEOUT` | `3` | hub | Probe timeout in seconds |
| `HEARTH_METRICS` | `1` | hub | `0` disables `/metrics` |

## How it sees the host

| Compose setting | Why |
|---|---|
| `network_mode: host`, `pid: host` | Shows the host's interfaces, TCP table, ARP neighbours and CPU |
| `/var/run/docker.sock:ro` | Container list, live stats and events. HEARTH only makes read calls unless `HEARTH_ALLOW_CONTROL=1`. (`:ro` doesn't restrict the Docker API itself; the opt-in flag does.) |
| `/:/hostfs:ro` | Filesystem usage and the OS name |
| `security_opt: label=disable` | Lets the container use docker.sock on SELinux hosts (Fedora/RHEL) |

## Security notes

- The **dashboard itself has no login**, so keep it on your LAN or put it behind a reverse proxy with auth. It works under a sub-path. This matters more if you turn on container control.
- Never commit `.env`: it holds the agent token. The `.gitignore` already excludes it.

## Endpoints (hub)

`/` dashboard · `/ws` browser stream · `/agent` agent stream · `/api/fleet` · `/api/snapshot?node=` · `/api/history?node=&range=1h|6h|24h|7d|30d` · `/api/summary?node=&hours=24` · `/api/events?node=&range=` · `/api/report?node=&days=1-30` · `/api/insights?node=` · `/api/beacons` · `/api/info` · `/metrics` · `/healthz`

POST (need the `X-Hearth: 1` header): `/api/container` `{node, id, action: start|stop|restart|logs}` · `/api/notify/test`
