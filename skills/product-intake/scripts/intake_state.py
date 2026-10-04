#!/usr/bin/env python3
"""Where a repo's product feedback stands: which requests no opportunity groups yet, and
where each opportunity issue is in the maintainer's accept or decline.

Usage: intake_state.py <owner/repo> [--config PATH] [--label opportunity]
                       [--accepted-label planned] [--marker "Triage:"] [--skip-label L]...
                       [--category C]... [--json]

Feedback is an open issue, or an open discussion when the repo enables Discussions, that:
  - carries no --skip-label (default: bug and shipyard's own labels) and not --label
  - has no triage verdict of implement, feature, duplicate, or won't fix: the verdict is
    the first bold word of the newest comment starting with --marker
    ("Triage: **implement**"), so a bug or a contract change stays with triage
  - for a discussion, sits in a --category (default: any category but Announcements)

An opportunity is an issue labelled --label, open or closed. Its members are the issues
and discussions its "## Evidence" section links: #N, owner/repo#N of this repo, or a
github.com link to an issue or a discussion. A member stays a member after it closes.

For each opportunity:
  OPPORTUNITY_OPEN  open: waiting for the maintainer to accept or decline it
  ACCEPTED          open and labelled --accepted-label, and its newest triage comment is
                    intake's own (Triage: **opportunity**) or there is none: hand it to
                    github-issue-triage's spec gate as a feature
  HANDED_OFF        accepted, and triage gave it a newer verdict (feature, with its spec
                    PR on a hold line): triage owns it now
  DECLINED          closed as not planned. The note is the reason: the first line of the
                    newest non-triage comment from an hour before the close on. Its members
                    read as declined and are never proposed again
  NO_REASON         closed as not planned with no such comment: the reason isn't recorded
  DONE              closed as completed
  MERGED            closed as a duplicate of another opportunity; it groups nothing, so its
                    members read NEW_FEEDBACK until the surviving opportunity lists them

For feedback and members:
  NEW_FEEDBACK      feedback that no opportunity lists (a MERGED one aside)
  OVERLAP           listed by more than one opportunity that is open or declined

Each opportunity's evidence: its members, and the thumbs-up reactions on them and on the
opportunity itself. The first line (text) or the first JSON line is the [autonomy] intake
level from the shipyard config (.github/shipyard.toml, or its alias
.github/release-policy.toml, in the current directory; none means the default, propose).

Exit 0 when nothing needs action, 1 when any row is NEW_FEEDBACK, ACCEPTED, NO_REASON, or
OVERLAP, 2 on bad input (an issue with more than 100 labels, a malformed config) or a gh
failure. Needs the gh CLI, authenticated. Python 3.10+, standard library only.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, NoReturn

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10: the config is read with regexes instead
    tomllib = None  # type: ignore[assignment]

CONFIGS = (Path(".github/shipyard.toml"), Path(".github/release-policy.toml"))  # the config, then its alias
INTAKE_LEVELS = ("observe", "propose")  # shipyard's autonomy: intake never acts
SKIP_LABELS = ("bug", "shipyard-proposal", "shipyard-hold", "incident", "release-blocker", "milestone-proposal")
SKIP_CATEGORIES = ("Announcements",)
CLOSING_VERDICTS = ("implement", "feature", "duplicate", "won't fix", "won’t fix", "wont fix")
OWN_VERDICT = "opportunity"  # intake's triage comment on an opportunity it opens
REASON_WINDOW = dt.timedelta(hours=1)  # "close with comment" posts the comment just before the close
COMMENTS = 50  # comments(last: 50); an issue with more is paged back only while no triage comment shows
ACTION = {"NEW_FEEDBACK", "ACCEPTED", "NO_REASON", "OVERLAP"}
ORDER = (
    "OVERLAP",
    "NO_REASON",
    "ACCEPTED",
    "NEW_FEEDBACK",
    "OPPORTUNITY_OPEN",
    "HANDED_OFF",
    "DECLINED",
    "DONE",
    "MERGED",
)
LIVE = {"OPPORTUNITY_OPEN", "ACCEPTED", "HANDED_OFF", "DECLINED", "NO_REASON"}  # an OVERLAP counts these

ISSUE_FIELDS = """
        number title url state stateReason closedAt body
        labels(first: 100) { pageInfo { hasNextPage } nodes { name } }
        reactions(content: THUMBS_UP) { totalCount }
        comments(last: 50) { pageInfo { hasPreviousPage startCursor } nodes { body createdAt } }
"""
OPEN_ISSUES = (
    """
