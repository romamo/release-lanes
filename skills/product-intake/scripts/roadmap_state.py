#!/usr/bin/env python3
"""Where a repo's roadmap stands: milestone progress, work in progress against the limit,
accepted opportunities in no milestone, and the next milestone they would fill.

Usage: roadmap_state.py <owner/repo> [--config PATH] [--specs DIR] [--label opportunity]
                        [--accepted-label planned] [--proposal-label milestone-proposal]
                        [--today YYYY-MM-DD] [--json]

Milestones are the roadmap. The capacity comes from the shipmill config's [roadmap]
section (.github/shipmill.toml in the current directory): wip, the issues
open at once across the open milestones (default 5), and cadence, the weeks from one
milestone's due date to the next (default 2).

  MILESTONE      an open milestone: closed of all issues, and its due date
  OVERDUE        an open milestone past its due date with issues still open
  WIP_OVER       the open milestones hold more open issues than wip
  UNPLANNED      an accepted opportunity (open, labelled --accepted-label) in no milestone
                 and in no proposal that is open or approved and not applied yet; the
                 note ranks it
  PROPOSAL_OPEN  an open --proposal-label issue: waiting for the maintainer's approval
  APPROVED       a proposal closed as completed with a comment starting "approve", whose
                 milestone doesn't exist yet or lacks an opportunity it lists (and that
                 milestone isn't closed): create it, or add them
  UNREADABLE     a proposal whose body lacks the marker line
                 <!-- shipmill:milestone title="<title>" due="YYYY-MM-DD" -->
  NEXT           with no proposal open or waiting to be applied, the unplanned opportunities that fit the free
                 capacity (wip less the open issues in open milestones), and the next due
                 date (the latest open milestone's due date, or today, plus cadence weeks)

A proposal lists its opportunities in a "## Opportunities" section, as #N. An opportunity's
evidence is its requests (the issues and discussions its "## Evidence" section links) plus
the thumbs-up on them and on itself. Its load is one issue for itself plus its spec's build
issues: the "## Issues" lines of the docs/specs/NNN-*.md file its body links, read from
--specs (default docs/specs); one when no linked spec is there yet. Rank orders by evidence
per load, then evidence, then the oldest first; NEXT fills the capacity in that order,
passing over one that doesn't fit.

Exit 0 when nothing needs action, 1 when any row is OVERDUE, WIP_OVER, APPROVED,
UNREADABLE, or NEXT, 2 on bad input (a malformed config) or a gh failure. Needs the gh
CLI, authenticated. Python 3.10+, standard library only.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from intake_state import (
    DISCUSSIONS,
    OLDER_COMMENTS,
    OPPORTUNITIES,
    Runner,
    Variables,
    config_file,
    config_values,
    evidence,
    fail,
    gh_graphql,
    labels,
    member_thumbs,
    paged,
    repository,
    split_repo,
)

WIP, CADENCE = 5, 2  # shipmill's roadmap.WIP and roadmap.CADENCE
LIMITS = {"wip": (1, 100), "cadence": (1, 26)}  # shipmill's RoadmapConfig ranges
ACTION = {"OVERDUE", "WIP_OVER", "APPROVED", "UNREADABLE", "NEXT"}
ORDER = ("WIP_OVER", "OVERDUE", "APPROVED", "UNREADABLE", "NEXT", "UNPLANNED", "PROPOSAL_OPEN", "MILESTONE")
MARKER = re.compile(r'<!--\s*shipmill:milestone\s+title="(?P<title>[^"]+)"\s+due="(?P<due>\d{4}-\d{2}-\d{2})"\s*-->')
APPROVE = re.compile(r"^\s*approved?\b", re.IGNORECASE)
SPEC_LINK = re.compile(r"docs/specs/(?P<number>\d{3,})-[a-z0-9]+(?:-[a-z0-9]+)*\.md")
SPEC_ISSUE = re.compile(r"^\s*-\s+(?:[\w.-]+/[\w.-]+)?#\d+\b")
MILESTONES = """
query($owner: String!, $name: String!, $cursor: String) {
  repository(owner: $owner, name: $name) {
    hasDiscussionsEnabled
    milestones(states: [OPEN, CLOSED], first: 50, after: $cursor, orderBy: {field: CREATED_AT, direction: ASC}) {
      pageInfo { hasNextPage endCursor }
      nodes {
        number title state dueOn
        open: issues(states: OPEN) { totalCount }
        closed: issues(states: CLOSED) { totalCount }
      }
    }
  }
}
"""


@dataclass(frozen=True)
class Roadmap:
    wip: int = WIP
    cadence: int = CADENCE


@dataclass
class Row:
    state: str
    number: int | None
    title: str
    note: str
    opportunities: list[int] = field(default_factory=list)


@dataclass(frozen=True)
class Candidate:
    """An accepted opportunity in no milestone and no pending proposal"""

    number: int
    title: str
    requests: int
    thumbs: int
    load: int
    spec: str  # S-NNN, or "" with no spec read

    @property
    def evidence(self) -> int:
        return self.requests + self.thumbs


def roadmap(path: Path | None) -> Roadmap:
    if path is None:
        return Roadmap()
    found = config_values(path, "roadmap", ("wip", "cadence"))
    values = {"wip": WIP, "cadence": CADENCE, **found}
    for key, (low, high) in LIMITS.items():
        value = values[key]
        if not isinstance(value, int) or isinstance(value, bool):
            fail(f"{path}: [roadmap] {key} must be an integer, got {value!r}")
        if not low <= value <= high:
            fail(f"{path}: [roadmap] {key} must be in {low}..{high}, got {value}")
    return Roadmap(wip=int(values["wip"]), cadence=int(values["cadence"]))


def load(body: str, specs: Path) -> tuple[int, str]:
    """One for the opportunity issue plus its spec's build issues, and the spec's id; one
    build issue is assumed while the linked spec isn't in the checkout"""
    link = SPEC_LINK.search(body or "")
    if link is None:
        return 2, ""
    number = link["number"]
    files = sorted(specs.glob(f"{number}-*.md"))
    if not files:
        return 2, ""
    inside, issues = False, 0
    for line in files[0].read_text(encoding="utf-8").splitlines():
        if line.startswith("## "):
            inside = line[3:].strip() == "Issues"
        elif inside and SPEC_ISSUE.match(line):
            issues += 1
    return 1 + max(issues, 1), f"S-{number}"


