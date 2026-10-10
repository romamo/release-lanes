#!/usr/bin/env python3
"""Classify a repo's issues by what triage still owes them.

Usage: triage_state.py <owner/repo> [--marker "Triage:"] [--postponed-label postponed]
                       [--stable-tag-regex REGEX] [--hold-label release-blocker]
                       [--incident-label incident] [--closed N] [--json] [--wip N]
                       [--bot-login LOGIN] [--trusted-only]

For each open issue:
  NEW              no comment starts with the triage marker
  NEEDS_PR         triaged "implement", no linked PR
  IN_PROGRESS      a linked PR is open
  DONE_NOT_CLOSED  a PR that closes it merged, none open, issue still open. A merged
                   "Part of #N" PR alone doesn't count: the rest of the issue is
                   still owed, so the issue reads as its other state
  BLOCKED          labelled blocked, or a comment says it is on hold / blocked /
                   waiting on an upstream issue that is still open, or on a pull
                   request (such as a spec PR) that is still open. The hold line
                   names it as owner/repo#N or its URL anywhere in the hold
                   phrase's sentence, or as a plain #N (this repo) right after
                   "waits on", "waiting on", "depends on", "blocked by", "blocked
                   on", "decided in", or "on hold until" (a #N elsewhere on the
                   line is context). A sentence ends at ".", "!", or "?" followed
                   by a space and a capital letter (not after "e.g.", "i.e.",
                   "cf.", or "vs."), so a hold word in one sentence of a
                   paragraph doesn't hold on a link in another (#309). Or,
                   once triaged, its body has a "Depends on owner/repo#N" line (or
                   "#N" anywhere on that line, the same repo: a build issue split
                   from a spec) naming an issue still open. Or, once triaged, GitHub
                   records an open issue it is blocked by (blockedBy) or an open
                   sub-issue, in any repo: these and the text holds are one set,
                   so an issue named both ways is listed once. The note marks one
                   only GitHub records with (native) and a sub-issue with (child),
                   such as o/r#12:open(child), and a parent's note starts with
                   "children K/N closed". An untriaged issue reads NEW whatever its
                   body depends on and whatever GitHub records holds it
  UNFILLED         a "Depends on" line of its body still names a placeholder such
                   as #{B1} from specs.py split: put in the dependency's number
  SPEC_REFUSED     a pull request it waits on (such as its spec PR) closed without
                   merging, and no triage comment came after the hold: decide again
                   (revise the spec in a new PR, postpone, or won't fix). A newer
                   triage comment is that decision, and the refused PR stops counting
  UNBLOCKED        every upstream issue it waits on has closed, and every pull
                   request merged: resume it. One closed as not planned shows as
                   owner/repo#N:not_planned in the note: its work never landed, so
                   decide whether the issue still makes sense before resuming
  POSTPONED        has the postponed label
  REVISIT          postponed before the newest stable tag: decide again. Stable means
                   the tag matches --stable-tag-regex (default: vX.Y.Z, no pre-release)
  TRIAGED          triaged, nothing pending (clarify, waiting on the reporter, ...)
  NEEDS_DECISION   labelled needs-decision and its question has no reply: a headless
                   session asked the user and left it (references/needs-decision.md).
                   The question is the newest comment whose first line is the marker
                   <!-- shipmill:needs-decision --> by an OWNER, MEMBER, or COLLABORATOR,
                   or with --bot-login the newest such comment by that login, unless an
                   OWNER, MEMBER, or COLLABORATOR wrote a newer one (then there is no
                   question); a reply is a newer comment without the marker by an OWNER,
                   MEMBER, or COLLABORATOR (other than the --bot-login). A marker comment
                   by anyone else never counts. A labelled issue with no question waits
                   too, until the label comes off
  DECIDED          labelled needs-decision and its question has a reply: act on the
                   reply and remove the label (references/needs-decision.md), after
                   which the issue reads its usual state. Read right after
                   NEEDS_DECISION, before any other state, so an answer that links an
                   open issue doesn't read BLOCKED (#309)
  UNTRUSTED        with --trusted-only, an issue whose author is neither an OWNER,
                   MEMBER, or COLLABORATOR nor the --bot-login, whatever it would read
                   otherwise: an unattended session leaves it to an interactive one (D-16)

For the N most recently closed issues (default 20):
  SUSPECT_CLOSE    closed by a commit whose message names "#N" after a closing
                   keyword only mid-line (a quote, a test string), not as a
                   trailer, or closed as COMPLETED with no closer at all, unless it
                   carries --hold-label (a release hold is meant to close by hand).
                   An issue with --incident-label closed as COMPLETED with no closer
                   isn't one either: an incident closes by hand once the environment
                   is healthy, and ship-watch's POSTMORTEM_DUE follows it up. The
                   script can't read the shipmill config: pass [operate]
                   incident_label here when it isn't "incident"

With --wip N (a work-in-progress limit, such as [roadmap] wip once the config has it), a
last line says how many issues are IN_PROGRESS, the room left under N, and the issues
ready to start (NEEDS_PR or UNBLOCKED), oldest first; with --json, as one JSON object.

--bot-login names the login shipmill's sessions write as (an App's <slug>[bot]).

Exit 0 when nothing needs action, 1 when any issue is NEW, NEEDS_PR, UNBLOCKED, UNFILLED,
SPEC_REFUSED, REVISIT, DONE_NOT_CLOSED, DECIDED, or SUSPECT_CLOSE (never for NEEDS_DECISION
or UNTRUSTED), 2 on bad input (an issue with more than 100
labels) or a gh failure. It pages past 100 open issues and an issue's 50 comments, 50
cross-references, 50 blocking issues, or 50 sub-issues, and back through tags to the
newest stable one, with one query when nothing is capped. A page GitHub rejects for its
resource limits is asked again at half the size, down to 10 items; one rejected at 10
fails with one error line. When GitHub's schema has no blockedBy or subIssues (an
undefinedField error naming one, such as a GitHub Enterprise Server without issue
dependencies), it classifies on the text holds alone and prints one "note: native
relations unavailable: <first error line>" line to stderr; any other gh error, an error
on one issue's relations too, still exits 2. Needs the gh
CLI, authenticated. Python 3.10+, standard library only.
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
# Who wrote it: GraphQL gives a Bot's login without the "[bot]" that REST and --bot-login carry
AUTHOR = "author { __typename login } authorAssociation"
COMMENT = "body createdAt " + AUTHOR
TAG_NODES = "nodes { name target { ... on Tag { tagger { date } } ... on Commit { committedDate } } }"
# The forge's own holds on an issue (spec 017): the issues it is blocked by, and its
# sub-issues, each with its repository, so a relation across repos holds as well
NATIVE_FIELDS = ("blockedBy", "subIssues")
NATIVE_NODES = "pageInfo { hasNextPage endCursor } nodes { number state stateReason repository { nameWithOwner } }"
NATIVE = "".join(f"\n        {field}(first: 50) {{ {NATIVE_NODES} }}" for field in NATIVE_FIELDS)


def open_issue_fields(native: bool) -> str:
    """An open issue's fields; without ``native``, the text holds' only, for a forge or
    token that can't read blockedBy or subIssues"""
    return (
        """
      nodes {
        number title body """
        + AUTHOR
        + """
        labels(first: 100) { pageInfo { hasNextPage } nodes { name } }
        comments(last: 50) { nodes { """
        + COMMENT
        + """ } }
        timelineItems(itemTypes: [CROSS_REFERENCED_EVENT, CONNECTED_EVENT], first: 50) {
          pageInfo { hasNextPage endCursor }"""
        + OPEN_TIMELINE
        + """
        }"""
        + (NATIVE if native else "")
        + """
      }
"""
    )


