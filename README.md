# HEARTH // 炉心監視システム

A sci-fi anime-styled homelab dashboard. It streams live telemetry from every machine on your LAN to your browser, keeps an archive of history, and has a kaomoji navigator who comments on what's happening.

| Panel | What it shows |
|---|---|
| Fleet strip | One card per machine showing whether it's online, plus CPU, memory, hottest temperature and container count. Click a card to switch the whole dashboard to that machine. |
| 01 CORE 炉心 | CPU load ring, per-core bars, memory, swap, load average, clock speed, CPU temperature |
| 02 NETWORK 通信 | Live RX/TX chart (120 s, hover for values) per interface, totals, TCP/UDP counts |
| 03 THERMAL 熱源 | Every hwmon sensor grouped by chip, with warn/critical thresholds and fans |
| 04 CONTAINERS 艦隊 | Each container's state, health, CPU, memory, network rate, compose project and ports |
| 05 STORAGE 記憶域 | Filesystem usage, a **days-until-full forecast**, and **S.M.A.R.T. drive health** |
| 06 ARCHIVE 履歴 | Stored history over **1H / 6H / 24H / 7D / 30D** (CPU, memory, temperature, network) plus a 24-hour summary |
| 07 NAVIGATOR 案内役 | KAGARI: alerts, recovery celebrations, daily report, optional sound effects |
| 08 NODES 近隣 | LAN neighbours from ARP (machines in your fleet are tagged) and active TCP peers |
| 09 SYSTEM LOG 記録 | Events from every machine. The log is stored, so it survives restarts. |

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
  - /dev/nvme0n1
  - /dev/sda
cap_add: [SYS_RAWIO, SYS_ADMIN]
```

List your drives with `lsblk -d -o NAME,SIZE,MODEL`. HEARTH reads wear level, spare blocks, media errors, reallocated and pending sectors, power-on hours and data written every 10 minutes. It flags problems in the log and through KAGARI. Without the passthrough, the panel says S.M.A.R.T. is unavailable and everything else still works.

The **days-until-full** forecast needs no extra setup. The hub records disk usage every 10 minutes and fits a trend line over the last 7 days. The trend shows after about 6 hours of samples.

## Archive

The hub stores one row per minute per machine in SQLite (`hearth-data` volume, `/data/hearth.db`), along with disk usage samples and the event log. Data older than `HEARTH_RETENTION_DAYS` (default 30) is pruned automatically. Back it up with:

```bash
docker run --rm -v hearth_hearth-data:/data -v "$PWD":/out alpine cp /data/hearth.db /out/
```

## Themes & KAGARI

- Five themes are available from the swatch button in the header: **HEARTH** 炉心, **REDLINE** 赤警報, **MECHA** 機甲, **SAKURA** 桜 and **ECLIPSE** 月蝕. Your choice is remembered per browser.
- Turn on **SFX** in the Navigator panel for synthesised alerts: an alarm for critical events, a blip for warnings, and a chime when something recovers.
- KAGARI reacts to overheating, silent machines, crashed or unhealthy containers, failing drives, full or filling disks, and recoveries. She also reads you the 24-hour report.

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

## How it sees the host

| Compose setting | Why |
|---|---|
| `network_mode: host`, `pid: host` | Shows the host's interfaces, TCP table, ARP neighbours and CPU |
| `/var/run/docker.sock:ro` | Container list, live stats and events. HEARTH only reads. |
| `/:/hostfs:ro` | Filesystem usage and the OS name |
| `security_opt: label=disable` | Lets the container use docker.sock on SELinux hosts (Fedora/RHEL) |

## Security notes

- The **dashboard itself has no login**, so keep it on your LAN or put it behind a reverse proxy with auth. It works under a sub-path.
- Never commit `.env`: it holds the agent token. The `.gitignore` already excludes it.

## Endpoints (hub)

`/` dashboard · `/ws` browser stream · `/agent` agent stream · `/api/fleet` · `/api/snapshot?node=` · `/api/history?node=&range=1h|6h|24h|7d|30d` · `/api/summary?node=&hours=24` · `/healthz`
