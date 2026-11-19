"""Prometheus text exposition (format 0.0.4) for /metrics: every node, sensor, disk, container, drive and beacon."""
from __future__ import annotations

import time

from . import __version__


def _esc(v) -> str:
    return str(v).replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


class Families:
    def __init__(self) -> None:
        self.f: dict[str, tuple[str, str, list[str]]] = {}

    def add(self, name: str, typ: str, doc: str, value, /, **labels) -> None:
        if value is None:
            return
        if isinstance(value, bool):
            value = int(value)
        fam = self.f.setdefault(name, (typ, doc, []))
        lbl = ",".join(f'{k}="{_esc(v)}"' for k, v in labels.items() if v is not None)
        fam[2].append(f"{name}{{{lbl}}} {value}" if lbl else f"{name} {value}")

    def text(self) -> str:
        out: list[str] = []
        for name, (typ, doc, lines) in self.f.items():
            out += [f"# HELP {name} {doc}", f"# TYPE {name} {typ}", *lines]
        return "\n".join(out) + "\n"


def render(hub) -> str:  # noqa: C901 (one flat walk over the snapshot)
    m = Families()
    G, C = "gauge", "counter"
    m.add("hearth_info", G, "HEARTH build information", 1, version=__version__, hub=hub.local.id)
    now = time.monotonic()
    for n in list(hub.nodes.values()):
        node, s = n.id, n.latest or {}
        m.add("hearth_node_up", G, "1 while the node is reporting", n.online, node=node)
        if n.last_seen:
            m.add("hearth_node_last_seen_seconds", G, "Seconds since the node last reported",
                  round(now - n.last_seen, 1), node=node)
        if not s:
            continue
        host = s.get("host") or {}
        m.add("hearth_uptime_seconds", G, "Host uptime", host.get("uptime"), node=node)
        cpu = s.get("cpu") or {}
        m.add("hearth_cpu_percent", G, "Total CPU utilisation", cpu.get("total"), node=node)
        for i, v in enumerate(cpu.get("cores") or []):
            m.add("hearth_cpu_core_percent", G, "Per-core CPU utilisation", v, node=node, core=i)
        for per, v in zip(("1m", "5m", "15m"), cpu.get("load") or []):
            m.add("hearth_load_average", G, "Load average", v, node=node, period=per)
        m.add("hearth_cpu_frequency_mhz", G, "Current CPU clock", cpu.get("mhz"), node=node)
        mem = s.get("mem") or {}
        m.add("hearth_memory_used_bytes", G, "Memory in use", mem.get("used"), node=node)
        m.add("hearth_memory_total_bytes", G, "Total memory", mem.get("total"), node=node)
        m.add("hearth_memory_percent", G, "Memory utilisation", mem.get("pct"), node=node)
        m.add("hearth_swap_used_bytes", G, "Swap in use", mem.get("swap_used"), node=node)
        m.add("hearth_swap_total_bytes", G, "Total swap", mem.get("swap_total"), node=node)
        for t in s.get("temps") or []:
            m.add("hearth_temperature_celsius", G, "hwmon sensor temperature", t.get("c"),
                  node=node, chip=t.get("chip"), sensor=t.get("label"))
        for f in s.get("fans") or []:
            m.add("hearth_fan_rpm", G, "Fan speed", f.get("rpm"), node=node, chip=f.get("chip"), fan=f.get("label"))
        net = s.get("net") or {}
        for i in net.get("ifaces") or []:
            lb = {"node": node, "iface": i.get("name"), "kind": i.get("kind")}
            m.add("hearth_network_up", G, "Interface link state", i.get("up"), **lb)
            m.add("hearth_network_receive_bytes_per_second", G, "Receive rate", i.get("rx"), **lb)
            m.add("hearth_network_transmit_bytes_per_second", G, "Transmit rate", i.get("tx"), **lb)
            m.add("hearth_network_receive_bytes_total", C, "Bytes received", i.get("total_rx"), **lb)
            m.add("hearth_network_transmit_bytes_total", C, "Bytes sent", i.get("total_tx"), **lb)
        conns = net.get("conns") or {}
        m.add("hearth_tcp_established", G, "Established TCP connections", conns.get("established"), node=node)
        for d in s.get("disks") or []:
            lb = {"node": node, "mount": d.get("mount"), "fs": d.get("fs"), "device": d.get("dev")}
            m.add("hearth_disk_used_bytes", G, "Filesystem bytes used", d.get("used"), **lb)
            m.add("hearth_disk_free_bytes", G, "Filesystem bytes available", d.get("free"), **lb)
            m.add("hearth_disk_total_bytes", G, "Filesystem size", d.get("total"), **lb)
            fc = n.forecast.get(d.get("mount")) or {}
            m.add("hearth_disk_full_eta_days", G, "Days until full at the 7-day growth rate", fc.get("eta_days"),
                  node=node, mount=d.get("mount"))
        dio = s.get("diskio") or {}
        m.add("hearth_disk_read_bytes_per_second", G, "Whole-disk read rate", dio.get("r"), node=node)
        m.add("hearth_disk_write_bytes_per_second", G, "Whole-disk write rate", dio.get("w"), node=node)
        dk = s.get("docker") or {}
        m.add("hearth_docker_up", G, "1 if the Docker engine is reachable", dk.get("online"), node=node)
        for c in dk.get("containers") or []:
            lb = {"node": node, "name": c.get("name"), "project": c.get("project")}
            m.add("hearth_container_running", G, "1 if the container is running", c.get("state") == "running", **lb)
            if c.get("health"):
                m.add("hearth_container_healthy", G, "1 if the healthcheck passes", c.get("health") == "healthy", **lb)
            if c.get("state") == "running":
                m.add("hearth_container_cpu_percent", G, "Container CPU (100 = one core)", c.get("cpu"), **lb)
                m.add("hearth_container_memory_bytes", G, "Container memory", c.get("mem"), **lb)
        for d in (s.get("smart") or {}).get("drives") or []:
            if d.get("error"):
                continue
            lb = {"node": node, "device": d.get("dev"), "model": d.get("model")}
            m.add("hearth_smart_healthy", G, "1 if S.M.A.R.T. reports no issues", d.get("lvl") == "ok", **lb)
            m.add("hearth_smart_wear_percent", G, "Drive wear", d.get("wear"), **lb)
            m.add("hearth_smart_temperature_celsius", G, "Drive temperature", d.get("temp"), **lb)
            m.add("hearth_smart_power_on_hours", C, "Drive power-on hours", d.get("hours"), **lb)
            m.add("hearth_smart_written_bytes", C, "Bytes written over the drive's life", d.get("written"), **lb)
        ars = s.get("arsenal") or {}
        if ars:
            ups = ars.get("updates") or []
            m.add("hearth_image_updates_available", G, "Containers whose image has a newer version (ARSENAL)",
                  sum(1 for u in ups if u.get("state") in ("update", "pulled")), node=node)
            for u in ups:
                if u.get("state") in ("update", "pulled", "current"):
                    m.add("hearth_container_update_available", G, "1 if a newer image exists for the container",
                          u["state"] != "current", node=node, name=u.get("name"), image=u.get("image"))
            host_m = ars.get("host") or {}
            if host_m:
                m.add("hearth_reboot_required", G, "1 if the host needs a reboot (new kernel or OS flag)", host_m.get("reboot"), node=node)
                m.add("hearth_os_updates_pending", G, "Pending OS package updates, where the OS reports them", host_m.get("pending"), node=node)
            for b in ars.get("backups") or []:
                lb = {"node": node, "name": b.get("name"), "kind": b.get("kind")}
                m.add("hearth_backup_last_success_timestamp_seconds", G, "When the backup last produced data",
                      b["last"] / 1000 if b.get("last") else None, **lb)
                m.add("hearth_backup_ok", G, "1 if the backup is within its max age", b.get("lvl") == "ok", **lb)
    for hb in hub.ops.heartbeats():
        m.add("hearth_heartbeat_last_ping_timestamp_seconds", G, "Last successful ping from a backup job",
              hb["last"] / 1000 if hb.get("last") else None, name=hb["name"])
        m.add("hearth_heartbeat_ok", G, "1 if the job pinged within its max age", hb.get("lvl") == "ok", name=hb["name"])
    m.add("hearth_maintenance_active", G, "Open maintenance windows (push alerts muted)", len(hub.ops.active()))
    for e in hub.gate.services()["services"]:
        if (e.get("cert") or {}).get("until"):
            m.add("hearth_cert_expiry_timestamp_seconds", G, "TLS certificate notAfter of a GATE service",
                  e["cert"]["until"] / 1000, name=e["name"], url=e["url"].replace("__HOST__", "hub"))
    for b in hub.beacons.snapshot():
        lb = {"name": b["name"], "kind": b["kind"], "target": b["target"]}
        m.add("hearth_beacon_up", G, "1 if the beacon answers", None if b["up"] is None else b["up"], **lb)
        m.add("hearth_beacon_rtt_ms", G, "Last probe round-trip time", b["rtt"], **lb)
        m.add("hearth_beacon_uptime_ratio", G, "Share of successful probes over 24 h", b["uptime"], **lb)
    for (lvl, src), v in sorted(hub.ev_counts.items()):
        m.add("hearth_events_total", C, "Events logged since the hub started", v, level=lvl, source=src)
    return m.text()
