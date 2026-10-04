#!/usr/bin/env python3
"""Classify a repo's issues by what triage still owes them.

Usage: triage_state.py <owner/repo> [--marker "Triage:"] [--postponed-label postponed]
                       [--stable-tag-regex REGEX] [--hold-label release-blocker]
                       [--closed N] [--json]

For each open issue:
  NEW              no comment starts with the triage marker
  NEEDS_PR         triaged "implement", no linked PR
  IN_PROGRESS      a linked PR is open
  DONE_NOT_CLOSED  a PR that closes it merged, none open, issue still open. A merged
                   "Part of #N" PR alone doesn't count: the rest of the issue is
                   still owed, so the issue reads as its other state
  BLOCKED          labelled blocked, or a comment says it is on hold / blocked /
                   waiting on an upstream issue that is still open
  UNBLOCKED        every upstream issue it waits on has closed: resume it
  POSTPONED        has the postponed label
  REVISIT          postponed before the newest stable tag: decide again. Stable means
                   the tag matches --stable-tag-regex (default: vX.Y.Z, no pre-release)
  TRIAGED          triaged, nothing pending (clarify, waiting on the reporter, ...)

For the N most recently closed issues (default 20):
  SUSPECT_CLOSE    closed by a commit whose message names "#N" after a closing
                   keyword only mid-line (a quote, a test string), not as a
                   trailer, or closed as COMPLETED with no closer at all, unless it
                   carries --hold-label (a release hold is meant to close by hand)

Exit 0 when nothing needs action, 1 when any issue is NEW, NEEDS_PR, UNBLOCKED,
REVISIT, DONE_NOT_CLOSED, or SUSPECT_CLOSE, 2 on bad input (an issue with more than 100
labels) or a gh failure. It pages past 100 open issues and an issue's 50 comments or 50
cross-references, and back through tags to the newest stable one, with one query when
nothing is capped. Needs the gh CLI, authenticated. Python 3.10+, standard library only.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import subprocess
import sys
from collections.abc import Callable
from typing import Any, NoReturn

OPEN_TIMELINE = """
          nodes {
            ... on CrossReferencedEvent {
              willCloseTarget isCrossRepository
              source { ... on PullRequest { number state title body } }
            }
            ... on ConnectedEvent { subject { ... on PullRequest { number state } } }
          }
"""
CLOSED_REFS = """
          nodes {
            ... on CrossReferencedEvent {
              isCrossRepository
              source { ... on PullRequest { number state } }
            }
          }
"""
TAG_NODES = "nodes { name target { ... on Tag { tagger { date } } ... on Commit { committedDate } } }"
OPEN_ISSUE = (
    """
      nodes {
        number title
        labels(first: 100) { pageInfo { hasNextPage } nodes { name } }
        comments(last: 50) { nodes { body createdAt } }
        timelineItems(itemTypes: [CROSS_REFERENCED_EVENT, CONNECTED_EVENT], first: 50) {
          pageInfo { hasNextPage endCursor }"""
    + OPEN_TIMELINE
    + """
        }
      }
"""
)

QUERY = (
    """
query($owner: String!, $name: String!, $closed: Int!) {
  repository(owner: $owner, name: $name) {
    open: issues(states: OPEN, first: 100, orderBy: {field: CREATED_AT, direction: DESC}) {
      pageInfo { hasNextPage endCursor }"""
    + OPEN_ISSUE
    + """
    }
    tags: refs(refPrefix: "refs/tags/", last: 50, orderBy: {field: TAG_COMMIT_DATE, direction: ASC}) {
      pageInfo { hasPreviousPage startCursor }
      """
    + TAG_NODES
    + """
    }
    closed: issues(states: CLOSED, first: $closed, orderBy: {field: UPDATED_AT, direction: DESC}) {
      nodes {
        number title stateReason
        labels(first: 100) { pageInfo { hasNextPage } nodes { name } }
        refs: timelineItems(itemTypes: [CROSS_REFERENCED_EVENT], first: 50) {
          pageInfo { hasNextPage endCursor }"""
    + CLOSED_REFS
    + """
        }
        timelineItems(itemTypes: [CLOSED_EVENT], last: 1) {
          nodes {
            ... on ClosedEvent {
              closer {
                __typename
                ... on PullRequest { number merged }
                ... on Commit { abbreviatedOid message }
              }
            }
          }
        }
      }
    }
  }
}
"""
)

# Follow-up queries, each run only for a connection that an earlier page reports as capped
# (comments: one that filled its page). Older open issues carry more history: a page of 100
# tripped GitHub's resource limits on astral-sh/uv, so later pages hold 50
OPEN_PAGE = (
    """