query($owner: String!, $name: String!, $cursor: String) {
  repository(owner: $owner, name: $name) {
    hasDiscussionsEnabled
    issues(states: OPEN, first: 50, after: $cursor, orderBy: {field: CREATED_AT, direction: ASC}) {
      pageInfo { hasNextPage endCursor }
      nodes {"""
    + ISSUE_FIELDS
    + """      }
    }
  }
}
"""
)
OPPORTUNITIES = (
    """
query($owner: String!, $name: String!, $label: String!, $cursor: String) {
  repository(owner: $owner, name: $name) {
    issues(labels: [$label], first: 50, after: $cursor, orderBy: {field: CREATED_AT, direction: ASC}) {
      pageInfo { hasNextPage endCursor }
      nodes {"""
    + ISSUE_FIELDS
    + """      }
    }
  }
}
"""
)
DISCUSSIONS = """
query($owner: String!, $name: String!, $cursor: String) {
  repository(owner: $owner, name: $name) {
    discussions(first: 50, after: $cursor, orderBy: {field: CREATED_AT, direction: ASC}) {
      pageInfo { hasNextPage endCursor }
      nodes { number title url closed category { name } reactions(content: THUMBS_UP) { totalCount } }
    }
  }
}
"""
OLDER_COMMENTS = """
query($owner: String!, $name: String!, $number: Int!, $cursor: String!) {
  repository(owner: $owner, name: $name) {
    issue(number: $number) {
      comments(last: 50, before: $cursor) { pageInfo { hasPreviousPage startCursor } nodes { body createdAt } }
    }
  }
}
"""
REF = re.compile(
    r"https?://github\.com/(?P<o1>[\w.-]+)/(?P<n1>[\w.-]+)/(?:issues|discussions|pull)/(?P<k1>\d+)"
    r"|(?<![\w/#])(?:(?P<o2>[\w.-]+)/(?P<n2>[\w.-]+))?#(?P<k2>\d+)\b"
)
MEMBER_THUMBS = "... on Issue { reactions(content: THUMBS_UP) { totalCount } }"
HEADING = re.compile(r"^#{1,6}\s+(.*?)\s*$")
Variables = dict[str, Any]
# Runs one GraphQL query with its variables and returns the parsed JSON response
Runner = Callable[[str, Variables], dict[str, Any]]


def fail(message: str) -> NoReturn:
    sys.stderr.write(f"error: {message}\n")
    raise SystemExit(2)


def gh_graphql(query: str, variables: Variables) -> dict[str, Any]:
    """The default runner: one ``gh api graphql`` call"""
    cmd = ["gh", "api", "graphql", "-f", f"query={query}"]
    for key, value in variables.items():
        # -F sends an int as a number; -f keeps a string a string (a cursor, a numeric repo name)
        cmd += ["-F" if isinstance(value, int) else "-f", f"{key}={value}"]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        sys.stderr.write(proc.stderr)
        raise SystemExit(2)
    response: dict[str, Any] = json.loads(proc.stdout)
    return response


def split_repo(repo: str) -> tuple[str, str]:
    owner, _, name = repo.partition("/")
    if not owner or not name or "/" in name:
        fail(f"repo must be owner/name, got {repo!r}")
    return owner, name


def repository(run: Runner, query: str, variables: Variables) -> dict[str, Any]:
    response = run(query, variables)
    if response.get("errors"):
        fail(f"GitHub GraphQL errors: {json.dumps(response['errors'])}")
    data = (response.get("data") or {}).get("repository")
    if data is None:
        fail(f"no repository {variables['owner']}/{variables['name']} in the GraphQL response")
    result: dict[str, Any] = data
    return result


def paged(run: Runner, query: str, variables: Variables, key: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Every node of one connection, oldest first, and the first page's repository fields"""
    first: dict[str, Any] | None = None
    nodes: list[dict[str, Any]] = []
    cursor: str | None = None
    seen: set[str] = set()
    while True:
        data = repository(run, query, {**variables, **({"cursor": cursor} if cursor else {})})
        first = first if first is not None else data
        nodes += data[key]["nodes"]
        info = data[key]["pageInfo"]
        if not info["hasNextPage"]:
            return nodes, first
        cursor = info["endCursor"]
        if not cursor or cursor in seen:
            fail(f"GitHub reported more {key}, but the page cursor did not advance ({cursor!r})")
        seen.add(cursor)


def labels(issue: dict[str, Any]) -> set[str]:
    if issue["labels"]["pageInfo"]["hasNextPage"]:
        fail(f"bad input: issue #{issue['number']} has more than 100 labels, which intake_state.py does not page")
    return {n["name"] for n in issue["labels"]["nodes"]}


