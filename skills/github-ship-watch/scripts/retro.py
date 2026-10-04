#!/usr/bin/env python3
"""Gather a repo's weekly retro, and check its proposals against the open issues.

Usage: retro.py gather <owner/repo> [--days 7] [--environment NAME ...]
                       [--incident-label incident] [--bot LOGIN ...] [--json]
       retro.py dedupe <owner/repo> --title TITLE [--title TITLE ...] [--json]

gather   runs metrics.py over the --days to now and over the --days before that, and prints
         each measure for both windows; then the pull requests of the window that were
         refused (closed without a merge) or reworked (merged after a review requested
         changes). Read-only
dedupe   each proposed issue title against the repo's open issues: DUPLICATE with the open
         issue whose title matches (ignoring case, punctuation, and spacing), else NEW.
         Read-only: the retro opens only the NEW ones

Exit 0 on success (dedupe: every title is new), 1 when dedupe finds a duplicate, 2 on bad
input or a gh or metrics.py failure. Needs the gh CLI, authenticated. Python 3.10+, standard
library only.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

METRICS = Path(__file__).resolve().parent / "metrics.py"
PR_LIMIT = 500  # closed pull requests in one window; more is refused, not truncated
ISSUE_LIMIT = 1000


@dataclass(frozen=True)
class Change:
    measure: str
    previous: str
    current: str


@dataclass(frozen=True)
class Pull:
    number: int
    title: str
    url: str
    why: str


@dataclass(frozen=True)
class Verdict:
    title: str
    duplicate: int | None  # the open issue it matches
    existing: str


def fail(message: str) -> SystemExit:
    sys.stderr.write(f"error: {message}\n")
    return SystemExit(2)


def gh(cmd: list[str]) -> Any:
    proc = subprocess.run(["gh", *cmd], capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise fail(f"gh {' '.join(cmd[:2])}: {proc.stderr.strip()}")
    return json.loads(proc.stdout)


def timestamp(text: str) -> dt.datetime:
    return dt.datetime.fromisoformat(text.replace("Z", "+00:00"))


# -- gather ---------------------------------------------------------------------------------


def compare(previous: dict[str, Any], current: dict[str, Any]) -> list[Change]:
    """Each measure of the current window beside the previous window's, in metrics.py's order"""
    before = {m["measure"]: m["text"] for m in previous["measures"]}
    missing = [m["measure"] for m in current["measures"] if m["measure"] not in before]
    if missing:
        raise fail(f"the earlier window has no {', '.join(missing)}")
    return [Change(m["measure"], before[m["measure"]], m["text"]) for m in current["measures"]]


def classify(pulls: list[dict[str, Any]], start: dt.datetime, end: dt.datetime) -> tuple[list[Pull], list[Pull]]:
    """The window's refused pull requests (closed unmerged) and reworked ones (merged after a
    review requested changes), each newest first as gh lists them"""
    refused, reworked = [], []
    for p in pulls:
        if p["mergedAt"] is None:
            if p["closedAt"] is not None and start <= timestamp(p["closedAt"]) <= end:
                refused.append(Pull(p["number"], p["title"], p["url"], "closed without a merge"))
            continue
        if not start <= timestamp(p["mergedAt"]) <= end:
            continue
        asked = sorted(
            {(r["author"] or {}).get("login", "ghost") for r in p["reviews"] if r["state"] == "CHANGES_REQUESTED"}
        )
        if asked:
            reworked.append(Pull(p["number"], p["title"], p["url"], "changes requested by @" + ", @".join(asked)))
    return refused, reworked


def metrics(repo: str, days: int, until: str | None, extra: list[str]) -> dict[str, Any]:
    cmd = [sys.executable, str(METRICS), repo, "--days", str(days), "--json", *extra]
    if until is not None:
        cmd += ["--until", until]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise fail(f"metrics.py: {proc.stderr.strip()}")
    found: dict[str, Any] = json.loads(proc.stdout)
    return found


def gather(args: argparse.Namespace) -> int:
    extra = [f"--environment={e}" for e in args.environment] + [f"--bot={b}" for b in args.bot]
    extra.append(f"--incident-label={args.incident_label}")
    current = metrics(args.repo, args.days, None, extra)
    previous = metrics(args.repo, args.days, current["start"], extra)
    start, end = timestamp(current["start"]), timestamp(current["end"])
    fields = "number,title,url,mergedAt,closedAt,reviews"
    search = f"closed:>={start:%Y-%m-%d}"
    cmd = [
        "pr",
        "list",
        "-R",
        args.repo,
        "--state",
        "closed",
        "--search",
        search,
        "-L",
        str(PR_LIMIT),
        "--json",
        fields,
    ]
    pulls = gh(cmd)
    if len(pulls) >= PR_LIMIT:
        raise fail(f"{args.repo} closed {PR_LIMIT}+ pull requests since {start:%Y-%m-%d}; use a shorter --days")
    refused, reworked = classify(pulls, start, end)
    changes = compare(previous, current)
    if args.json:
        window = {"start": current["start"], "end": current["end"], "previous_start": previous["start"]}
        report = {
            "window": window,
            "metrics": [asdict(c) for c in changes],
            "refused": [asdict(p) for p in refused],
            "reworked": [asdict(p) for p in reworked],
        }
        print(json.dumps(report, indent=2, sort_keys=True))
        return 0
    print(f"{args.repo}: the {args.days} days to {end:%Y-%m-%d %H:%M} UTC, against the {args.days} days before")
    for c in changes:
        print(f"  {c.measure:<22} {c.previous:<28} -> {c.current}")
    for name, found in (("Refused", refused), ("Reworked", reworked)):
        print(f"{name}: {len(found) or 'none'}")
        for p in found:
            print(f"  #{p.number} {p.title} ({p.why}) {p.url}")
    return 0


# -- dedupe ---------------------------------------------------------------------------------


def normal(title: str) -> str:
    """A title without case, punctuation, or extra spacing"""
    return " ".join(re.sub(r"[\W_]+", " ", title.casefold()).split())


def dedupe(titles: list[str], issues: list[dict[str, Any]]) -> list[Verdict]:
    """Each proposed title: the open issue whose title matches it, or none"""
    open_titles = {normal(i["title"]): i for i in sorted(issues, key=lambda i: int(i["number"]))}
    verdicts = []
    proposed: dict[str, str] = {}
    for title in titles:
        key = normal(title)
        if not key:
            raise fail(f"an empty proposal title: {title!r}")
        if key in proposed:  # both would read NEW and open twice
            raise fail(f"two proposals share a title: {proposed[key]!r} and {title!r}")
        proposed[key] = title
        match = open_titles.get(key)
        verdicts.append(Verdict(title, int(match["number"]), match["title"]) if match else Verdict(title, None, ""))
    return verdicts


def dedupe_command(args: argparse.Namespace) -> int:
    cmd = ["issue", "list", "-R", args.repo, "--state", "open", "-L", str(ISSUE_LIMIT), "--json", "number,title"]
    issues = gh(cmd)
    if len(issues) >= ISSUE_LIMIT:
        raise fail(f"{args.repo} has {ISSUE_LIMIT}+ open issues")
    verdicts = dedupe(args.title, issues)
    for v in verdicts:
        if args.json:
            print(json.dumps(asdict(v), sort_keys=True))
        elif v.duplicate is None:
            print(f"NEW        {v.title}")
        else:
            print(f"DUPLICATE  {v.title} (open as #{v.duplicate}: {v.existing})")
    return 1 if any(v.duplicate is not None for v in verdicts) else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    g = sub.add_parser("gather", help="the two windows' metrics and the window's refused and reworked PRs")
    g.add_argument("repo", help="owner/name")
    g.add_argument("--days", type=int, default=7, help="each window, in days")
    g.add_argument("--environment", action="append", default=[], help="passed to metrics.py")
    g.add_argument("--incident-label", default="incident", help="[operate] incident_label")
    g.add_argument("--bot", action="append", default=[], help="passed to metrics.py")
    g.add_argument("--json", action="store_true")
    d = sub.add_parser("dedupe", help="each proposed title against the open issues")
    d.add_argument("repo", help="owner/name")
    d.add_argument("--title", action="append", required=True, help="a proposed issue's title")
    d.add_argument("--json", action="store_true", help="JSON lines")
    args = parser.parse_args()
    if not re.fullmatch(r"[\w.-]+/[\w.-]+", args.repo):
        parser.error("repo must be owner/name")
    if args.command == "gather":
        if args.days < 1:
            parser.error("--days must be at least 1")
        return gather(args)
    return dedupe_command(args)


if __name__ == "__main__":
    sys.exit(main())
