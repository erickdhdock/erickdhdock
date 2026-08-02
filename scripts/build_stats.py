#!/usr/bin/env python3
"""Build the commit-analytics panel for the profile README.

Why this exists: GitHub's public surface reports ~85 commits for this account,
because ~95% of the work lives in private org repos. `contributionsCollection`
cannot itemize that work (per-org queries return restrictedContributionsCount: 0),
but `repository.defaultBranchRef.target.history(author:, since:)` *can* — it
resolves private and org repos given a repo-scoped token. So we walk every repo
and count authored commits directly.

Privacy rule: private repos are aggregated into org totals. No private repo name
is ever written into a committed artifact. Public repo names are fine.

Stdlib only, so CI needs no pip install.
"""

import json
import math
import os
import ssl
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from xml.etree import ElementTree

API = "https://api.github.com/graphql"
LOGIN = os.environ.get("PROFILE_LOGIN", "erickdhdock")
ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
ASSETS = os.path.join(ROOT, "assets")
PAGE = 50

TOKEN = os.environ.get("PROFILE_TOKEN") or os.environ.get("GITHUB_TOKEN")
if not TOKEN:
    sys.exit("need PROFILE_TOKEN or GITHUB_TOKEN in the environment")


# ---------------------------------------------------------------- api

def ssl_context():
    """CI has a system trust store; a python.org macOS build often doesn't
    (Install Certificates.command never run). Fall back to certifi if it's
    around rather than failing, and never disable verification."""
    ctx = ssl.create_default_context()
    if ssl.get_default_verify_paths().cafile is None:
        try:
            import certifi
            ctx.load_verify_locations(certifi.where())
        except ImportError:
            pass
    return ctx


SSL_CTX = ssl_context()


def gql(query, variables=None):
    body = json.dumps({"query": query, "variables": variables or {}}).encode()
    req = urllib.request.Request(
        API,
        data=body,
        headers={
            "Authorization": "bearer " + TOKEN,
            "Content-Type": "application/json",
            "User-Agent": "profile-stats-builder",
        },
    )
    last = None
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=45, context=SSL_CTX) as r:
                payload = json.load(r)
            if "errors" in payload:
                msgs = "; ".join(e.get("message", "?") for e in payload["errors"])
                # A missing org / no-access repo shouldn't kill the whole build.
                if payload.get("data"):
                    print(f"  ! partial: {msgs}", file=sys.stderr)
                    return payload["data"]
                raise RuntimeError(msgs)
            return payload["data"]
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as e:
            last = e
            print(f"  ! retry {attempt + 1}/4: {e}", file=sys.stderr)
    raise RuntimeError(f"graphql failed after retries: {last}")


# ---------------------------------------------------------------- collect

SINCE = (datetime.now(timezone.utc) - timedelta(days=365)).strftime("%Y-%m-%dT%H:%M:%SZ")

REPO_FIELDS = """
  nameWithOwner
  isPrivate
  isFork
  stargazerCount
  primaryLanguage { name }
  allTime: defaultBranchRef { target { ... on Commit {
    history(first: 1, author: {id: $me}) { totalCount } } } }
  window: defaultBranchRef { target { ... on Commit {
    history(first: 1, author: {id: $me}, since: $since) { totalCount } } } }
"""

USER_REPOS = """
query($login: String!, $me: ID!, $since: GitTimestamp!, $after: String) {
  user(login: $login) {
    repositories(first: %d, after: $after, ownerAffiliations: OWNER) {
      pageInfo { hasNextPage endCursor }
      nodes { %s }
    }
  }
}""" % (PAGE, REPO_FIELDS)

ORG_REPOS = """
query($login: String!, $me: ID!, $since: GitTimestamp!, $after: String) {
  organization(login: $login) {
    repositories(first: %d, after: $after) {
      pageInfo { hasNextPage endCursor }
      nodes { %s }
    }
  }
}""" % (PAGE, REPO_FIELDS)


