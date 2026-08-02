#!/usr/bin/env python3
"""Strip the misleading panels out of the generated 3D contribution chart.

github-profile-3d-contrib always draws a radar and a language donut alongside
the calendar, and both are built from the GraphQL contributions collection —
which sees only public activity. On this account that means a donut reading
"Python / other" and a radar collapsed to near-zero on four of five axes, while
the real figures are ~2,600 commits and ~108 pull requests. Rather than publish
numbers that undersell by an order of magnitude, the calendar is kept and those
two panels are removed; assets/stats-*.svg carry the real versions.

The star/fork counters go too — both are 0 and add nothing.

Groups are matched on their shape rather than their index, so an upstream
reorder doesn't silently strip the wrong thing:

  calendar  rect, no path      -> keep
  radar     polygon            -> drop
  donut     path AND rect      -> drop
  footer    path, no rect      -> keep, minus the icon+count pairs
"""

import glob
import os
import sys
from xml.etree import ElementTree as ET

NS = "http://www.w3.org/2000/svg"
Q = "{%s}" % NS
ET.register_namespace("", NS)

TARGET = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                      "..", "profile-3d-contrib", "*.svg")


def has(el, tag):
    return el.find(f".//{Q}{tag}") is not None


def trim(path):
    tree = ET.parse(path)
    root = tree.getroot()
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
            remove = set()
            for i, ch in enumerate(kids):
                if ch.tag == Q + "g":
                    remove.add(i)
                    if i + 1 < len(kids) and kids[i + 1].tag == Q + "text":
                        remove.add(i + 1)
            for i in sorted(remove, reverse=True):
                g.remove(kids[i])
            if remove:
                dropped.append(f"{len(remove) // 2} counters")

    name = os.path.basename(path)
    if not dropped:
        # Distinguish "already trimmed" from "upstream moved things around".
        # The calendar is a top-level group holding one group per day, so its
        # presence means we have a real chart that simply has nothing left to
        # strip — re-running must stay a no-op, not an error.
        grid = any(len([k for k in g if k.tag == Q + "g"]) > 100 for g in groups)
        if grid:
            print(f"  {name}: already trimmed")
            return True
        print(f"  ! {name}: no calendar grid and nothing to strip — "
              f"upstream layout likely changed", file=sys.stderr)
        return False

    tree.write(path, encoding="unicode", xml_declaration=False)
    print(f"  {name}: dropped {', '.join(dropped)}")
    return True


def main():
    files = sorted(glob.glob(TARGET))
    if not files:
        sys.exit("no SVGs found in profile-3d-contrib/")
    ok = [trim(f) for f in files]
    if not any(ok):
        sys.exit("trimmed nothing in any file — upstream layout likely changed")


if __name__ == "__main__":
    main()