def verdict(body: str, marker: str) -> str | None:
    """A triage comment's verdict, lowercased: its first bold phrase, else its first word;
    None for a comment that isn't a triage comment"""
    text = body.lstrip()
    if not text.startswith(marker):
        return None
    rest = text[len(marker) :].strip()
    bold = re.match(r"\*\*([^*]+)\*\*", rest)
    if bold:
        return bold.group(1).strip().lower()
    return rest.split(maxsplit=1)[0].lower() if rest else ""


def newest_verdict(issue: dict[str, Any], marker: str) -> str | None:
    for comment in reversed(issue["comments"]["nodes"]):
        found = verdict(comment["body"], marker)
        if found is not None:
            return found
    return None


def complete_comments(run: Runner, owner: str, name: str, issue: dict[str, Any], marker: str) -> None:
    """Page an issue's comments back, oldest first, only while none of them is a triage comment:
    the newest triage comment is all intake reads from an issue's history"""
    comments = issue["comments"]
    seen: set[str] = set()
    while comments["pageInfo"]["hasPreviousPage"] and newest_verdict(issue, marker) is None:
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


def evidence(body: str, owner: str, name: str) -> set[int]:
    """The issue and discussion numbers the body's "Evidence" section links in this repo"""
    found: set[int] = set()
    inside = False
    for line in (body or "").splitlines():
        heading = HEADING.match(line)
        if heading:
            inside = heading.group(1).lower() == "evidence"
            continue
        if not inside:
            continue
        for m in REF.finditer(line):
            ref_owner, ref_name = (m["o1"], m["n1"]) if m["k1"] else (m["o2"], m["n2"])
            same = ref_owner is None or (ref_owner.lower(), ref_name.lower()) == (owner.lower(), name.lower())
            if same:
                found.add(int(m["k1"] or m["k2"]))
    return found


def timestamp(text: str) -> dt.datetime:
    """A GitHub ISO 8601 time; "Z", which fromisoformat rejects before Python 3.11, made an offset"""
    try:
        moment = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        fail(f"gh returned a time that isn't ISO 8601: {text!r}")
    if moment.tzinfo is None:
        fail(f"gh returned a time without a timezone: {text!r}")
    return moment


def decline_reason(issue: dict[str, Any], marker: str) -> str | None:
    """The first line of the newest comment that isn't a triage comment, from an hour before
    the close on; None when there is none"""
    closed = timestamp(issue["closedAt"]) - REASON_WINDOW
    for comment in reversed(issue["comments"]["nodes"]):
        if verdict(comment["body"], marker) is None and timestamp(comment["createdAt"]) >= closed:
            lines = [line.strip() for line in comment["body"].strip().splitlines() if line.strip()]
            return lines[0] if lines else None
    return None


def opportunity_state(issue: dict[str, Any], accepted: str, marker: str) -> tuple[str, str]:
    if issue["state"] == "CLOSED":
        if issue["stateReason"] == "NOT_PLANNED":
            reason = decline_reason(issue, marker)
            return ("DECLINED", reason) if reason else ("NO_REASON", "closed as not planned with no comment")
        if issue["stateReason"] == "DUPLICATE":
            return "MERGED", "closed as a duplicate"
        return "DONE", ""
    if accepted not in labels(issue):
        return "OPPORTUNITY_OPEN", ""
    found = newest_verdict(issue, marker)
    if found is None or found.startswith(OWN_VERDICT):
        return "ACCEPTED", "hand it to triage's spec gate"
    return "HANDED_OFF", f"triage: {found}"


def is_feedback(issue: dict[str, Any], skip: set[str], marker: str) -> bool:
    if labels(issue) & skip:
        return False
    found = newest_verdict(issue, marker)
    return found is None or not found.startswith(CLOSING_VERDICTS)


@dataclass
class Item:
    """An issue or a discussion, as intake counts it"""

    number: int
    kind: str  # issue, discussion, or opportunity
    title: str
    thumbs: int
    feedback: bool = False
    state: str = ""
    note: str = ""
    members: list[int] = field(default_factory=list)


@dataclass
class Intake:
    autonomy: str
    opportunities: list[Item]
    feedback: list[Item]
    thumbs: dict[int, int]  # every issue and discussion number with a known thumbs-up count


