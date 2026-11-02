#!/usr/bin/env python3
"""Render the Open Graph card (1200x630) from data.json.

Why this exists (Rune, 2026-11-02): instead of hand-building an image to post alongside the
monthly tweet, the link itself should preview with the scoreboard — X, Slack, iMessage and
LinkedIn all read og:image — which also gives the site real SEO metadata instead of none.

Runs inside the daily-publish GitHub Action, so it uses Pillow (no browser, no CDP): the
runner has no Chrome and installing one for a static card would be absurd. Pillow and the
DejaVu fonts are the only dependencies; the workflow pip-installs Pillow.

Output: og.png next to index.html. The publisher's `git add -A` picks it up.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data.json"
OUT = ROOT / "og.png"
CUTOUT = ROOT / "jason-cutout.png"

W, H = 1200, 630
BG = (7, 11, 18)
PANEL = (13, 20, 32)
PANEL2 = (17, 26, 41)
LINE = (29, 42, 61)
INK = (245, 247, 250)
MUTED = (163, 174, 194)
DIM = (124, 135, 153)
RED = (255, 107, 107)
BTC = (247, 147, 26)
BLUE = (94, 168, 255)
GREEN = (46, 230, 168)

FONT_DIRS = [
    "/usr/share/fonts/truetype/dejavu",
    "/usr/share/fonts/dejavu",
    "/System/Library/Fonts/Supplemental",
]


def font(name: str, size: int) -> ImageFont.FreeTypeFont:
    for d in FONT_DIRS:
        p = Path(d) / name
        if p.exists():
            return ImageFont.truetype(str(p), size)
    return ImageFont.load_default()


def pct(v: float | None) -> str:
    if v is None:
        return "—"
    return f"{v:+.1f}%"


def circle_cutout(size: int) -> Image.Image | None:
    """Jason's portrait, masked to a circle. Missing file is not fatal — the card still renders."""
    if not CUTOUT.exists():
        print(f"WARNING: {CUTOUT.name} missing, drawing the card without a portrait", file=sys.stderr)
        return None
    src = Image.open(CUTOUT).convert("RGBA").resize((size, size), Image.LANCZOS)
    mask = Image.new("L", (size * 4, size * 4), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, size * 4 - 1, size * 4 - 1), fill=255)
    mask = mask.resize((size, size), Image.LANCZOS)
    out = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    out.paste(src, (0, 0), mask)
    return out


def main() -> int:
    if not DATA.exists():
        print("OG FAILED: data.json not found", file=sys.stderr)
        return 1
    d = json.loads(DATA.read_text())
    meta, sb, c, chart = d["meta"], d["scoreboard"], d["bets"]["counts"], d["chart"]

    img = Image.new("RGB", (W, H), BG)
    dr = ImageDraw.Draw(img)

    # frame
    dr.rounded_rectangle((20, 20, W - 20, H - 20), radius=26, fill=PANEL, outline=LINE, width=2)

    # portrait
    av = circle_cutout(196)
    if av:
        img.paste(av, (64, 62), av)
        dr.ellipse((64, 62, 64 + 196, 62 + 196), outline=LINE, width=2)

    f_title = font("DejaVuSans-Bold.ttf", 46)
    f_sub = font("DejaVuSans.ttf", 22)
    f_num = font("DejaVuSans-Bold.ttf", 58)
    f_stat = font("DejaVuSans-Bold.ttf", 26)
    f_small = font("DejaVuSans.ttf", 19)
    f_foot = font("DejaVuSans.ttf", 20)

    dr.text((296, 74), "Jason vs Bitcoin", font=f_title, fill=INK)
    dr.text((298, 136), f"live scoreboard · day {meta['day_of']} of {meta['total_days']}",
            font=f_sub, fill=DIM)

    # scorebug
    dr.rounded_rectangle((296, 178, 470, 250), radius=36, fill=PANEL2, outline=LINE, width=2)
    dr.text((334, 190), str(c["won"]), font=f_num, fill=RED)
    dr.line((383, 202, 383, 228), fill=LINE, width=2)
    dr.text((406, 190), str(c["lost"]), font=f_num, fill=BTC)

    # stats
    x = 296
    for label, value, colour in (
        ("BTC", pct(sb["btc"]["ret_pct"]), BTC),
        ("QQQ", pct(sb["qqq"]["ret_pct"]), BLUE),
        ("AI", pct(sb["ai"]["ret_pct"]), GREEN),
    ):
        dr.ellipse((x, 274, x + 12, 286), fill=colour)
        dr.text((x + 22, 268), label, font=f_small, fill=MUTED)
        dr.text((x + 22, 292), value, font=f_stat, fill=colour)
        x += 186

    # chart
    left, right, top, bottom = 296, W - 62, 356, 552
    dates = chart.get("dates") or []
    series = {"btc": BTC, "qqq": BLUE, "ai": GREEN}
    vals = [v for k in series for v in (chart.get(k) or []) if v is not None]
    if len(dates) > 1 and vals:
        lo, hi = min(min(vals), 0.0), max(max(vals), 0.0)
        span = (hi - lo) or 1.0

        def ypos(v: float) -> float:
            return bottom - (v - lo) / span * (bottom - top)

        zero_y = ypos(0.0)
        dr.line((left, zero_y, right, zero_y), fill=(42, 57, 80), width=1)
        for key, colour in series.items():
            pts, row = [], chart.get(key) or []
            for i, v in enumerate(row):
                if v is None:
                    continue
                pts.append((left + i / (len(dates) - 1) * (right - left), ypos(v)))
            if len(pts) > 1:
                dr.line(pts, fill=colour, width=4, joint="curve")
    else:
        dr.text((left, top), "chart data unavailable", font=f_small, fill=DIM)

    dr.text((296, 566), "runestone0.github.io/jason-claim", font=f_foot, fill=DIM)

    img.save(OUT, "PNG", optimize=True)
    print(f"wrote {OUT} ({OUT.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