def count(node, key):
    branch = node.get(key) or {}
    target = branch.get("target") or {}
    return (target.get("history") or {}).get("totalCount", 0) or 0


def scan(query, login, me, root_key):
    """Walk one owner's repos. Returns rows with authored-commit counts."""
    rows, after = [], None
    while True:
        data = gql(query, {"login": login, "me": me, "since": SINCE, "after": after})
        root = (data or {}).get(root_key)
        if not root:
            break
        block = root["repositories"]
        for n in block["nodes"]:
            if not n:
                continue
            all_time = count(n, "allTime")
            if not all_time:
                continue
            rows.append({
                "repo": n["nameWithOwner"],
                "private": n["isPrivate"],
                "fork": n["isFork"],
                "stars": n["stargazerCount"],
                "lang": (n["primaryLanguage"] or {}).get("name"),
                "all_time": all_time,
                "window": count(n, "window"),
            })
        if not block["pageInfo"]["hasNextPage"]:
            break
        after = block["pageInfo"]["endCursor"]
    return rows


def activity():
    """PR / issue / review counts.

    The search API resolves private repos the token can see, which the
    contributions collection does not: it reports 0 pull requests for this
    account while search finds 108, all but one of them private.
    """
    def q(expr):
        return f'search(query: "{expr}", type: ISSUE) {{ issueCount }}'

    d = gql("{ %s }" % " ".join([
        f'prs: {q(f"author:{LOGIN} type:pr")}',
        f'merged: {q(f"author:{LOGIN} type:pr is:merged")}',
        f'issues: {q(f"author:{LOGIN} type:issue")}',
        f'reviews: {q(f"reviewed-by:{LOGIN}")}',
    ]))
    return {k: v["issueCount"] for k, v in d.items()}


def collect():
    ident = gql("""
    query($login: String!) {
      viewer { id login organizations(first: 50) { nodes { login name } } }
      user(login: $login) {
        id name
        organizations(first: 50) { nodes { login name } }
        contributionsCollection {
          totalCommitContributions
          restrictedContributionsCount
          contributionCalendar {
            totalContributions
            weeks { contributionDays { date contributionCount } }
          }
        }
      }
    }""", {"login": LOGIN})

    user = ident["user"]
    me = user["id"]
    viewer = ident["viewer"]

    # viewer.organizations includes private memberships; user.organizations is
    # public-only. Prefer the richer list when we're authenticated as ourselves.
    src = viewer if viewer["login"].lower() == LOGIN.lower() else user
    orgs = {o["login"]: (o["name"] or o["login"]) for o in src["organizations"]["nodes"]}

    print(f"scanning {LOGIN} + {len(orgs)} orgs since {SINCE[:10]}")

    raw = scan(USER_REPOS, LOGIN, me, "user")
    print(f"  {LOGIN}: {len(raw)} repos with commits")
    for login in orgs:
        got = scan(ORG_REPOS, login, me, "organization")
        print(f"  {login}: {len(got)} repos with commits")
        raw += got

    # Pagination repeats a repo when pushedAt ordering shifts mid-scan. Left
    # unhandled this silently inflates every total (it added ~600 commits and
    # 8 phantom repos on the first probe run), so dedupe is load-bearing.
    seen, rows = set(), []
    for r in raw:
        if r["repo"] in seen:
            continue
        seen.add(r["repo"])
        rows.append(r)
    dupes = len(raw) - len(rows)
    if dupes:
        print(f"  deduped {dupes} repeated repo rows")

    cal = user["contributionsCollection"]["contributionCalendar"]
    days = [d for w in cal["weeks"] for d in w["contributionDays"]]

    # Monthly rhythm, from the calendar so private days are included.
    months, order = {}, []
    for d in days:
        key = d["date"][:7]
        if key not in months:
            months[key] = 0
            order.append(key)
        months[key] += d["contributionCount"]

    return {
        "rows": rows,
        "me_name": user["name"] or LOGIN,
        "orgs": orgs,
        "calendar": {
            "total": cal["totalContributions"],
            "active_days": sum(1 for d in days if d["contributionCount"] > 0),
            "peak": max((d["contributionCount"] for d in days), default=0),
        },
        "months": [{"month": m, "count": months[m]} for m in order],
        "activity": activity(),
        # If the token can't see private work, restricted stays high while our
        # own walk finds only public repos. Surfacing this makes PAT expiry
        # visible instead of silently halving the numbers.
        "public_only": all(not r["private"] for r in rows),
    }


