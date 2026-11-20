"""The shared look: palettes and KAGARI's common lines, served to every HEARTH-family client.

HEARTH, RyuCore, KuroDeck and neosh all ship the same nine palettes and the same KAGARI greetings.
The hub is the one place they are defined for a fleet: `/api/theme` serves the palettes, the
theme picked fleet-wide and KAGARI's shared lines, and each client follows it.

An optional JSON file (`HEARTH_PACK`, default `pack.json` next to the database) can change colours,
add palettes and replace KAGARI's lines:

    {"palettes": [{"id": "neon", "name": "NEON", "jp": "電光", "a1": "#ff00aa", "a2": "#00ffcc", "a3": "#ffee00"}],
     "kagari": {"idle": [{"kao": "(・ω・)", "en": "Hello from the pack!", "jp": "こんにちは"}]}}
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from pathlib import Path

log = logging.getLogger("hearth")

HEX = re.compile(r"^#[0-9a-fA-F]{6}$")
ID = re.compile(r"^[a-z][a-z0-9_]{0,15}$")
MAX_CUSTOM = 32
KEYS = ("a1", "a2", "a3", "surface", "bg", "bg2", "panel", "text", "dim", "faint")

# Same values as the `:root[data-theme=…]` blocks in style.css. `surface` is --s-rgb and `panel`
# is --panel without its 0.9 alpha.
_P = [
    ("hearth", "HEARTH", "炉心", "#3ef3ff #ff3ea5 #9b7bff #0e142a #05060d #0a0d1c #090d1c #d8f6ff #7d93b5 #45567a"),
    ("redline", "REDLINE", "赤警報", "#ff6a3d #ffd23f #ff9a76 #260e0c #0b0405 #160808 #180909 #ffe9e0 #b58a7d #6e4a42"),
    ("mecha", "MECHA", "機甲", "#8dff5c #ffe14d #4dffd2 #101e10 #040805 #08110a #09140b #e4ffe0 #86a982 #47613f"),
    ("sakura", "SAKURA", "桜", "#ff9ad5 #8fdcff #d4b3ff #241022 #0b0510 #150a18 #180a1a #ffeaf6 #b48aa6 #6b4a63"),
    ("eclipse", "ECLIPSE", "月蝕", "#c3a6ff #ffcb5c #7fb4ff #16102a #06050e #0d0a1c #0e0a1e #ece6ff #8d86b5 #4c4675"),
    ("kurenai", "KURENAI", "紅", "#ff2e4d #ff8a8a #c21a3a #22060a #0a0203 #160406 #1a0508 #ffe3e6 #b57a82 #6e3a42"),
    ("abyss", "ABYSS", "深海", "#2f9bff #3effc1 #5a6bff #061226 #02060d #051022 #050e1e #def0ff #7a97b8 #3c5475"),
    ("aurum", "AURUM", "黄金", "#ffc94a #fff1b8 #d98b2b #1e1608 #090702 #141006 #161006 #fff5dc #b5a27a #6e5f3e"),
    ("specter", "SPECTER", "幽霊", "#e6eef7 #9fb4c9 #6e8296 #14171c #060708 #0d0f12 #101216 #f2f5f8 #8c96a2 #4c5560"),
]
BUILTIN = [{"id": i, "name": n, "jp": j, "builtin": True, **dict(zip(KEYS, cols.split()))} for i, n, j, cols in _P]

# KAGARI's lines that every client shares. Greetings are picked by the hour (`until` is exclusive).
# Placeholders: {time} = HH:MM, {where} = what the client watches ("network", "shell", "deck").
KAGARI = {
    "greetings": [
        {"until": 5, "kao": "(￣o￣) zzz", "en": "Still up at {time}, commander? I'll keep the hearth warm.", "jp": "夜更かしですね…"},
        {"until": 11, "kao": "(｡•̀ᴗ-)✧", "en": "Good morning, commander! Systems warmed up and ready.", "jp": "おはようございます！"},
        {"until": 18, "kao": "(・ω・)ノ", "en": "Good afternoon! Everything is humming along nicely.", "jp": "こんにちは！"},
        {"until": 24, "kao": "(´｡• ᵕ •｡`)", "en": "Good evening, commander. Quiet night on the {where} so far.", "jp": "こんばんは。"},
    ],
    # data-free lines a client may mix into its idle rotation
    "idle": [
        {"kao": "(・ω・)つ☕", "en": "Remember to drink some water, commander. Even reactors need coolant.", "jp": "水分補給を忘れずに。"},
        {"kao": "( ˘▽˘)っ♨", "en": "Nothing to report. I like it when it's quiet like this~", "jp": "異常なし、平和です。"},
        {"kao": "(｀・ω・´)ゞ", "en": "Standing watch. Call me if anything looks off!", "jp": "監視続行中！"},
    ],
}


def _clean_line(x, need_until: bool = False) -> dict | None:
    if not isinstance(x, dict):
        return None
    out = {}
    for k, n in (("kao", 40), ("en", 300), ("jp", 80)):
        v = x.get(k)
        if not isinstance(v, str) or not v.strip() or any(ord(c) < 32 for c in v):
            return None
        out[k] = v.strip()[:n]
    if need_until:
        try:
            out["until"] = max(1, min(24, int(x.get("until"))))
        except (TypeError, ValueError):
            return None
    return out


def _clean_palette(x, base: dict | None) -> dict | None:
    if not isinstance(x, dict) or not isinstance(x.get("id"), str) or not ID.match(x["id"]):
        return None
    p = dict(base) if base else {"id": x["id"], "builtin": False}
    for k in ("name", "jp"):
        v = x.get(k)
        if isinstance(v, str) and v.strip() and not any(ord(c) < 32 for c in v):
            p[k] = v.strip()[:16]
    for k in KEYS:
        v = x.get(k)
        if isinstance(v, str) and HEX.match(v):
            p[k] = v.lower()
    if base is None:
        if not all(isinstance(p.get(k), str) for k in ("a1", "a2", "a3")):
            return None
        p.setdefault("name", p["id"].upper())
        p.setdefault("jp", "")
        _derive(p)
    return p


def _rgb(h: str) -> tuple[int, int, int]:
    return int(h[1:3], 16), int(h[3:5], 16), int(h[5:7], 16)


def _hex(r: float, g: float, b: float) -> str:
    return "#" + "".join(f"{max(0, min(255, round(v))):02x}" for v in (r, g, b))


def _derive(p: dict) -> None:
    """Fill the background and text roles of a custom palette from its primary accent."""
    r, g, b = _rgb(p["a1"])
    mix = lambda f, base: _hex(*(c * f + bc * (1 - f) for c, bc in zip((r, g, b), base)))  # noqa: E731
    p.setdefault("dim", mix(.3, (140, 150, 175)))
    dr, dg, db = _rgb(p["dim"])
    p.setdefault("faint", _hex(dr * .55, dg * .55, db * .55))
    p.setdefault("bg", mix(.03, (5, 6, 13)))
    p.setdefault("bg2", mix(.06, (10, 13, 28)))
    p.setdefault("panel", mix(.06, (9, 13, 28)))
    p.setdefault("surface", mix(.10, (14, 20, 42)))
    p.setdefault("text", mix(.15, (232, 240, 250)))


class Pack:
    def __init__(self, path: str | None) -> None:
        self.path = Path(path) if path else None
        self.mtime = None
        self.palettes: list[dict] = []
        self.kagari: dict = {}
        self.reload()

    def reload(self) -> None:
        pal = [dict(p) for p in BUILTIN]
        kag = {k: [dict(x) for x in v] for k, v in KAGARI.items()}
        raw = {}
        if self.path and self.path.is_file():
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
                self.mtime = self.path.stat().st_mtime
                if not isinstance(raw, dict):
                    raise TypeError("top level must be an object")
            except (OSError, ValueError, TypeError) as e:
                log.warning("pack %s ignored: %s", self.path, e)
                raw = {}
        by = {p["id"]: p for p in pal}
        custom = 0
        for x in raw.get("palettes") or []:
            base = by.get(x.get("id")) if isinstance(x, dict) else None
            if base is None and custom >= MAX_CUSTOM:
                continue
            p = _clean_palette(x, base)
            if p is None:
                log.warning("pack: ignoring palette %r", x.get("id") if isinstance(x, dict) else x)
                continue
            if base is None:
                custom += 1
                pal.append(p)
                by[p["id"]] = p
            else:
                base.update(p)
        k = raw.get("kagari") if isinstance(raw.get("kagari"), dict) else {}
        g = [y for y in (_clean_line(x, True) for x in (k.get("greetings") or [])[:12]) if y]
        if g:
            kag["greetings"] = sorted(g, key=lambda x: x["until"])
            kag["greetings"][-1]["until"] = 24  # the last greeting covers the rest of the day
        idle = [y for y in (_clean_line(x) for x in (k.get("idle") or [])[:40]) if y]
        if "idle" in k:
            kag["idle"] = idle
        self.palettes, self.kagari = pal, kag

    def maybe_reload(self) -> None:
        if not self.path:
            return
        try:
            m = self.path.stat().st_mtime if self.path.is_file() else None
        except OSError:
            m = None
        if m != self.mtime:
            self.mtime = m
            self.reload()

    def get(self, pid: str) -> dict | None:
        return next((p for p in self.palettes if p["id"] == pid), None)

    def rev(self, theme: str) -> str:
        body = json.dumps([theme, self.palettes, self.kagari], sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(body.encode()).hexdigest()[:12]

    def text(self, theme: str) -> str:
        """Line format for shells: `key=value`, nothing to parse but splits on spaces and `|`."""
        out = [f"rev={self.rev(theme)}", f"theme={theme}"]
        for p in self.palettes:
            out.append(f"palette.{p['id']}={p['a1']} {p['a2']} {p['a3']} {p['dim']} {p['faint']}")
            if p.get("jp"):
                out.append(f"jp.{p['id']}={p['jp']}")
        bar = lambda x: "|".join(x[k].replace("|", "/") for k in ("kao", "en", "jp"))  # noqa: E731
        for g in self.kagari.get("greetings", []):
            out.append(f"greet.{g['until']}={bar(g)}")
        for x in self.kagari.get("idle", []):
            out.append(f"idle={bar(x)}")
        return "\n".join(s.replace("\r", " ") for s in out) + "\n"


def default_path(db_path: str) -> str:
    return os.getenv("HEARTH_PACK") or str(Path(db_path).parent / "pack.json")