query($owner: String!, $name: String!, $cursor: String!) {
  repository(owner: $owner, name: $name) {
    open: issues(states: OPEN, first: 50, after: $cursor, orderBy: {field: CREATED_AT, direction: DESC}) {
      pageInfo { hasNextPage endCursor }"""
    + OPEN_ISSUE
    + """
    }
  }
}
"""
)
COMMENTS_PAGE = """
query($owner: String!, $name: String!, $number: Int!, $cursor: String) {
  repository(owner: $owner, name: $name) {
    issue(number: $number) {
      comments(last: 100, before: $cursor) { pageInfo { hasPreviousPage startCursor } nodes { body createdAt } }
    }
  }
}
"""
TIMELINE_PAGE = (
    """
query($owner: String!, $name: String!, $number: Int!, $cursor: String!) {
  repository(owner: $owner, name: $name) {
    issue(number: $number) {
      timelineItems(itemTypes: [CROSS_REFERENCED_EVENT, CONNECTED_EVENT], first: 100, after: $cursor) {
        pageInfo { hasNextPage endCursor }"""
    + OPEN_TIMELINE
    + """
      }
    }
  }
}
"""
)
REFS_PAGE = (
    """
query($owner: String!, $name: String!, $number: Int!, $cursor: String!) {
  repository(owner: $owner, name: $name) {
    issue(number: $number) {
      refs: timelineItems(itemTypes: [CROSS_REFERENCED_EVENT], first: 100, after: $cursor) {
        pageInfo { hasNextPage endCursor }"""
    + CLOSED_REFS
    + """
      }
    }
  }
}
"""
)
TAGS_PAGE = (
    """
query($owner: String!, $name: String!, $cursor: String!) {
  repository(owner: $owner, name: $name) {
    tags: refs(refPrefix: "refs/tags/", last: 100, before: $cursor, orderBy: {field: TAG_COMMIT_DATE, direction: ASC}) {
      pageInfo { hasPreviousPage startCursor }
      """
    + TAG_NODES
    + """
    }
  }
}
"""
)

COMMENTS_CAP = 50  # comments(last: 50) in OPEN_ISSUE
Variables = dict[str, str | int]
# Runs one GraphQL query with its variables and returns the parsed JSON response
Runner = Callable[[str, Variables], dict[str, Any]]

ACTION = {"NEW", "NEEDS_PR", "UNBLOCKED", "REVISIT", "DONE_NOT_CLOSED", "SUSPECT_CLOSE"}
HOLD = re.compile(r"\b(?:on hold|blocked|waits? on|waiting on|pending|depends on)\b", re.IGNORECASE)
UPSTREAM = re.compile(r"(?:https://github\.com/)?(?P<owner>[\w.-]+)/(?P<name>[\w.-]+)(?:#|/issues/|/pull/)(?P<num>\d+)")
STABLE = re.compile(r"^v?\d+\.\d+\.\d+$")
KEYWORDS = r"(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)"


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


def fail(message: str) -> NoReturn:
    sys.stderr.write(f"error: {message}\n")
    raise SystemExit(2)


def repository(run: Runner, query: str, variables: Variables) -> dict[str, Any]:
    response = run(query, variables)
    if response.get("errors"):
        fail(f"GitHub GraphQL errors: {json.dumps(response['errors'])}")
    data = (response.get("data") or {}).get("repository")
    if data is None:
        fail(f"no repository {variables['owner']}/{variables['name']} in the GraphQL response")
    result: dict[str, Any] = data
    return result


def advance(cursor: str | None, seen: set[str], what: str) -> str:
    """The next page's cursor; a missing or repeated one would page forever"""
    if not cursor or cursor in seen:
        fail(f"GitHub reported more {what}, but the page cursor did not advance ({cursor!r})")
    seen.add(cursor)
    return cursor