def aggregate(bundle):
    rows, orgs = bundle["rows"], bundle["orgs"]

    owners = {}
    for r in rows:
        login = r["repo"].split("/")[0]
        o = owners.setdefault(login, {
            "login": login,
            "name": orgs.get(login, login if login != LOGIN else "Personal"),
            "repos": 0, "private": 0, "all_time": 0, "window": 0,
        })
        o["repos"] += 1
        o["private"] += 1 if r["private"] else 0
        o["all_time"] += r["all_time"]
        o["window"] += r["window"]
    owners = sorted(owners.values(), key=lambda o: -o["all_time"])

    langs = {}
    for r in rows:
        if r["lang"]:
            langs[r["lang"]] = langs.get(r["lang"], 0) + r["all_time"]
    ranked = sorted(langs.items(), key=lambda kv: -kv[1])
    total_lang = sum(langs.values()) or 1
    top = ranked[:5]
    other = sum(v for _, v in ranked[5:])
    mix = [{"name": k, "commits": v, "pct": round(100 * v / total_lang, 1)} for k, v in top]
    if other:
        mix.append({"name": "Other", "commits": other,
                    "pct": round(100 * other / total_lang, 1)})

    return {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "login": LOGIN,
        "name": bundle["me_name"],
        "public_only": bundle["public_only"],
        "totals": {
            "commits_all_time": sum(r["all_time"] for r in rows),
            "commits_last_year": sum(r["window"] for r in rows),
            "repos": len(rows),
            "repos_private": sum(1 for r in rows if r["private"]),
        },
        "calendar": bundle["calendar"],
        "months": bundle["months"],
        # Radar axes need commits and repos alongside the search-derived
        # counts; reviews is carried in the data but not plotted (it is 0).
        "activity": dict(bundle["activity"],
                         commits=sum(r["all_time"] for r in rows),
                         repos=len(rows)),
        # Org-level only. Private repo names deliberately never reach this file.
        "owners": owners,
        "languages": mix,
        "public_repos_by_commits": [
            {"repo": r["repo"], "commits": r["all_time"], "stars": r["stars"]}
            for r in sorted((x for x in rows if not x["private"]),
                            key=lambda r: -r["all_time"])[:5]
        ],
    }


# ---------------------------------------------------------------- render

# Palettes validated with the dataviz validator against the two surfaces this
# panel actually renders on (#ffffff / #0d1117), categorical mode:
#   light -> all checks pass; contrast WARN on aqua/yellow/magenta, which
#            obligates visible text labels. The legend names every segment.
#   dark  -> all checks pass including contrast.
LIGHT = {
    "surface": "#ffffff", "ink": "#0b0b0b", "ink2": "#52514e", "muted": "#898781",
    "rule": "#e1e0d9", "track": "#eceef1", "accent": "#2a78d6",
    "series": ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#898781"],
}
DARK = {
    "surface": "#0d1117", "ink": "#ffffff", "ink2": "#c3c2b7", "muted": "#898781",
    "rule": "#2c2c2a", "track": "#1b2027", "accent": "#3987e5",
    "series": ["#3987e5", "#d95926", "#199e70", "#c98500", "#d55181", "#898781"],
}