def ranked(candidates: list[Candidate]) -> list[Candidate]:
    return sorted(candidates, key=lambda c: (-c.evidence / c.load, -c.evidence, c.number))


def next_milestone(candidates: list[Candidate], free: int) -> list[Candidate]:
    """The ranked candidates that fit the free capacity, in rank order"""
    chosen = []
    for c in ranked(candidates):
        if c.load <= free:
            chosen.append(c)
            free -= c.load
    return chosen


def approved(issue: dict[str, Any]) -> bool:
    return (
        issue["state"] == "CLOSED"
        and issue["stateReason"] == "COMPLETED"
        and any(APPROVE.match(c["body"]) for c in issue["comments"]["nodes"])
    )


def complete_approval(run: Runner, owner: str, name: str, issue: dict[str, Any]) -> None:
    """Page a closed proposal's comments back while none approves it"""
    comments = issue["comments"]
    seen: set[str] = set()
    while issue["state"] == "CLOSED" and comments["pageInfo"]["hasPreviousPage"] and not approved(issue):
        cursor = comments["pageInfo"]["startCursor"]
        if not cursor or cursor in seen:
            fail(f"GitHub reported more comments on #{issue['number']}, but the page cursor did not advance")
        seen.add(cursor)
        variables: Variables = {"owner": owner, "name": name, "number": issue["number"], "cursor": cursor}
        page = (repository(run, OLDER_COMMENTS, variables).get("issue") or {}).get("comments")
        if page is None:
            fail(f"issue #{issue['number']} vanished while paging its comments")
        comments["nodes"] = page["nodes"] + comments["nodes"]
        comments["pageInfo"] = page["pageInfo"]


def day(text: str | None) -> dt.date | None:
    return dt.date.fromisoformat(text[:10]) if text else None