def fetch(
    repo: str,
    *,
    label: str,
    accepted: str,
    marker: str,
    skip: set[str],
    categories: set[str] | None,
    autonomy: str,
    run: Runner = gh_graphql,
) -> Intake:
    owner, name = split_repo(repo)
    base: Variables = {"owner": owner, "name": name}
    opened, first = paged(run, OPEN_ISSUES, base, "issues")
    opened = [i for i in opened if label not in labels(i)]  # an open opportunity comes with the rest
    opps, _ = paged(run, OPPORTUNITIES, {**base, "label": label}, "issues")
    for issue in opened + opps:
        complete_comments(run, owner, name, issue, marker)
    thumbs = {i["number"]: i["reactions"]["totalCount"] for i in opened + opps}
    feedback = [
        Item(i["number"], "issue", i["title"], i["reactions"]["totalCount"], feedback=True)
        for i in opened
        if is_feedback(i, skip, marker)
    ]
    if first["hasDiscussionsEnabled"]:
        discussions, _ = paged(run, DISCUSSIONS, base, "discussions")
        for d in discussions:
            thumbs[d["number"]] = d["reactions"]["totalCount"]
            category = (d.get("category") or {}).get("name", "")
            counted = category in categories if categories is not None else category not in SKIP_CATEGORIES
            if not d["closed"] and counted:
                feedback.append(Item(d["number"], "discussion", d["title"], d["reactions"]["totalCount"], True))
    opportunities = []
    for issue in opps:
        state, note = opportunity_state(issue, accepted, marker)
        members = sorted(evidence(issue["body"], owner, name) - {issue["number"]})
        item = Item(issue["number"], "opportunity", issue["title"], issue["reactions"]["totalCount"])
        item.state, item.note, item.members = state, note, members
        opportunities.append(item)
    missing = sorted({m for o in opportunities for m in o.members} - set(thumbs))
    thumbs.update(member_thumbs(run, owner, name, missing))
    return Intake(autonomy, opportunities, feedback, thumbs)


def member_thumbs(run: Runner, owner: str, name: str, numbers: list[int]) -> dict[int, int]:
    """Thumbs-up counts of members fetch didn't see (closed issues), 50 to a query; a number
    that is a pull request or doesn't exist counts 0"""
    found: dict[int, int] = {}
    for start in range(0, len(numbers), 50):
        chunk = numbers[start : start + 50]
        parts = " ".join(f"m{n}: issueOrPullRequest(number: {n}) {{ {MEMBER_THUMBS} }}" for n in chunk)
        query = f"query($owner: String!, $name: String!) {{ repository(owner: $owner, name: $name) {{ {parts} }} }}"
        data = repository(run, query, {"owner": owner, "name": name})
        for n in chunk:
            node = data.get(f"m{n}") or {}
            found[n] = (node.get("reactions") or {}).get("totalCount", 0)
    return found


def rows(intake: Intake) -> list[Item]:
    """The opportunities with their evidence, then NEW_FEEDBACK and OVERLAP"""
    listed: dict[int, list[int]] = {}
    found = [
        replace(o, thumbs=o.thumbs + sum(intake.thumbs.get(m, 0) for m in o.members)) for o in intake.opportunities
    ]
    for o in intake.opportunities:
        if o.state != "MERGED":
            for m in o.members:
                listed.setdefault(m, []).append(o.number)
    for item in intake.feedback:
        if item.number not in listed:
            item.state = "NEW_FEEDBACK"
            item.note = item.kind
            found.append(item)
    states = {o.number: o.state for o in intake.opportunities}
    titles = {i.number: (i.kind, i.title) for i in intake.feedback}
    for number, owners in sorted(listed.items()):
        live = [o for o in owners if states[o] in LIVE]
        if len(live) > 1:
            kind, title = titles.get(number, ("member", ""))
            shown = ", ".join(f"#{o}:{states[o].lower()}" for o in live)
            found.append(Item(number, kind, title, intake.thumbs.get(number, 0), state="OVERLAP", note=shown))
    found.sort(key=lambda r: (ORDER.index(r.state), -r.number))
    return found


# -- the config: [autonomy] intake, read with tomllib on 3.11+, with regexes on 3.10, where any
# form but a plain key = value line in a plain [table] is refused rather than misread


def config_file(given: Path | None) -> Path | None:
    if given is not None:
        if not given.is_file():
            fail(f"no config at {given}")
        return given
    found = [p for p in CONFIGS if p.is_file()]
    if len(found) > 1:
        fail(f"both {CONFIGS[0]} and {CONFIGS[1]} exist; keep one")
    return found[0] if found else None