# Single quotes inside: this string is interpolated into a double-quoted XML
# attribute, and "Segoe UI" with double quotes terminates it early.
FONT = "system-ui,-apple-system,'Segoe UI',Roboto,sans-serif"
W = 880
BAR_X = 164          # bar baseline
BAR_MAX = 156
VAL_X = 372          # all-time value, right-aligned
REC_X = 468          # "N recent", right-aligned; keeps clear of STACK_X
ROW_H = 30
ORG_TOP = 236
STACK_X = 512
STACK_W = 340
LEG_TOP = 280
LEG_H = 22

# Section 3: monthly rhythm (left) and the activity radar (right).
MON_X = 28           # first column
MON_W = 28           # column width
MON_GAP = 9
MON_BASE_OFF = 120   # baseline below the section title
MON_MAX = 80         # tallest column; leaves headroom for the value labels
RAD_CX = 682
RAD_R = 70
# Log rings, because commits (thousands) and issues (tens) share one plot.
RAD_RINGS = [10, 100, 1000, 10000]
# Reviews is deliberately not an axis: it is genuinely 0, and a vertex pinned
# to the centre reads as a broken chart rather than as data. It stays in
# stats.json. Repos is plotted in its place.
RAD_AXES = [("Commits", "commits"), ("Pull requests", "prs"),
            ("Merged", "merged"), ("Repos", "repos"), ("Issues", "issues")]


MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
          "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def esc(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def month_label(ym, with_year=False):
    name = MONTHS[int(ym[5:7]) - 1]
    return f"{name} {ym[:4]}" if with_year else name


def radar_pt(cx, cy, i, frac):
    th = math.radians(i * (360.0 / len(RAD_AXES)))
    return cx + RAD_R * frac * math.sin(th), cy - RAD_R * frac * math.cos(th)


def rad_frac(v):
    """Log scale: commits run to thousands while issues run to tens."""
    return min(1.0, math.log10(v + 1) / 4.0)


def n(v):
    return f"{v:,}"


def clip(s, limit):
    return s if len(s) <= limit else s[: limit - 1] + "…"