def issue_page(run: Runner, query: str, base: Variables, number: int, field: str, cursor: str | None) -> dict[str, Any]:
    """One page of an issue's connection; with no cursor, its first page (for comments, the newest)"""
    variables: Variables = {**base, "number": number}
    if cursor is not None:
        variables["cursor"] = cursor
    issue = repository(run, query, variables).get("issue")
    if issue is None:
        fail(f"issue #{number} vanished while paging its {field}")
    page: dict[str, Any] = issue[field]
    return page


def check_labels(issue: dict[str, Any]) -> None:
    if issue["labels"]["pageInfo"]["hasNextPage"]:
        fail(f"bad input: issue #{issue['number']} has more than 100 labels, which triage_state.py does not page")


def complete_comments(run: Runner, base: Variables, issue: dict[str, Any]) -> None:
    """Page an issue's comments backwards, keeping them oldest first. pageInfo on the first
    query's comments trips GitHub's resource limits on a busy repo, so a full first page
    (COMMENTS_CAP) is read again here with it"""
    if len(issue["comments"]["nodes"]) < COMMENTS_CAP:
        return
    comments = issue_page(run, COMMENTS_PAGE, base, issue["number"], "comments", None)
    issue["comments"] = comments
    seen: set[str] = set()
    while comments["pageInfo"]["hasPreviousPage"]:
        cursor = advance(comments["pageInfo"]["startCursor"], seen, f"comments on #{issue['number']}")
        page = issue_page(run, COMMENTS_PAGE, base, issue["number"], "comments", cursor)
        comments["nodes"] = page["nodes"] + comments["nodes"]
        comments["pageInfo"] = page["pageInfo"]


def complete_refs(run: Runner, query: str, base: Variables, issue: dict[str, Any], field: str) -> None:
    """Page an issue's cross-references forwards, oldest first"""
    items = issue[field]
    seen: set[str] = set()
    while items["pageInfo"]["hasNextPage"]:
        cursor = advance(items["pageInfo"]["endCursor"], seen, f"cross-references on #{issue['number']}")
        page = issue_page(run, query, base, issue["number"], field, cursor)
        items["nodes"] = items["nodes"] + page["nodes"]
        items["pageInfo"] = page["pageInfo"]


def fetch(repo: str, closed: int, stable_pattern: re.Pattern[str] = STABLE, run: Runner = gh_graphql) -> dict[str, Any]:
    """One query, plus follow-up pages only for the connections it reports as capped"""
    owner, _, name = repo.partition("/")
    if not owner or not name or "/" in name:
        fail(f"repo must be owner/name, got {repo!r}")
    base: Variables = {"owner": owner, "name": name}
    data = repository(run, QUERY, {**base, "closed": closed})

    issues = data["open"]
    seen: set[str] = set()
    while issues["pageInfo"]["hasNextPage"]:
        cursor = advance(issues["pageInfo"]["endCursor"], seen, "open issues")
        page = repository(run, OPEN_PAGE, {**base, "cursor": cursor})["open"]
        issues["nodes"] = issues["nodes"] + page["nodes"]
        issues["pageInfo"] = page["pageInfo"]
    for issue in issues["nodes"]:
        check_labels(issue)
        complete_comments(run, base, issue)
        complete_refs(run, TIMELINE_PAGE, base, issue, "timelineItems")
    for issue in data["closed"]["nodes"]:
        check_labels(issue)
        complete_refs(run, REFS_PAGE, base, issue, "refs")

    # Only the newest stable tag is used: page back until one is in hand, not through every tag
    tags = data["tags"]
    seen = set()
    while tags["pageInfo"]["hasPreviousPage"] and latest_stable(tags["nodes"], stable_pattern) is None:
        cursor = advance(tags["pageInfo"]["startCursor"], seen, "tags")
        page = repository(run, TAGS_PAGE, {**base, "cursor": cursor})["tags"]
        tags["nodes"] = page["nodes"] + tags["nodes"]
        tags["pageInfo"] = page["pageInfo"]
    return data


def closes(pr: dict[str, Any], number: int, *, partial: bool = False) -> bool:
    """A closing keyword (or, with ``partial``, "Part of #N", for a PR covering part of the
    issue) naming the issue in the PR's title or body; GitHub's own willCloseTarget misses some"""
    text = f"{pr.get('title', '')}\n{pr.get('body', '')}"
    words = f"{KEYWORDS}|part of" if partial else KEYWORDS
    return re.search(rf"\b(?:{words}):?\s+#{number}\b", text, re.IGNORECASE) is not None