def first_query(native: bool) -> str:
    return (
        """
query($owner: String!, $name: String!, $size: Int!, $closed: Int!) {
  repository(owner: $owner, name: $name) {
    open: issues(states: OPEN, first: $size, orderBy: {field: CREATED_AT, direction: DESC}) {
      pageInfo { hasNextPage endCursor }"""
        + open_issue_fields(native)
        + CLOSED_AND_TAGS
    )


def open_page(native: bool) -> str:
    return (
        """
query($owner: String!, $name: String!, $size: Int!, $cursor: String!) {
  repository(owner: $owner, name: $name) {
    open: issues(states: OPEN, first: $size, after: $cursor, orderBy: {field: CREATED_AT, direction: DESC}) {
      pageInfo { hasNextPage endCursor }"""
        + open_issue_fields(native)
        + """
    }
  }
}
"""
    )


def native_page(field: str) -> str:
    """The next page of an issue's blockedBy or subIssues"""
    return (
        """
query($owner: String!, $name: String!, $size: Int!, $number: Int!, $cursor: String!) {
  repository(owner: $owner, name: $name) {
    issue(number: $number) {
      """
        + field
        + "(first: $size, after: $cursor) { "
        + NATIVE_NODES
        + """ }
    }
  }
}
"""
    )


CLOSED_AND_TAGS = (
    """
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
QUERY = first_query(native=True)
TEXT_QUERY = first_query(native=False)  # once the forge refused blockedBy or subIssues

# Follow-up queries, each run only for a connection that an earlier page reports as capped
# (comments: one that filled its page). Every query takes its page size as $size, which
# halves when GitHub rejects the page for its resource limits (see SIZES and sized)
OPEN_PAGE = open_page(native=True)
TEXT_OPEN_PAGE = open_page(native=False)
NATIVE_PAGES = {field: native_page(field) for field in NATIVE_FIELDS}
COMMENTS_PAGE = (
    """
query($owner: String!, $name: String!, $size: Int!, $number: Int!, $cursor: String) {
  repository(owner: $owner, name: $name) {
    issue(number: $number) {
      comments(last: $size, before: $cursor) { pageInfo { hasPreviousPage startCursor } nodes { """
    + COMMENT
    + """ } }
    }
  }
}
"""
)
TIMELINE_PAGE = (
    """
query($owner: String!, $name: String!, $size: Int!, $number: Int!, $cursor: String!) {
  repository(owner: $owner, name: $name) {
    issue(number: $number) {
      timelineItems(itemTypes: [CROSS_REFERENCED_EVENT, CONNECTED_EVENT], first: $size, after: $cursor) {
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
query($owner: String!, $name: String!, $size: Int!, $number: Int!, $cursor: String!) {
  repository(owner: $owner, name: $name) {
    issue(number: $number) {
      refs: timelineItems(itemTypes: [CROSS_REFERENCED_EVENT], first: $size, after: $cursor) {
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
query($owner: String!, $name: String!, $size: Int!, $cursor: String!) {
  repository(owner: $owner, name: $name) {
    tags: refs(refPrefix: "refs/tags/", last: $size, before: $cursor,
               orderBy: {field: TAG_COMMIT_DATE, direction: ASC}) {
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
# Each query's first page size, and what a page holds, for the error line. Older open
# issues carry more history: a page of 100 tripped GitHub's resource limits on
# astral-sh/uv, so later open-issue pages start at 50
SIZES = {
    QUERY: 100,
    TEXT_QUERY: 100,
    OPEN_PAGE: 50,
    TEXT_OPEN_PAGE: 50,
    COMMENTS_PAGE: 100,
    TIMELINE_PAGE: 100,
    REFS_PAGE: 100,
    TAGS_PAGE: 100,
    **{query: 100 for query in NATIVE_PAGES.values()},
}
# A rejected page halves down to this floor; one that fails at the floor ends the run.
# At 10 open issues a page asks for a tenth of the first query, so a page still rejected
# there holds an issue too heavy for any size, and pypa/pip's 957 issues would already
# take some 100 calls of about 5 seconds each
MIN_PAGE = 10
RESOURCE_LIMITS = "RESOURCE_LIMITS_EXCEEDED"
Variables = dict[str, str | int]
# Runs one GraphQL query with its variables and returns the parsed JSON response
Runner = Callable[[str, Variables], dict[str, Any]]
Sizes = dict[str, int]


class ResourceLimitsExceeded(Exception):
    """GitHub rejected a query as too costly to run; a smaller page may fit"""


def resource_limited(response: dict[str, Any]) -> bool:
    """Every error is GitHub's resource limit (gh prints one per rejected node), so a
    smaller page may succeed; any other error is real"""
    errors = response.get("errors")
    return bool(errors) and all(isinstance(e, dict) and e.get("type") == RESOURCE_LIMITS for e in errors)


class NativeUnavailable(Exception):
    """The forge has no blockedBy or subIssues: classify on text holds alone"""


NATIVE_NAME = re.compile(r"\b(?:" + "|".join(NATIVE_FIELDS) + r")\b")
UNDEFINED = re.compile(r"doesn't exist on type|isn't a defined input type")


def names_native(error: object) -> bool:
    """Whether a GraphQL error is the schema refusing blockedBy or subIssues: an
    undefinedField naming one, or a message saying one "doesn't exist on type". An error on
    one issue's relations (a blocker the token can't read) is not: falling back then would
    read an issue held only natively as ready, so it fails like any other error"""
    if not isinstance(error, dict):
        return False
    extensions = error.get("extensions")
    message = str(error.get("message", ""))
    if isinstance(extensions, dict) and extensions.get("code") == "undefinedField":
        return extensions.get("fieldName") in NATIVE_FIELDS or NATIVE_NAME.search(message) is not None
    return UNDEFINED.search(message) is not None and NATIVE_NAME.search(message) is not None


def native_refused(response: dict[str, Any]) -> str | None:
    """The first error's first line when every error is the schema refusing blockedBy or
    subIssues (a GitHub Enterprise Server without issue dependencies); None for any other
    response, whose errors are real"""
    errors = response.get("errors")
    if not errors or not isinstance(errors, list) or not all(names_native(e) for e in errors):
        return None
    lines = str(errors[0].get("message", "")).strip().splitlines()
    return lines[0] if lines else json.dumps(errors[0])


INCIDENT_LABEL = "incident"  # the default of [operate] incident_label
DECISION_LABEL = "needs-decision"  # references/needs-decision.md
DECISION_MARKER = "<!-- shipmill:needs-decision -->"  # the first line of a session's question
# The author associations whose comment answers a question, and whose issue an unattended
# session works on (D-16); CONTRIBUTOR, FIRST_TIMER, NONE, and the rest never count
TRUSTED = frozenset({"OWNER", "MEMBER", "COLLABORATOR"})
ACTION = {
    "NEW",
    "NEEDS_PR",
    "UNBLOCKED",
    "UNFILLED",
    "SPEC_REFUSED",
    "REVISIT",
    "DONE_NOT_CLOSED",
    "DECIDED",
    "SUSPECT_CLOSE",
}
HOLD = re.compile(r"\b(?:on hold|blocked|waits? on|waiting on|pending|depends on)\b", re.IGNORECASE)
# Where a sentence of a comment line ends: a hold phrase holds only on the links of its own
# sentence, so "a command whose run length depends on its input" in one sentence of a
# paragraph doesn't hold on an issue another sentence of it names (#309). "e.g. #5" and
# "github.com" don't end one: a sentence ends only before a capital letter, and never after
# "e.g.", "i.e.", "cf.", or "vs.", which a capitalised owner ("e.g. PyCQA/flake8#5") follows
SENTENCE = re.compile(r"(?<!\b[eE]\.g\.)(?<!\b[iI]\.e\.)(?<!\b[cC]f\.)(?<!\b[vV]s\.)(?<=[.!?])\s+(?=[A-Z])")
UPSTREAM = re.compile(r"(?:https://github\.com/)?(?P<owner>[\w.-]+)/(?P<name>[\w.-]+)(?:#|/issues/|/pull/)(?P<num>\d+)")
# A plain #N right after a hold phrase names the issue or pull request of the repo itself
# that the hold waits on; a #N elsewhere on the line ("#59 stays open") is only context
SAME_REPO = re.compile(
    r"\b(?:waits? on|waiting on|depends on|blocked (?:by|on)|decided in|on hold until)[ \t]+#(?P<num>\d+)\b",
    re.IGNORECASE,
)
# A "Depends on" line of an issue body (a build issue split from a spec) and the issues it
# names: owner/repo#N, a URL, or a plain #N for the same repo anywhere on the line, since
# the line itself is the dependency
DEPENDS = re.compile(r"^[ \t]*(?:[-*][ \t]+)?depends on\b:?(?P<refs>.*)$", re.IGNORECASE | re.MULTILINE)
DEPENDENCY = re.compile(
    r"(?:(?:https://github\.com/)?(?P<owner>[\w.-]+)/(?P<name>[\w.-]+)(?:#|/issues/|/pull/)|(?<![\w/])#)(?P<num>\d+)\b"
)
# A dependency specs.py split names by key until the issue is filed: #{B1}, o/r#{B1}
PLACEHOLDER = re.compile(r"#\{(?P<key>[^{}\s]*)\}")
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
        # gh exits 1 on a resource limit, with the response on stdout and its message once
        # per rejected node on stderr: hand the response back to be retried smaller. It
        # exits 1 too on a schema without blockedBy or subIssues: hand that back to be
        # asked again without them
        try:
            rejected = json.loads(proc.stdout)
        except ValueError:
            rejected = None
        if isinstance(rejected, dict) and (resource_limited(rejected) or native_refused(rejected) is not None):
            return rejected
        sys.stderr.write(proc.stderr)
        raise SystemExit(2)
    response: dict[str, Any] = json.loads(proc.stdout)
    return response


def fail(message: str) -> NoReturn:
    sys.stderr.write(f"error: {message}\n")
    raise SystemExit(2)


def repository(run: Runner, query: str, variables: Variables) -> dict[str, Any]:
    response = run(query, variables)
    if resource_limited(response):
        raise ResourceLimitsExceeded
    refused = native_refused(response)
    if refused is not None:
        raise NativeUnavailable(refused)
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


def sized(run: Runner, query: str, variables: Variables, sizes: Sizes, what: str) -> dict[str, Any]:
    """``repository`` at the query's page size, halving it while GitHub rejects the page for
    its resource limits. The smaller size sticks for the query's later pages, and a cursor
    from a bigger page stays valid. A page rejected at MIN_PAGE fails with one line"""
    while True:
        size = sizes[query]
        try:
            return repository(run, query, {**variables, "size": size})
        except ResourceLimitsExceeded:
            if size <= MIN_PAGE:
                fail(
                    f"{variables['owner']}/{variables['name']}: GitHub's resource limits rejected "
                    f"{what} even at {size} per page"
                )
            sizes[query] = max(size // 2, MIN_PAGE)


def issue_page(
    run: Runner, query: str, base: Variables, sizes: Sizes, number: int, field: str, cursor: str | None
) -> dict[str, Any]:
    """One page of an issue's connection; with no cursor, its first page (for comments, the newest)"""
    variables: Variables = {**base, "number": number}
    if cursor is not None:
        variables["cursor"] = cursor
    what = {
        "comments": "comments",
        "timelineItems": "cross-references",
        "refs": "cross-references",
        "blockedBy": "blocking issues",
        "subIssues": "sub-issues",
    }[field]
    issue = sized(run, query, variables, sizes, f"the {what} of #{number}").get("issue")
    if issue is None:
        fail(f"issue #{number} vanished while paging its {field}")
    page: dict[str, Any] = issue[field]
    return page


def check_labels(issue: dict[str, Any]) -> None:
    if issue["labels"]["pageInfo"]["hasNextPage"]:
        fail(f"bad input: issue #{issue['number']} has more than 100 labels, which triage_state.py does not page")


def complete_comments(run: Runner, base: Variables, sizes: Sizes, issue: dict[str, Any]) -> None:
    """Page an issue's comments backwards, keeping them oldest first. pageInfo on the first
    query's comments trips GitHub's resource limits on a busy repo, so a full first page
    (COMMENTS_CAP) is read again here with it"""
    if len(issue["comments"]["nodes"]) < COMMENTS_CAP:
        return
    comments = issue_page(run, COMMENTS_PAGE, base, sizes, issue["number"], "comments", None)
    issue["comments"] = comments
    seen: set[str] = set()
    while comments["pageInfo"]["hasPreviousPage"]:
        cursor = advance(comments["pageInfo"]["startCursor"], seen, f"comments on #{issue['number']}")
        page = issue_page(run, COMMENTS_PAGE, base, sizes, issue["number"], "comments", cursor)
        comments["nodes"] = page["nodes"] + comments["nodes"]
        comments["pageInfo"] = page["pageInfo"]


def complete_refs(run: Runner, query: str, base: Variables, sizes: Sizes, issue: dict[str, Any], field: str) -> None:
    """Page an issue's cross-references (or blockedBy, or subIssues) forwards, oldest first"""
    items = issue[field]
    seen: set[str] = set()
    while items["pageInfo"]["hasNextPage"]:
        what = field if field in NATIVE_FIELDS else "cross-references"
        cursor = advance(items["pageInfo"]["endCursor"], seen, f"{what} on #{issue['number']}")
        page = issue_page(run, query, base, sizes, issue["number"], field, cursor)
        items["nodes"] = items["nodes"] + page["nodes"]
        items["pageInfo"] = page["pageInfo"]


def fetch(repo: str, closed: int, stable_pattern: re.Pattern[str] = STABLE, run: Runner = gh_graphql) -> dict[str, Any]:
    """One query, plus follow-up pages only for the connections it reports as capped. When
    the forge refuses blockedBy or subIssues, one stderr note, then the text holds alone"""
    owner, _, name = repo.partition("/")
    if not owner or not name or "/" in name:
        fail(f"repo must be owner/name, got {repo!r}")
    try:
        return fetch_pages(owner, name, closed, stable_pattern, run, native=True)
    except NativeUnavailable as refused:
        sys.stderr.write(f"note: native relations unavailable: {refused}\n")
    return fetch_pages(owner, name, closed, stable_pattern, run, native=False)


def fetch_pages(
    owner: str, name: str, closed: int, stable_pattern: re.Pattern[str], run: Runner, native: bool
) -> dict[str, Any]:
    base: Variables = {"owner": owner, "name": name}
    sizes = dict(SIZES)
    first, later = (QUERY, OPEN_PAGE) if native else (TEXT_QUERY, TEXT_OPEN_PAGE)
    data = sized(run, first, {**base, "closed": closed}, sizes, "the first query (open issues, tags, recent closes)")

    issues = data["open"]
    # A first query that had to shrink says how heavy this repo's issues are
    sizes[later] = min(sizes[later], sizes[first])
    seen: set[str] = set()
    while issues["pageInfo"]["hasNextPage"]:
        cursor = advance(issues["pageInfo"]["endCursor"], seen, "open issues")
        what = f"the open issues after the first {len(issues['nodes'])}"
        page = sized(run, later, {**base, "cursor": cursor}, sizes, what)["open"]
        issues["nodes"] = issues["nodes"] + page["nodes"]
        issues["pageInfo"] = page["pageInfo"]
    for issue in issues["nodes"]:
        check_labels(issue)
        complete_comments(run, base, sizes, issue)
        complete_refs(run, TIMELINE_PAGE, base, sizes, issue, "timelineItems")
        for field, query in NATIVE_PAGES.items():
            if issue.get(field) is not None:  # absent when the query didn't ask for it
                complete_refs(run, query, base, sizes, issue, field)
    for issue in data["closed"]["nodes"]:
        check_labels(issue)
        complete_refs(run, REFS_PAGE, base, sizes, issue, "refs")

    # Only the newest stable tag is used: page back until one is in hand, not through every tag
    tags = data["tags"]
    seen = set()
    while tags["pageInfo"]["hasPreviousPage"] and latest_stable(tags["nodes"], stable_pattern) is None:
        cursor = advance(tags["pageInfo"]["startCursor"], seen, "tags")
        page = sized(run, TAGS_PAGE, {**base, "cursor": cursor}, sizes, "older tags")["tags"]
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


def upstream_refs(issue: dict[str, Any], repo: tuple[str, str]) -> list[tuple[str, str, int]]:
    """Issues and pull requests that a hold comment says this one waits on"""
    return sorted(hold_refs(issue, repo))


def hold_refs(issue: dict[str, Any], repo: tuple[str, str]) -> dict[tuple[str, str, int], int]:
    """Each issue or pull request a hold comment names, with the index of the newest
    comment naming it: an owner/repo#N or URL in a sentence with a hold phrase, or a plain
    #N right after one, which is one of repo's own (owner, name)"""
    refs: dict[tuple[str, str, int], int] = {}
    for i, c in enumerate(issue["comments"]["nodes"]):
        for line in c["body"].splitlines():
            for sentence in SENTENCE.split(line):
                if HOLD.search(sentence):
                    refs.update(((m["owner"], m["name"], int(m["num"])), i) for m in UPSTREAM.finditer(sentence))
                    refs.update(((*repo, int(m["num"])), i) for m in SAME_REPO.finditer(sentence))
    return refs


def dependency_refs(issue: dict[str, Any], repo: tuple[str, str]) -> list[tuple[str, str, int]]:
    """The issues a "Depends on" line of the issue's body names; a plain #N is one of
    repo's own (owner, name)"""
    found: set[tuple[str, str, int]] = set()
    for line in DEPENDS.finditer(issue["body"]):
        for m in DEPENDENCY.finditer(line["refs"]):
            found.add((m["owner"], m["name"], int(m["num"])) if m["owner"] is not None else (*repo, int(m["num"])))
    return sorted(found)


def unfilled_dependencies(issue: dict[str, Any]) -> list[str]:
    """The placeholders ({B1}) a "Depends on" line of the issue's body still names instead
    of an issue number: specs.py split's keys, never filled in"""
    keys = (m["key"] for line in DEPENDS.finditer(issue["body"]) for m in PLACEHOLDER.finditer(line["refs"]))
    return list(dict.fromkeys(keys))


def upstream_states(refs: set[tuple[str, str, int]]) -> dict[tuple[str, str, int], str]:
    if not refs:
        return {}
    ordered = sorted(refs)
    parts = [
        f'r{i}: repository(owner: "{o}", name: "{n}") {{ issueOrPullRequest(number: {k}) '
        "{ __typename ... on Issue { state stateReason } ... on PullRequest { state } } }"
        for i, (o, n, k) in enumerate(ordered)
    ]
    proc = subprocess.run(
        ["gh", "api", "graphql", "-f", "query={" + " ".join(parts) + "}"], capture_output=True, text=True, check=False
    )
    data = json.loads(proc.stdout or "{}").get("data") or {}
    states = {}
    for i, ref in enumerate(ordered):
        states[ref] = ref_state((data.get(f"r{i}") or {}).get("issueOrPullRequest") or {})
    return states


def ref_state(node: dict[str, Any]) -> str:
    """An upstream issue's state (OPEN, CLOSED, and NOT_PLANNED for one closed as not
    planned: it holds nothing, but its work never landed), or a pull request's (OPEN,
    MERGED, and CLOSED_UNMERGED for one closed without merging, such as a refused spec PR:
    the issue reads SPEC_REFUSED until a newer triage comment decides again). GitHub
    numbers issues and PRs in one sequence, so owner/repo#N may be either"""
    state: str = node.get("state", "UNKNOWN")
    if node.get("__typename") == "Issue" and state == "CLOSED" and node.get("stateReason") == "NOT_PLANNED":
        return "NOT_PLANNED"
    if node.get("__typename") == "PullRequest" and state == "CLOSED":
        return "CLOSED_UNMERGED"
    return state


Ref = tuple[str, str, int]


def native_holds(issue: dict[str, Any]) -> dict[Ref, tuple[str, str]]:
    """The issues the forge says hold this one, each with its kind ("native" for one it
    is blocked by, "child" for a sub-issue, which wins when it is both) and its state as
    ref_state reads it. An issue fetched without them (the forge refused them) has none"""
    holds: dict[Ref, tuple[str, str]] = {}
    for field, kind in (("blockedBy", "native"), ("subIssues", "child")):
        for node in (issue.get(field) or {}).get("nodes", []):
            owner, _, name = node["repository"]["nameWithOwner"].partition("/")
            ref = (owner, name, int(node["number"]))
            if kind == "child" or ref not in holds:
                holds[ref] = (kind, ref_state({**node, "__typename": "Issue"}))
    return holds


def fold(ref: Ref) -> Ref:
    """GitHub owner and repo names ignore case: O/R#3 and o/r#3 are one issue"""
    return (ref[0].lower(), ref[1].lower(), ref[2])


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
    repo: tuple[str, str],
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
    # The body's dependencies are older than every comment; a comment naming one too is newer.
    # They hold a triaged issue only, as the forge's own relations do (spec 017): an
    # untriaged one with "Depends on #48", a blockedBy issue, or a sub-issue still reads NEW
    held = bool(triage)
    body = dependency_refs(issue, repo) if held else []
    named = {**dict.fromkeys(body, -1), **hold_refs(issue, repo)}
    # The forge's relations join the text holds as one set: an issue named both ways counts
    # once, under the text's spelling. The note marks one only the forge records (native)
    # and a sub-issue (child), and a parent's starts with how many of its children closed
    texts = {fold(r): r for r in named}
    native = native_holds(issue) if held else {}
    states = {**states}
    marks: dict[Ref, str] = {}
    children: list[str] = []
    for ref, (kind, state) in native.items():
        key = texts.get(fold(ref), ref)
        named.setdefault(key, -1)
        states[key] = state
        if kind == "child":
            marks[key] = "(child)"
            children.append(state)
        elif fold(ref) not in texts:
            marks[key] = "(native)"
    comments = issue["comments"]["nodes"]
    verdict = max((i for i, c in enumerate(comments) if c["body"].lstrip().startswith(marker)), default=-1)
    # A refused PR named before the newest triage comment was decided again: it holds nothing
    waits = sorted(r for r, i in named.items() if states.get(r) != "CLOSED_UNMERGED" or i >= verdict)
    shown = " ".join(f"{o}/{n}#{k}:{states.get((o, n, k), '?').lower()}{marks.get((o, n, k), '')}" for o, n, k in waits)
    if children:
        closed = sum(1 for s in children if s in ("CLOSED", "NOT_PLANNED"))
        shown = f"children {closed}/{len(children)} closed {shown}"
    unfilled = unfilled_dependencies(issue)
    if unfilled:
        # Which issue it waits on is unknown, so neither BLOCKED nor ready: someone fills it in
        return "UNFILLED", " ".join([*(f"unfilled dependency {{{k}}}" for k in unfilled), shown]).strip()
    if waits or "blocked" in labels:
        if any(states.get(r) == "CLOSED_UNMERGED" for r in waits):
            return "SPEC_REFUSED", shown
        still = [r for r in waits if states.get(r) not in ("CLOSED", "NOT_PLANNED", "MERGED")]
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


def login(author: dict[str, Any] | None) -> str:
    """An author's login as REST and --bot-login spell it: a Bot's with "[bot]", a deleted
    account's as ghost"""
    if not author:
        return "ghost"
    name = str(author.get("login") or "ghost")
    if author.get("__typename") == "Bot" and not name.endswith("[bot]"):
        name += "[bot]"
    return name


def same_login(a: str, b: str) -> bool:
    return a.lower() == b.lower()  # GitHub logins ignore case


def marked(body: str) -> bool:
    """Whether a comment's first line is the needs-decision marker; a quote of it further
    down is no question"""
    return body.lstrip().split("\n", 1)[0].strip() == DECISION_MARKER


def question(comments: list[tuple[str, str, str]], bot_login: str | None) -> int | None:
    """The index of an item's question, or None; comments are (login, author association,
    body), oldest first. A question's first line is the marker. Without --bot-login it is
    the newest such comment by an OWNER, MEMBER, or COLLABORATOR; with it, the newest such
    comment by that login, unless an OWNER, MEMBER, or COLLABORATOR wrote a newer one: a
    person asked after the bot, so the item has no question. A marker comment by any other
    author never counts, so an outsider can't re-park an answered item (#184)"""
    for i in range(len(comments) - 1, -1, -1):
        author, association, body = comments[i]
        if not marked(body):
            continue
        if bot_login is not None and same_login(author, bot_login):
            return i
        if association in TRUSTED:
            return None if bot_login is not None else i
    return None


def replies(comment: tuple[str, str, str], bot_login: str | None) -> bool:
    """A reply: a comment without the marker by an OWNER, MEMBER, or COLLABORATOR other
    than the --bot-login"""
    author, association, body = comment
    if bot_login is not None and same_login(author, bot_login):
        return False
    return association in TRUSTED and not marked(body)


def waits_on_decision(comments: list[tuple[str, str, str]], bot_login: str | None) -> bool:
    """Whether a needs-decision item still waits; comments are (login, author association,
    body), oldest first. It waits until a reply comes after its question; with no question
    at all, a person parked it, so it waits too"""
    asked = question(comments, bot_login)
    if asked is None:
        return True
    return not any(replies(c, bot_login) for c in comments[asked + 1 :])


def trusted(author: str, association: str, bot_login: str | None) -> bool:
    """D-16: an OWNER, MEMBER, or COLLABORATOR, or the sessions' own bot"""
    return association in TRUSTED or (bot_login is not None and same_login(author, bot_login))


def gated(issue: dict[str, Any], bot_login: str | None, trusted_only: bool) -> tuple[str, str] | None:
    """UNTRUSTED (with trusted_only), then NEEDS_DECISION or DECIDED, each read before any
    other state; None when none holds, and the issue reads as classify_open says. DECIDED
    comes first so the reply is acted on, whatever its text links (#309)"""
    author, association = login(issue.get("author")), str(issue.get("authorAssociation") or "NONE")
    if trusted_only and not trusted(author, association, bot_login):
        return "UNTRUSTED", f"opened by @{author} ({association.lower()})"
    if DECISION_LABEL in {n["name"] for n in issue["labels"]["nodes"]}:
        comments = [
            (login(c.get("author")), str(c.get("authorAssociation") or "NONE"), c["body"])
            for c in issue["comments"]["nodes"]
        ]
        asked = question(comments, bot_login)
        if waits_on_decision(comments, bot_login):
            return "NEEDS_DECISION", "waits on a reply" if asked is not None else "labelled, no question"
        # it no longer waits, so it has a question and a reply after it: the newest reply
        reply = next(c for c in reversed(comments) if replies(c, bot_login))
        return "DECIDED", f"answered by @{reply[0]}"
    return None


def classify_closed(issue: dict[str, Any], hold: str, incident: str = INCIDENT_LABEL) -> tuple[str, str] | None:
    labels = {n["name"] for n in issue["labels"]["nodes"]}
    if hold in labels:
        return None
    events = issue["timelineItems"]["nodes"]
    closer = events[0].get("closer") if events else None
    number = issue["number"]
    if closer is None:
        # an incident closes by hand once the environment is healthy again; its follow-up is
        # ship-watch's POSTMORTEM_DUE, not triage
        if issue["stateReason"] == "COMPLETED" and incident not in labels and not merged_mentions(issue):
            return "SUSPECT_CLOSE", "closed as completed by hand, and no merged PR mentions it"
        return None
    if closer["__typename"] == "PullRequest":
        return None if closer["merged"] else ("SUSPECT_CLOSE", f"closer PR #{closer['number']} is not merged")
    message: str = closer.get("message", "")
    trailer = re.compile(rf"^\s*(?:[-*]\s*)?{KEYWORDS}:?\s+#{number}\b", re.IGNORECASE | re.MULTILINE)
    if trailer.search(message):
        return None
    return "SUSPECT_CLOSE", f"commit {closer.get('abbreviatedOid')} names #{number} only mid-line"


def wip_room(rows: list[dict[str, Any]], wip: int) -> dict[str, Any]:
    """How many more issues may start under a WIP limit of ``wip``, counting each issue with
    an open PR as in progress, and the issues ready to start (NEEDS_PR or UNBLOCKED), oldest
    first: build issues are filed in build order"""
    in_progress = sum(1 for r in rows if r["state"] == "IN_PROGRESS")
    ready = sorted(r["number"] for r in rows if r["state"] in ("NEEDS_PR", "UNBLOCKED"))
    return {"wip": wip, "in_progress": in_progress, "room": max(wip - in_progress, 0), "ready": ready}


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
    parser.add_argument(
        "--incident-label",
        default=INCIDENT_LABEL,
        help="the repo's [operate] incident_label: an incident closed by hand is not SUSPECT_CLOSE",
    )
    parser.add_argument("--closed", type=int, default=20, help="recently closed issues to check")
    parser.add_argument("--json", action="store_true", help="JSON lines instead of a table")
    parser.add_argument("--wip", type=int, help="at most N issues in progress at once: report the room left")
    parser.add_argument("--bot-login", help="the login shipmill's sessions write as, such as <slug>[bot]")
    parser.add_argument(
        "--trusted-only", action="store_true", help="report issues by untrusted authors as UNTRUSTED (D-16)"
    )
    args = parser.parse_args()
    if not 0 <= args.closed <= 100:
        parser.error("--closed must be 0..100")
    if args.wip is not None and args.wip < 1:
        parser.error("--wip must be at least 1")
    if args.bot_login is not None and not args.bot_login.strip():
        parser.error("--bot-login must not be empty")
    try:
        stable_pattern = re.compile(args.stable_tag_regex)
    except re.error as exc:
        parser.error(f"--stable-tag-regex: {exc}")

    data = fetch(args.repo, max(args.closed, 1), stable_pattern)
    owner, _, name = args.repo.partition("/")  # fetch checked it is owner/name
    rows: list[dict[str, Any]] = []
    refs = {r for issue in data["open"]["nodes"] for r in upstream_refs(issue, (owner, name))}
    refs |= {r for issue in data["open"]["nodes"] for r in dependency_refs(issue, (owner, name))}
    # The forge's relations came with their states: ask only for the others
    native = {r: s for issue in data["open"]["nodes"] for r, (_, s) in native_holds(issue).items()}
    states = {**upstream_states(refs - set(native)), **native}
    stable = latest_stable(data["tags"]["nodes"], stable_pattern)
    for issue in data["open"]["nodes"]:
        state, note = gated(issue, args.bot_login, args.trusted_only) or classify_open(
            issue, args.marker, args.postponed_label, states, stable, (owner, name)
        )
        rows.append({"number": issue["number"], "state": state, "title": issue["title"], "note": note})
    for issue in data["closed"]["nodes"][: args.closed]:
        verdict = classify_closed(issue, args.hold_label, args.incident_label)
        if verdict is not None:
            rows.append({"number": issue["number"], "state": verdict[0], "title": issue["title"], "note": verdict[1]})

    order = [
        "SUSPECT_CLOSE",
        "DONE_NOT_CLOSED",
        "DECIDED",
        "UNBLOCKED",
        "UNFILLED",
        "SPEC_REFUSED",
        "REVISIT",
        "NEW",
        "NEEDS_PR",
        "IN_PROGRESS",
        "BLOCKED",
        "TRIAGED",
        "POSTPONED",
        "NEEDS_DECISION",
        "UNTRUSTED",
    ]
    rows.sort(key=lambda r: (order.index(r["state"]), -r["number"]))
    for r in rows:
        if args.json:
            print(json.dumps(r, sort_keys=True))
        else:
            print(f"#{r['number']:<5} {r['state']:<16} {r['title'][:70]}" + (f"  [{r['note']}]" if r["note"] else ""))
    if args.wip is not None:
        room = wip_room(rows, args.wip)
        if args.json:
            print(json.dumps(room, sort_keys=True))
        else:
            ready = " ".join(f"#{n}" for n in room["ready"]) or "none"
            print(f"WIP {room['wip']}: {room['in_progress']} in progress, room for {room['room']}; ready: {ready}")
    return 1 if any(r["state"] in ACTION for r in rows) else 0


if __name__ == "__main__":
    sys.exit(main())