def render(d, p):
    t = d["totals"]
    owners = d["owners"][:6]
    langs = d["languages"]

    act = d["activity"]
    months = d["months"]

    org_bottom = ORG_TOP + (len(owners) - 1) * ROW_H + 6
    leg_bottom = LEG_TOP + (len(langs) - 1) * LEG_H + 6
    mid_rule = max(org_bottom, leg_bottom) + 22

    sec3_top = mid_rule + 30           # section-3 heading baseline
    mon_base = sec3_top + MON_BASE_OFF
    rad_cy = sec3_top + 76
    # Lower radar labels sit ~96px below centre (axis label + value line).
    rule_y = max(mon_base + 34, rad_cy + RAD_R + 46)
    foot_y = rule_y + 26
    h = foot_y + 18
    out = []
    a = out.append

    a(f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{h}" '
      f'viewBox="0 0 {W} {h}" role="img" font-family="{FONT}">')
    a(f'<title>Commit analytics for {esc(d["name"])}</title>')
    a(f'<desc>{n(t["commits_all_time"])} commits authored all-time, '
      f'{n(t["commits_last_year"])} in the last 12 months, across {t["repos"]} '
      f'repositories ({t["repos_private"]} private) in {len(d["owners"])} accounts. '
      f'Top language {esc(langs[0]["name"])} at {langs[0]["pct"]} percent.</desc>')
    a(f'<rect width="{W}" height="{h}" rx="8" fill="{p["surface"]}"/>')

    # ---- header
    a(f'<text x="28" y="40" font-size="17" font-weight="600" fill="{p["ink"]}">'
      f'Commit analytics</text>')
    scope = "public repos only · " if d["public_only"] else ""
    a(f'<text x="{W - 28}" y="40" font-size="11.5" text-anchor="end" '
      f'fill="{p["muted"]}">{scope}updated {d["generated_at"]}</text>')
    a(f'<line x1="28" y1="58" x2="{W - 28}" y2="58" stroke="{p["rule"]}"/>')

    # ---- KPI row (stat tiles, not a chart: four headline numbers)
    tiles = [
        (n(t["commits_all_time"]), "commits authored"),
        (n(t["commits_last_year"]), "in the last 12 months"),
        (n(act["prs"]), "pull requests opened"),
        (n(t["repos"]), "repositories"),
        (n(t["repos_private"]), "of them private"),
    ]
    tw = (W - 56) / len(tiles)
    for i, (val, lab) in enumerate(tiles):
        x = 28 + i * tw
        a(f'<text x="{x:.0f}" y="108" font-size="31" font-weight="650" '
          f'fill="{p["ink"]}">{val}</text>')
        a(f'<text x="{x:.0f}" y="128" font-size="11" fill="{p["muted"]}">'
          f'{esc(lab)}</text>')
    a(f'<line x1="28" y1="152" x2="{W - 28}" y2="152" stroke="{p["rule"]}"/>')

    # ---- section headings
    a(f'<text x="28" y="180" font-size="13" font-weight="600" fill="{p["ink2"]}">'
      f'Where the commits go</text>')
    a(f'<text x="28" y="198" font-size="11" fill="{p["muted"]}">'
      f'authored commits per account, all-time</text>')
    a(f'<text x="{STACK_X}" y="180" font-size="13" font-weight="600" '
      f'fill="{p["ink2"]}">Language mix</text>')
    a(f'<text x="{STACK_X}" y="198" font-size="11" fill="{p["muted"]}">'
      f'repos weighted by commits authored</text>')

    # ---- org bars: single hue. Length already encodes magnitude, so a second
    # colour channel would be redundant; the title names the one series.
    top = max((o["all_time"] for o in owners), default=1) or 1
    a(f'<clipPath id="bars"><rect x="{BAR_X}" y="150" width="{W}" '
      f'height="{h}"/></clipPath>')
    for i, o in enumerate(owners):
        y = ORG_TOP + i * ROW_H
        w = max(3.0, BAR_MAX * o["all_time"] / top)
        # A long display name truncates to mush ("VNE Medical Techno…"); the
        # login is shorter and is what people actually recognise.
        label = o["name"] if len(o["name"]) <= 19 else min(
            o["name"], o["login"], key=len)
        a(f'<text x="28" y="{y + 4}" font-size="12" fill="{p["ink2"]}">'
          f'{esc(clip(label, 19))}</text>')
        a(f'<rect x="{BAR_X}" y="{y - 6}" width="{BAR_MAX}" height="12" rx="4" '
          f'fill="{p["track"]}"/>')
        # Drawn 4px left of the baseline and clipped, so the data end is
        # rounded while the baseline end stays square.
        #
        # The base width is the FINAL value, not the animation's start value.
        # GitHub serves these under `Content-Security-Policy: ...; sandbox`,
        # which renders SVG statically — SMIL never runs. Anything whose
        # geometry depends on the animation having played renders as an empty
        # track there. Animation is enhancement; the static state must be right.
        a(f'<g clip-path="url(#bars)">'
          f'<rect x="{BAR_X - 4}" y="{y - 6}" width="{w + 4:.1f}" height="12" '
          f'rx="4" fill="{p["accent"]}">'
          f'<animate attributeName="width" values="4;{w + 4:.1f}" dur="0.9s" '
          f'begin="{0.08 * i:.2f}s" fill="freeze" calcMode="spline" '
          f'keyTimes="0;1" keySplines="0.22 0.9 0.3 1"/></rect></g>')
        a(f'<text x="{VAL_X}" y="{y + 4}" font-size="12" text-anchor="end" '
          f'font-weight="600" fill="{p["ink"]}" '
          f'style="font-variant-numeric:tabular-nums">{n(o["all_time"])}</text>')
        # Dormant accounts read as "—" rather than a missing cell, so the
        # difference between "no recent work" and "no data" is explicit.
        recent = f'{n(o["window"])} recent' if o["window"] else "dormant"
        a(f'<text x="{REC_X}" y="{y + 4}" font-size="11" text-anchor="end" '
          f'fill="{p["muted"]}">{recent}</text>')

    # ---- language stack: part-to-whole, categorical, 2px surface gaps.
    sy = 232
    a(f'<clipPath id="stack"><rect x="{STACK_X}" y="{sy}" width="{STACK_W}" '
      f'height="22" rx="4"/></clipPath>')
    # Base width is the full stack, same reason as the bars: with SMIL inert
    # a zero-width reveal clip would hide the whole chart.
    a(f'<clipPath id="reveal"><rect x="{STACK_X}" y="{sy}" width="{STACK_W}" '
      f'height="22">'
      f'<animate attributeName="width" values="0;{STACK_W}" dur="1.0s" '
      f'begin="0.1s" fill="freeze" calcMode="spline" keyTimes="0;1" '
      f'keySplines="0.22 0.9 0.3 1"/></rect></clipPath>')
    a('<g clip-path="url(#stack)"><g clip-path="url(#reveal)">')
    x = float(STACK_X)
    total_pct = sum(l["pct"] for l in langs) or 100.0
    for i, l in enumerate(langs):
        seg = STACK_W * l["pct"] / total_pct
        a(f'<rect x="{x:.1f}" y="{sy}" width="{max(seg - 2, 1):.1f}" height="22" '
          f'fill="{p["series"][i % len(p["series"])]}"/>')
        x += seg
    a('</g></g>')

    # Legend doubles as the direct labels: every segment is named in text, so
    # identity never rests on colour alone (required by the light-mode
    # contrast WARN on aqua/yellow/magenta).
    # One column, full stack width — it balances against the six org rows and
    # leaves room for the commit count beside each share.
    for i, l in enumerate(langs):
        ly = LEG_TOP + i * LEG_H
        a(f'<rect x="{STACK_X}" y="{ly - 9}" width="10" height="10" rx="2.5" '
          f'fill="{p["series"][i % len(p["series"])]}"/>')
        a(f'<text x="{STACK_X + 18}" y="{ly}" font-size="11.5" '
          f'fill="{p["ink2"]}">{esc(clip(l["name"], 18))}</text>')
        a(f'<text x="{STACK_X + STACK_W - 52}" y="{ly}" font-size="11.5" '
          f'text-anchor="end" fill="{p["muted"]}" '
          f'style="font-variant-numeric:tabular-nums">'
          f'{n(l["commits"])}</text>')
        a(f'<text x="{STACK_X + STACK_W}" y="{ly}" font-size="11.5" '
          f'text-anchor="end" fill="{p["ink2"]}" font-weight="600" '
          f'style="font-variant-numeric:tabular-nums">{l["pct"]}%</text>')

    a(f'<line x1="28" y1="{mid_rule}" x2="{W - 28}" y2="{mid_rule}" '
      f'stroke="{p["rule"]}"/>')

    # ---- section 3 headings
    a(f'<text x="28" y="{sec3_top}" font-size="13" font-weight="600" '
      f'fill="{p["ink2"]}">Contribution rhythm</text>')
    span = months[-12:]
    a(f'<text x="28" y="{sec3_top + 18}" font-size="11" fill="{p["muted"]}">'
      f'contributions per month · {month_label(span[0]["month"], True)} – '
      f'{month_label(span[-1]["month"], True)} · private included</text>')
    a(f'<text x="{STACK_X}" y="{sec3_top}" font-size="13" font-weight="600" '
      f'fill="{p["ink2"]}">Activity shape</text>')
    a(f'<text x="{STACK_X}" y="{sec3_top + 18}" font-size="11" '
      f'fill="{p["muted"]}">all-time totals, log scale</text>')

    # ---- monthly columns: one hue, since length already carries magnitude
    peak = max((m["count"] for m in span), default=1) or 1
    for i, m in enumerate(span):
        x = MON_X + i * (MON_W + MON_GAP)
        ht = max(2.0, MON_MAX * m["count"] / peak)
        a(f'<rect x="{x}" y="{mon_base - ht:.1f}" width="{MON_W}" '
          f'height="{ht:.1f}" rx="3" fill="{p["accent"]}">'
          f'<animate attributeName="height" values="2;{ht:.1f}" dur="0.8s" '
          f'begin="{0.05 * i:.2f}s" fill="freeze" calcMode="spline" '
          f'keyTimes="0;1" keySplines="0.22 0.9 0.3 1"/></rect>')
        # Every month carries its count. Nothing here is sensitive, and the
        # peak alone left the rest of the year unreadable.
        top = m["count"] == peak
        a(f'<text x="{x + MON_W / 2:.0f}" y="{mon_base - ht - 5:.1f}" '
          f'font-size="9.5" text-anchor="middle" '
          f'font-weight="{600 if top else 400}" '
          f'fill="{p["ink"] if top else p["muted"]}" '
          f'style="font-variant-numeric:tabular-nums">{n(m["count"])}</text>')
        a(f'<text x="{x + MON_W / 2:.0f}" y="{mon_base + 14}" font-size="9.5" '
          f'text-anchor="middle" fill="{p["muted"]}">'
          f'{MONTHS[int(m["month"][5:7]) - 1]}</text>')
    a(f'<line x1="{MON_X}" y1="{mon_base}" '
      f'x2="{MON_X + 12 * (MON_W + MON_GAP) - MON_GAP}" y2="{mon_base}" '
      f'stroke="{p["rule"]}"/>')

    # ---- activity radar
    for ring in RAD_RINGS:
        pts = " ".join(f"{x:.1f},{y:.1f}" for x, y in
                       (radar_pt(RAD_CX, rad_cy, i, rad_frac(ring))
                        for i in range(len(RAD_AXES))))
        a(f'<polygon points="{pts}" fill="none" stroke="{p["rule"]}" '
          f'stroke-dasharray="2 3"/>')
    for i in range(len(RAD_AXES)):
        x, y = radar_pt(RAD_CX, rad_cy, i, 1.0)
        a(f'<line x1="{RAD_CX}" y1="{rad_cy}" x2="{x:.1f}" y2="{y:.1f}" '
          f'stroke="{p["rule"]}"/>')

    vals = [act.get(key, 0) for _, key in RAD_AXES]
    shape = " ".join(f"{x:.1f},{y:.1f}" for x, y in
                     (radar_pt(RAD_CX, rad_cy, i, rad_frac(v))
                      for i, v in enumerate(vals)))
    a(f'<polygon points="{shape}" fill="{p["accent"]}" fill-opacity="0.22" '
      f'stroke="{p["accent"]}" stroke-width="2" stroke-linejoin="round"/>')

    # Every axis carries its number as text. A log-scale radar flatters the
    # shape, so the figures — not the polygon — are what should be read.
    for i, (label, key) in enumerate(RAD_AXES):
        lx, ly = radar_pt(RAD_CX, rad_cy, i, 1.0 + 20.0 / RAD_R)
        anchor = "middle" if i == 0 else ("start" if lx > RAD_CX else "end")
        dy = -4 if i == 0 else (10 if ly > rad_cy else 0)
        a(f'<text x="{lx:.0f}" y="{ly + dy:.0f}" font-size="10.5" '
          f'text-anchor="{anchor}" fill="{p["muted"]}">{esc(label)}</text>')
        a(f'<text x="{lx:.0f}" y="{ly + dy + 13:.0f}" font-size="12" '
          f'text-anchor="{anchor}" font-weight="600" fill="{p["ink"]}" '
          f'style="font-variant-numeric:tabular-nums">'
          f'{n(act.get(key, 0))}</text>')

    # ---- footer
    c = d["calendar"]
    a(f'<line x1="28" y1="{rule_y}" x2="{W - 28}" y2="{rule_y}" '
      f'stroke="{p["rule"]}"/>')
    a(f'<text x="28" y="{foot_y}" font-size="11.5" fill="{p["muted"]}">'
      f'{n(c["total"])} contributions in the last year · '
      f'{c["active_days"]} active days · peak {c["peak"]} in a day</text>')
    a(f'<text x="{W - 28}" y="{foot_y}" font-size="11.5" text-anchor="end" '
      f'fill="{p["muted"]}">private repos aggregated by account</text>')
    a('</svg>')
    return "\n".join(out)