def config_values(path: Path, table: str, keys: tuple[str, ...]) -> dict[str, object]:
    """The keys a config table sets, of those asked for"""
    text = path.read_text(encoding="utf-8")
    if tomllib is not None:
        try:
            raw = tomllib.loads(text)
        except tomllib.TOMLDecodeError as exc:
            fail(f"{path}: {exc}")
        section = raw.get(table, {})
        if not isinstance(section, dict):
            fail(f"{path}: {table} must be a table")
        return {k: section[k] for k in keys if k in section}
    return config_values_310(text, path, table, keys)


def config_values_310(text: str, path: Path, table: str, keys: tuple[str, ...]) -> dict[str, object]:
    unreadable = f"{path}: can't read [{table}] on Python 3.10: use 3.11+, or plain key = value lines in [{table}]"
    body: list[str] | None = None
    current = ""
    for line in text.splitlines():
        header = re.match(r"^\s*\[\[?\s*([^\[\]]+?)\s*\]\]?\s*(?:#.*)?$", line)
        if header:
            current = ".".join(p.strip().strip("\"'") for p in header.group(1).split("."))
            if current == table:
                body = []
            continue
        if current == "" and re.match(rf"^\s*[\"']?{re.escape(table)}[\"']?\s*[.=]", line):
            fail(unreadable)
        if current == table and body is not None:
            body.append(line)
    found: dict[str, object] = {}
    for line in body or []:
        for key in keys:
            if not re.match(rf"^\s*[\"']?{re.escape(key)}[\"']?\s*=", line):
                continue
            value = re.match(
                r"^[^=]*=\s*(?:\"([^\"\\]*)\"|'([^']*)'|(-?\d+)|(true|false))\s*(?:#.*)?$",
                line,
            )
            if value is None:
                fail(unreadable)
            text_value = value.group(1) if value.group(1) is not None else value.group(2)
            if text_value is not None:
                found[key] = text_value
            elif value.group(3) is not None:
                found[key] = int(value.group(3))
            else:
                found[key] = value.group(4) == "true"
    return found


def intake_autonomy(path: Path | None) -> str:
    if path is None:
        return "propose"
    level = config_values(path, "autonomy", ("intake",)).get("intake", "propose")
    if level == "act":
        fail(f"{path}: [autonomy] intake is observe or propose: only the maintainer accepts an opportunity")
    if not isinstance(level, str) or level not in INTAKE_LEVELS:
        fail(f"{path}: [autonomy] intake must be one of {list(INTAKE_LEVELS)}, got {level!r}")
    return level


def show(row: Item) -> str:
    detail = []
    if row.kind == "opportunity":
        detail.append(f"{len(row.members)} requests, +1 {row.thumbs}")
    else:
        detail.append(f"+1 {row.thumbs}")
    if row.note:
        detail.append(row.note)
    return f"#{row.number:<5} {row.state:<16} {row.title[:70]}  [{'; '.join(detail)}]"


def main() -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("repo", help="owner/name")
    parser.add_argument("--config", type=Path, help="the shipyard config (default: .github/shipyard.toml or its alias)")
    parser.add_argument("--label", default="opportunity", help="the label of opportunity issues")
    parser.add_argument("--accepted-label", default="planned", help="the label the maintainer accepts with")
    parser.add_argument("--marker", default="Triage:", help="prefix of a triage comment")
    parser.add_argument("--skip-label", action="append", help=f"not feedback (repeatable; default {list(SKIP_LABELS)})")
    parser.add_argument("--category", action="append", help="discussion categories that count (repeatable)")
    parser.add_argument("--json", action="store_true", help="JSON lines instead of a table")
    args = parser.parse_args()

    autonomy = intake_autonomy(config_file(args.config))
    intake = fetch(
        args.repo,
        label=args.label,
        accepted=args.accepted_label,
        marker=args.marker,
        skip=set(args.skip_label or SKIP_LABELS),
        categories=set(args.category) if args.category else None,
        autonomy=autonomy,
    )
    found = rows(intake)
    if args.json:
        print(json.dumps({"autonomy": autonomy}))
        for r in found:
            print(
                json.dumps(
                    {
                        "number": r.number,
                        "kind": r.kind,
                        "state": r.state,
                        "title": r.title,
                        "thumbs": r.thumbs,
                        "members": r.members,
                        "note": r.note,
                    },
                    sort_keys=True,
                )
            )
    else:
        print(f"intake autonomy: {autonomy}")
        for r in found:
            print(show(r))
    return 1 if any(r.state in ACTION for r in found) else 0


if __name__ == "__main__":
    sys.exit(main())