def plan(
    repo: str,
    *,
    config: Roadmap,
    today: dt.date,
    specs: Path,
    label: str,
    accepted: str,
    proposal_label: str,
    run: Runner = gh_graphql,
) -> list[Row]:
    owner, name = split_repo(repo)
    base: Variables = {"owner": owner, "name": name}
    milestones, first = paged(run, MILESTONES, base, "milestones")
    opps, _ = paged(run, OPPORTUNITIES, {**base, "label": label}, "issues")
    proposals, _ = paged(run, OPPORTUNITIES, {**base, "label": proposal_label}, "issues")
    for proposal in proposals:
        complete_approval(run, owner, name, proposal)
    rows: list[Row] = []

    open_ms = [m for m in milestones if m["state"] == "OPEN"]
    in_progress = sum(m["open"]["totalCount"] for m in open_ms)
    for m in open_ms:
        total = m["open"]["totalCount"] + m["closed"]["totalCount"]
        due = day(m["dueOn"])
        note = f"{m['closed']['totalCount']}/{total} closed" + (f", due {due}" if due else ", no due date")
        overdue = due is not None and due < today and m["open"]["totalCount"] > 0
        rows.append(Row("OVERDUE" if overdue else "MILESTONE", m["number"], m["title"], note))
    if in_progress > config.wip:
        note = f"{in_progress} open issues in open milestones, wip {config.wip}"
        rows.append(Row("WIP_OVER", None, "work in progress", note))

    by_title = {m["title"]: m for m in milestones}
    open_opps = {o["number"]: o for o in opps if o["state"] == "OPEN"}
    proposed: set[int] = set()
    held = False  # an open proposal, or an approved one not applied yet, holds the next plan
    for p in proposals:
        listed = sorted(evidence(p["body"], owner, name, "opportunities"))
        marker = MARKER.search(p["body"] or "")
        if p["state"] == "OPEN":
            held = True
            proposed.update(listed)
        if marker is None:
            if p["state"] == "OPEN" or approved(p):
                rows.append(Row("UNREADABLE", p["number"], p["title"], "no shipmill:milestone marker line", listed))
                held = True
                proposed.update(listed)
            continue
        title, due = marker["title"], marker["due"]
        if p["state"] == "OPEN":
            rows.append(Row("PROPOSAL_OPEN", p["number"], p["title"], f"{title} due {due}", listed))
        elif approved(p):
            milestone = by_title.get(title)
            if milestone is None:
                rows.append(Row("APPROVED", p["number"], p["title"], f"create milestone {title} due {due}", listed))
                held = True
                proposed.update(listed)
            elif milestone["state"] == "OPEN":
                placed = {n: (o["milestone"] or {}).get("title") for n, o in open_opps.items()}
                missing = [n for n in listed if n in placed and placed[n] != title]
                if missing:
                    shown = ", ".join(f"#{n}" for n in missing)
                    rows.append(Row("APPROVED", p["number"], p["title"], f"add {shown} to {title}", missing))
                    held = True
                    proposed.update(missing)

    unplanned = [
        o for o in open_opps.values() if accepted in labels(o) and not o["milestone"] and o["number"] not in proposed
    ]
    members = {o["number"]: sorted(evidence(o["body"], owner, name) - {o["number"]}) for o in unplanned}
    thumbs: dict[int, int] = {}
    if unplanned and first["hasDiscussionsEnabled"]:
        discussions, _ = paged(run, DISCUSSIONS, base, "discussions")
        thumbs = {d["number"]: d["reactions"]["totalCount"] for d in discussions}
    wanted = sorted({m for listed in members.values() for m in listed} - set(thumbs))
    thumbs.update(member_thumbs(run, owner, name, wanted))
    candidates = []
    for o in unplanned:
        count = o["reactions"]["totalCount"] + sum(thumbs[m] for m in members[o["number"]])
        weight, spec = load(o["body"], specs)
        candidates.append(Candidate(o["number"], o["title"], len(members[o["number"]]), count, weight, spec))
    free = max(config.wip - in_progress, 0)
    for rank, c in enumerate(ranked(candidates), 1):
        effort = f"load {c.load}" + (f" ({c.spec})" if c.spec else " (no spec yet)")
        note = f"rank {rank}: {c.requests} requests, +1 {c.thumbs}, {effort}"
        if c.load > config.wip:
            note += f"; larger than wip {config.wip}: split it"
        rows.append(Row("UNPLANNED", c.number, c.title, note))
    if not held:
        chosen = next_milestone(candidates, free)
        if chosen:
            latest = max((d for d in (day(m["dueOn"]) for m in open_ms) if d is not None), default=today)
            due = max(latest, today) + dt.timedelta(weeks=config.cadence)
            note = f"capacity {free} of wip {config.wip}, load {sum(c.load for c in chosen)}, due {due}"
            rows.append(Row("NEXT", None, "next milestone", note, [c.number for c in chosen]))
    rows.sort(key=lambda r: ORDER.index(r.state))  # stable: UNPLANNED stays in rank order
    return rows


def show(row: Row) -> str:
    subject = f"#{row.number}" if row.number is not None else "-"
    listed = " ".join(f"#{n}" for n in row.opportunities)
    detail = "; ".join(x for x in (row.note, listed) if x)
    return f"{subject:<6} {row.state:<14} {row.title[:60]}  [{detail}]"


def main() -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("repo", help="owner/name")
    parser.add_argument("--config", type=Path, help="the shipmill config (default: .github/shipmill.toml)")
    parser.add_argument("--specs", type=Path, default=Path("docs/specs"), help="the specs folder")
    parser.add_argument("--label", default="opportunity", help="the label of opportunity issues")
    parser.add_argument("--accepted-label", default="planned", help="the label the maintainer accepts with")
    parser.add_argument("--proposal-label", default="milestone-proposal", help="the label of milestone proposals")
    parser.add_argument("--today", type=dt.date.fromisoformat, default=dt.datetime.now(dt.timezone.utc).date())  # noqa: UP017 (3.10)
    parser.add_argument("--json", action="store_true", help="JSON lines instead of a table")
    args = parser.parse_args()

    config = roadmap(config_file(args.config))
    rows = plan(
        args.repo,
        config=config,
        today=args.today,
        specs=args.specs,
        label=args.label,
        accepted=args.accepted_label,
        proposal_label=args.proposal_label,
    )
    if args.json:
        print(json.dumps({"wip": config.wip, "cadence": config.cadence}))
        for r in rows:
            fields = {"state": r.state, "number": r.number, "title": r.title, "note": r.note}
            print(json.dumps({**fields, "opportunities": r.opportunities}, sort_keys=True))
    else:
        print(f"roadmap: wip {config.wip}, cadence {config.cadence} weeks")
        for r in rows:
            print(show(r))
    return 1 if any(r.state in ACTION for r in rows) else 0


if __name__ == "__main__":
    sys.exit(main())
