#!/usr/bin/env python3
"""Fail the build if a private repo name reached a committed artifact.

This repo is public and the workflow regenerates assets/ every day, so a future
change to build_stats.py could start leaking private repo names without anyone
noticing. This runs after the generator and blocks the commit if it does.

Boundary note: repo names contain '-', which regex \\b treats as a word break,
so \\bHawk-Brain\\b matches inside the *public* Hawk-Brain-V2. Names are matched
as complete tokens instead — no name-continuation character on either side.
"""

import json
import os
import re
import subprocess
import sys

LOGIN = os.environ.get("PROFILE_LOGIN", "erickdhdock")
ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
ASSETS = os.path.join(ROOT, "assets")
NAME_CHAR = r"[A-Za-z0-9._-]"

QUERY = """
query($login: String!, $after: String) {
  %s {
    repositories(first: 100, after: $after%s) {
      pageInfo { hasNextPage endCursor }
      nodes { name nameWithOwner isPrivate }
    }
  }
}"""


def gql(query, variables):
    args = ["gh", "api", "graphql", "-f", "query=" + query]
    for k, v in variables.items():
        if v is not None:
            args += ["-f", f"{k}={v}"]
    p = subprocess.run(args, capture_output=True, text=True)
    if p.returncode != 0:
        raise RuntimeError(p.stderr.strip()[:400])
    return json.loads(p.stdout)["data"]


def private_names():
    owners = [("user(login: $login)", ", ownerAffiliations: OWNER")]
    orgs = gql("query($login: String!) { user(login: $login) "
               "{ organizations(first: 50) { nodes { login } } } }",
               {"login": LOGIN})
    logins = [o["login"] for o in orgs["user"]["organizations"]["nodes"]]
    logins += [l for l in json.loads(os.environ.get("EXTRA_ORGS", "[]"))]

    found = set()

    def walk(root_expr, extra, login):
        after = None
        while True:
            d = gql(QUERY % (root_expr, extra), {"login": login, "after": after})
            root = d[list(d.keys())[0]]
            if not root:
                return
            block = root["repositories"]
            for r in block["nodes"]:
                if r and r["isPrivate"]:
                    found.add(r["name"])
                    found.add(r["nameWithOwner"])
            if not block["pageInfo"]["hasNextPage"]:
                return
            after = block["pageInfo"]["endCursor"]

    for expr, extra in owners:
        walk(expr, extra, LOGIN)
    for login in logins:
        walk("organization(login: $login)", "", login)
    return found


def main():
    names = private_names()
    if not names:
        print("no private repos visible to this token — nothing to check")
        return 0

    blob = []
    for fn in sorted(os.listdir(ASSETS)):
        if fn.endswith((".json", ".svg", ".md")):
            with open(os.path.join(ASSETS, fn)) as f:
                blob.append((fn, f.read()))

    leaks = []
    for name in sorted(names, key=len, reverse=True):
        pat = re.compile(rf"(?<!{NAME_CHAR}){re.escape(name)}(?!{NAME_CHAR})")
        for fn, text in blob:
            if pat.search(text):
                leaks.append((fn, name))

    if leaks:
        print(f"FAIL — private repo name in a committed artifact:", file=sys.stderr)
        for fn, name in leaks:
            print(f"  {fn}: {name}", file=sys.stderr)
        return 1

    print(f"PASS — {len(blob)} artifacts clean against "
          f"{len(names)} private repo identifiers")
    return 0


if __name__ == "__main__":
    sys.exit(main())