def linked_prs(issue: dict[str, Any]) -> list[tuple[int, str, bool]]:
    """Same-repo PRs that will close the issue, cover part of it, or were linked to it by
    hand, each with whether it closes the whole issue"""
    found: dict[int, tuple[str, bool]] = {}
    for node in issue["timelineItems"]["nodes"]:
        if "subject" in node:
            pr = node["subject"] or {}
            whole = True
        elif node.get("isCrossRepository"):
            continue
        else:
            pr = node.get("source") or {}
            whole = bool(node.get("willCloseTarget")) or closes(pr, issue["number"])
            if not (whole or closes(pr, issue["number"], partial=True)):
                continue
        if "number" in pr:
            _, before = found.get(pr["number"], ("", False))
            found[pr["number"]] = (pr["state"], whole or before)
    return sorted((n, s, w) for n, (s, w) in found.items())


def merged_mentions(issue: dict[str, Any]) -> list[int]:
    """Same-repo merged PRs that mention the issue at all"""
    return sorted(
        n["source"]["number"]
        for n in issue["refs"]["nodes"]
        if not n.get("isCrossRepository") and n.get("source", {}).get("state") == "MERGED"
    )


def upstream_refs(issue: dict[str, Any]) -> list[tuple[str, str, int]]:
    """Issues elsewhere that a hold comment says this one waits on"""
    refs: set[tuple[str, str, int]] = set()
    for c in issue["comments"]["nodes"]:
        for line in c["body"].splitlines():
            if HOLD.search(line):
                refs.update((m["owner"], m["name"], int(m["num"])) for m in UPSTREAM.finditer(line))
    return sorted(refs)


def upstream_states(refs: set[tuple[str, str, int]]) -> dict[tuple[str, str, int], str]:
    if not refs:
        return {}
    ordered = sorted(refs)
    parts = [
        f'r{i}: repository(owner: "{o}", name: "{n}") {{ issueOrPullRequest(number: {k}) '
        "{ ... on Issue { state } ... on PullRequest { state } } }"
        for i, (o, n, k) in enumerate(ordered)
    ]
    proc = subprocess.run(
        ["gh", "api", "graphql", "-f", "query={" + " ".join(parts) + "}"], capture_output=True, text=True, check=False
    )
    data = json.loads(proc.stdout or "{}").get("data") or {}
    states = {}
    for i, ref in enumerate(ordered):
        node = (data.get(f"r{i}") or {}).get("issueOrPullRequest") or {}
        states[ref] = node.get("state", "UNKNOWN")
    return states


def timestamp(text: str) -> dt.datetime:
    """A GitHub ISO 8601 time; tagger dates carry an offset, comment dates a "Z", which
    fromisoformat rejects before Python 3.11"""
    try:
        moment = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        moment = None
    if moment is None or moment.tzinfo is None:
        sys.stderr.write(f"error: gh returned a time without a timezone or not ISO 8601: {text!r}\n")
        raise SystemExit(2)
    return moment


def latest_stable(tags: list[dict[str, Any]], pattern: re.Pattern[str] = STABLE) -> tuple[dt.datetime, str] | None:
    stable = []
    for t in tags:
        target = t["target"] or {}
        date = (target.get("tagger") or {}).get("date") or target.get("committedDate")
        if date and pattern.match(t["name"]):
            stable.append((timestamp(date), t["name"]))
    return max(stable) if stable else None


def classify_open(
    issue: dict[str, Any],
    marker: str,
    postponed: str,
    states: dict[tuple[str, str, int], str],
    stable: tuple[dt.datetime, str] | None,
) -> tuple[str, str]:
    labels = {n["name"] for n in issue["labels"]["nodes"]}
    triaged = [c for c in issue["comments"]["nodes"] if c["body"].lstrip().startswith(marker)]
    triage = [c["body"] for c in triaged]
    prs = linked_prs(issue)
    open_prs = [n for n, s, _ in prs if s == "OPEN"]
    merged = [n for n, s, whole in prs if s == "MERGED" and whole]
    note = " ".join(f"#{n}:{s.lower()}" + ("" if whole else "(part)") for n, s, whole in prs)
    if open_prs:
        return "IN_PROGRESS", note
    if merged:
        return "DONE_NOT_CLOSED", note
    waits = upstream_refs(issue)
    if waits or "blocked" in labels:
        shown = " ".join(f"{o}/{n}#{k}:{states.get((o, n, k), '?').lower()}" for o, n, k in waits)
        still = [r for r in waits if states.get(r) != "CLOSED" and states.get(r) != "MERGED"]
        if waits and not still:
            return "UNBLOCKED", shown
        return "BLOCKED", shown or "labelled blocked"
    if postponed in labels:
        # Comments come oldest first: a re-decision after the tag clears REVISIT
        if stable and triaged and timestamp(triaged[-1]["createdAt"]) < stable[0]:
            return "REVISIT", f"postponed before {stable[1]}"
        return "POSTPONED", note
    if not triage:
        return "NEW", note
    if re.search(r"\bimplement\b", triage[-1], re.IGNORECASE):
        return "NEEDS_PR", note
    return "TRIAGED", note