# ---------------------------------------------------------------- main

SVG_NS = "{http://www.w3.org/2000/svg}"


def check_static_state(svg):
    """Every animated attribute must already hold its FINAL value statically.

    GitHub serves README SVGs with a `sandbox` CSP, so SMIL never runs there.
    An element whose base attribute is the animation's *start* value renders as
    an empty bar — which is exactly how the first version of this panel shipped.
    """
    root = ElementTree.fromstring(svg)
    bad = []
    for parent in root.iter():
        for ch in parent:
            if ch.tag != SVG_NS + "animate":
                continue
            attr = ch.get("attributeName")
            values = (ch.get("values") or "").split(";")
            if not attr or len(values) < 2:
                continue
            final, base = values[-1], parent.get(attr)
            try:
                if base is None or abs(float(base) - float(final)) > 0.05:
                    bad.append(f"{parent.tag.split('}')[-1]}.{attr}: "
                               f"base={base} final={final}")
            except ValueError:
                if base != final:
                    bad.append(f"{parent.tag.split('}')[-1]}.{attr}")
    return bad


def guard_regression(data):
    """Refuse to overwrite full numbers with a degraded public-only snapshot.

    PAT expiry is otherwise silent: the panel would quietly fall from ~2,600
    commits to ~85 and keep committing that as if it were true."""
    path = os.path.join(ASSETS, "stats.json")
    if not os.path.exists(path):
        return
    try:
        with open(path) as f:
            prev = json.load(f)
    except (ValueError, OSError):
        return
    was = prev.get("totals", {}).get("repos_private", 0)
    if was and not data["totals"]["repos_private"]:
        sys.exit(
            f"refusing to overwrite: the last run resolved {was} private repos, "
            f"this run resolved none. PROFILE_TOKEN is missing or expired.")


