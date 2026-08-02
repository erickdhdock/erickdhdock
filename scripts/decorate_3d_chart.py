#!/usr/bin/env python3
"""Rework the generated 3D contribution chart: drop the misleading panels,
then fill the space they leave.

github-profile-3d-contrib always draws a radar and a language donut alongside
the calendar, and both are built from the GraphQL contributions collection —
which sees only public activity. On this account that means a donut reading
"Python / other" and a radar collapsed to near-zero on four of five axes, while
the real figures are ~2,600 commits and ~108 pull requests. Publishing numbers
that undersell by an order of magnitude is worse than publishing none, so they
go; assets/stats-*.svg carry the honest versions.

Removing them leaves the isometric ribbon running corner to corner with two
large empty triangles. Those get a streak block (top-right) and a weekday
distribution (bottom-left), read from assets/stats.json — facts that appear
nowhere else on the profile and that name nothing.

Everything injected uses the chart's own CSS classes (.fill-fg, .fill-strong,
.fill-weak, .cont-top-0), so the light and dark variants theme themselves and
no palette is duplicated here.
"""

import glob
import json
import os
import sys
from xml.etree import ElementTree as ET

NS = "http://www.w3.org/2000/svg"
Q = "{%s}" % NS
ET.register_namespace("", NS)

HERE = os.path.dirname(os.path.abspath(__file__))
TARGET = os.path.join(HERE, "..", "profile-3d-contrib", "*.svg")
STATS = os.path.join(HERE, "..", "assets", "stats.json")

MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
          "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def has(el_, tag):
    return el_.find(f".//{Q}{tag}") is not None


def el(tag, **attrs):
    node = ET.Element(Q + tag)
    for k, v in attrs.items():
        node.set(k.replace("_", "-"), str(v))
    return node


def text(x, y, body, size, cls="fill-fg", anchor="start", weight=None):
    t = el("text", x=x, y=y, font_size=size)
    t.set("class", cls)
    if anchor != "start":
        t.set("text-anchor", anchor)
    if weight:
        t.set("font-weight", str(weight))
    t.text = str(body)
    return t


def pretty_date(iso):
    return f"{int(iso[8:10])} {MONTHS[int(iso[5:7]) - 1]} {iso[:4]}"


def streak_block(r):
    """Top-right void: the live streak, plus three supporting figures."""
    g = el("g", transform="translate(946, 96)")
    g.append(text(0, 62, r["current_streak"], 64, "fill-strong", weight=700))
    g.append(text(0, 88, "DAY STREAK", 14, "fill-fg", weight=600))
    same = r["current_streak"] == r["longest_streak"]
    tail = ", longest of the year" if same else ""
    g.append(text(0, 108,
                  f"unbroken since {pretty_date(r['current_streak_start'])}"
                  f"{tail}", 11.5, "fill-weak"))
    line = el("line", x1=0, y1=128, x2=296, y2=128, stroke_opacity="0.35")
    line.set("class", "stroke-weak")
    g.append(line)
    rows = [
        (r["active_days"], f"active days of {r['total_days']}"),
        (r["peak"], f"busiest day, {pretty_date(r['peak_date'])}"),
        (r["avg_per_active_day"], "average per active day"),
    ]
    for i, (val, lab) in enumerate(rows):
        y = 156 + i * 26
        g.append(text(44, y, val, 16, "fill-fg", anchor="end", weight=600))
        g.append(text(58, y, lab, 12, "fill-weak"))
    return g


def weekday_block(r):
    """Bottom-left void: contributions by weekday, ranked."""
    g = el("g", transform="translate(60, 556)")
    g.append(text(0, 0, "When the work lands", 15, "fill-fg", weight=600))
    g.append(text(0, 20, "contributions by weekday, past year", 11.5,
                  "fill-weak"))

    days = sorted(r["weekdays"], key=lambda d: -d["count"])
    top = max((d["count"] for d in days), default=1) or 1
    track_x, track_w = 44, 264
    for i, d in enumerate(days):
        y = 48 + i * 23
        w = max(3.0, track_w * d["count"] / top)
        g.append(text(0, y + 4, d["day"], 12, "fill-weak"))
        track = el("rect", x=track_x, y=y - 6, width=track_w, height=11, rx=3)
        track.set("class", "cont-top-0")
        g.append(track)
        bar = el("rect", x=track_x, y=y - 6, width=round(w, 1), height=11, rx=3)
        bar.set("class", "fill-strong")
        g.append(bar)
        g.append(text(track_x + track_w + 12, y + 4, f"{d['count']:,}", 12,
                      "fill-fg", weight=600))
    return g


def decorate(path, stats):
    tree = ET.parse(path)
    root = tree.getroot()

    # Clear anything a previous run added, so re-running is a no-op rather
    # than stacking duplicate blocks.
    for g in [c for c in root if c.get("data-injected")]:
        root.remove(g)

    groups = [c for c in root if c.tag == Q + "g"]
    dropped = []
    for g in groups:
        if has(g, "polygon"):
            root.remove(g)
            dropped.append("radar")
        elif has(g, "path") and has(g, "rect"):
            root.remove(g)
            dropped.append("donut")

    # Footer: drop each icon <g> and the count <text> right after it.
    for g in [c for c in root if c.tag == Q + "g"]:
        if has(g, "path") and not has(g, "rect"):
            kids = list(g)
            drop = set()
            for i, ch in enumerate(kids):
                if ch.tag == Q + "g":
                    drop.add(i)
                    if i + 1 < len(kids) and kids[i + 1].tag == Q + "text":
                        drop.add(i + 1)
            for i in sorted(drop, reverse=True):
                g.remove(kids[i])
            if drop:
                dropped.append(f"{len(drop) // 2} counters")

    name = os.path.basename(path)
    # The calendar is a top-level group holding one group per day. Its absence
    # alongside nothing to strip means upstream moved things around.
    grid = any(len([k for k in g if k.tag == Q + "g"]) > 100 for g in groups)
    if not dropped and not grid:
        print(f"  ! {name}: no calendar grid and nothing to strip — "
              f"upstream layout likely changed", file=sys.stderr)
        return False

    added = []
    if stats and stats.get("rhythm"):
        for build in (streak_block, weekday_block):
            block = build(stats["rhythm"])
            block.set("data-injected", "1")
            root.append(block)
            added.append(build.__name__.replace("_block", ""))

    tree.write(path, encoding="unicode", xml_declaration=False)
    note = f"dropped {', '.join(dropped)}" if dropped else "already trimmed"
    if added:
        note += f"; added {', '.join(added)}"
    print(f"  {name}: {note}")
    return True


def main():
    files = sorted(glob.glob(TARGET))
    if not files:
        sys.exit("no SVGs found in profile-3d-contrib/")

    stats = None
    try:
        with open(STATS) as f:
            stats = json.load(f)
    except (OSError, ValueError):
        # The chart is still worth trimming without the extra blocks.
        print(f"  ! {STATS} unreadable — skipping the injected blocks",
              file=sys.stderr)

    ok = [decorate(f, stats) for f in files]
    if not any(ok):
        sys.exit("decorated nothing — upstream layout likely changed")


if __name__ == "__main__":
    main()