def classify_closed(issue: dict[str, Any], hold: str) -> tuple[str, str] | None:
    if hold in {n["name"] for n in issue["labels"]["nodes"]}:
        return None
    events = issue["timelineItems"]["nodes"]
    closer = events[0].get("closer") if events else None
    number = issue["number"]
    if closer is None:
        if issue["stateReason"] == "COMPLETED" and not merged_mentions(issue):
            return "SUSPECT_CLOSE", "closed as completed by hand, and no merged PR mentions it"
        return None
    if closer["__typename"] == "PullRequest":
        return None if closer["merged"] else ("SUSPECT_CLOSE", f"closer PR #{closer['number']} is not merged")
    message: str = closer.get("message", "")
    trailer = re.compile(rf"^\s*(?:[-*]\s*)?{KEYWORDS}:?\s+#{number}\b", re.IGNORECASE | re.MULTILINE)
    if trailer.search(message):
        return None
    return "SUSPECT_CLOSE", f"commit {closer.get('abbreviatedOid')} names #{number} only mid-line"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("repo", help="owner/name")
    parser.add_argument("--marker", default="Triage:", help="prefix of a triage comment")
    parser.add_argument("--postponed-label", default="postponed")
    parser.add_argument(
        "--stable-tag-regex",
        default=STABLE.pattern,
        help="tags that count as stable releases, e.g. '^\\d{4}\\.\\d{2}' for CalVer",
    )
    parser.add_argument(
        "--hold-label", default="release-blocker", help="closed issues with this label were closed by hand on purpose"
    )
    parser.add_argument("--closed", type=int, default=20, help="recently closed issues to check")
    parser.add_argument("--json", action="store_true", help="JSON lines instead of a table")
    args = parser.parse_args()
    if not 0 <= args.closed <= 100:
        parser.error("--closed must be 0..100")
    try:
        stable_pattern = re.compile(args.stable_tag_regex)
    except re.error as exc:
        parser.error(f"--stable-tag-regex: {exc}")

    data = fetch(args.repo, max(args.closed, 1), stable_pattern)
    rows: list[dict[str, Any]] = []
    refs = {r for issue in data["open"]["nodes"] for r in upstream_refs(issue)}
    states = upstream_states(refs)
    stable = latest_stable(data["tags"]["nodes"], stable_pattern)
    for issue in data["open"]["nodes"]:
        state, note = classify_open(issue, args.marker, args.postponed_label, states, stable)
        rows.append({"number": issue["number"], "state": state, "title": issue["title"], "note": note})
    for issue in data["closed"]["nodes"][: args.closed]:
        verdict = classify_closed(issue, args.hold_label)
        if verdict is not None:
            rows.append({"number": issue["number"], "state": verdict[0], "title": issue["title"], "note": verdict[1]})

    order = [
        "SUSPECT_CLOSE",
        "DONE_NOT_CLOSED",
        "UNBLOCKED",
        "REVISIT",
        "NEW",
        "NEEDS_PR",
        "IN_PROGRESS",
        "BLOCKED",
        "TRIAGED",
        "POSTPONED",
    ]
    rows.sort(key=lambda r: (order.index(r["state"]), -r["number"]))
    for r in rows:
        if args.json:
            print(json.dumps(r, sort_keys=True))
        else:
            print(f"#{r['number']:<5} {r['state']:<16} {r['title'][:70]}" + (f"  [{r['note']}]" if r["note"] else ""))
    return 1 if any(r["state"] in ACTION for r in rows) else 0


if __name__ == "__main__":
    sys.exit(main())