def main():
    data = aggregate(collect())
    guard_regression(data)

    os.makedirs(ASSETS, exist_ok=True)
    with open(os.path.join(ASSETS, "stats.json"), "w") as f:
        json.dump(data, f, indent=2)
        f.write("\n")
    for name, pal in (("light", LIGHT), ("dark", DARK)):
        svg = render(data, pal)
        # Parse before writing. A malformed attribute renders as a browser error
        # page rather than a chart, and nothing downstream would catch it.
        try:
            ElementTree.fromstring(svg)
        except ElementTree.ParseError as e:
            sys.exit(f"generated {name} SVG is not well-formed XML: {e}")
        stale = check_static_state(svg)
        if stale:
            sys.exit(f"{name} SVG depends on SMIL for its geometry, which "
                     f"GitHub's sandbox CSP disables:\n  " + "\n  ".join(stale))
        with open(os.path.join(ASSETS, f"stats-{name}.svg"), "w") as f:
            f.write(svg)

    t = data["totals"]
    print(f"\n{t['commits_all_time']:,} commits all-time · "
          f"{t['commits_last_year']:,} last 12mo · "
          f"{t['repos']} repos ({t['repos_private']} private)")
    for o in data["owners"]:
        print(f"  {o['name'][:26]:26} {o['all_time']:>6} all · {o['window']:>6} recent")
    print("  langs: " + ", ".join(f"{l['name']} {l['pct']}%" for l in data["languages"]))
    if data["public_only"]:
        print("\n!! public-only: no private repo resolved. PROFILE_TOKEN missing or expired.",
              file=sys.stderr)


if __name__ == "__main__":
    main()
