#!/usr/bin/env python3
"""Measure a repo's issue-to-release line over a window: the four DORA measures and the
factory's own, from what GitHub already holds.

Usage: metrics.py <owner/repo> [--days 30] [--environment NAME ...] [--incident-label incident]
                  [--blocker-label release-blocker] [--bot LOGIN ...] [--json | --markdown]

Measures, each over the --days before now:
  Deploy frequency   deployments that reached success, by environment (created in the
                     window); for a repo that has never deployed, stable GitHub releases
                     (not a draft or a pre-release) published in the window
  Lead time          for each merged PR first shipped by a stable release published in the
                     window: its first commit's author date to that release (median, p90).
                     "First shipped by" means its merge commit is among the commits the
                     release's tag adds over the previous stable version (by version, not by
                     date), from GitHub's compare; a release with no earlier stable version
                     isn't measured. One edge it misreads: a patch release cut from main and
                     published before the window, when the previous version is a later patch
                     on a release/X.Y branch that lacks it (v1.2.1 from main, then the hotfix
                     v1.2.2 on release/1.2 from v1.2.0). The next minor (v1.3.0) adds
                     v1.2.1's commits over v1.2.2, so their lead time runs to v1.3.0. Ruling
                     it out takes a compare against every earlier release, not one
  Change failure     with deployments: issues labelled --incident-label opened in the window
  rate               / deployments. Without: --blocker-label issues opened in the window plus
                     hotfix releases / stable releases. A hotfix release is a stable X.Y.Z
                     (Z > 0) whose tag is on a release/X.Y branch, where shipyard's hotfix
                     lane puts it
  Time to restore    issues labelled --incident-label restored in the window: opened to the
                     first "<env> is healthy again on" comment shipyard operate posts, or to
                     closed, whichever is first (median); still open ones are counted aside
  Issue to release   issues whose first "Released in <tag>" notice (github-pr-triage's
                     shipped.py) was posted in the window: opened to that notice (median, p90)
  Human touch        PRs merged in the window that a person merged or approved / all merged
                     PRs. A person is a GitHub User whose login doesn't end in "[bot]" and
                     isn't a --bot; apps (github-actions, dependabot, mergify) are Bots. An
                     agent that merges with a person's token counts as that person
  Agent share        PRs merged in the window whose own work says an agent made them / all
                     merged PRs: the body carries Claude Code's "Generated with [Claude Code]"
                     footer, or a commit a "Co-Authored-By: Claude" or "Claude-Session:"
                     trailer (AGENT_MARKS, case-insensitive). Of those, how many a person
                     approved in a GitHub review: the human gate

A measure with nothing to measure (no releases, no merges, no deployments) reads "no data",
never 0. Exit 0 on success, 2 on bad input or a gh failure. Every list is paged; a page GitHub
rejects for its resource limits is asked again at half the size, down to 10 items; one
rejected at 10 fails with one error line. Needs the gh CLI, authenticated. Python 3.10+,
standard library only.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import re
import statistics
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, NoReturn

STABLE = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)$")
NOTICE = re.compile(r"^Released in \S+")  # shipped.py's marker, f"Released in {tag}"
HEALTHY = re.compile(r"^\S+ is healthy again on ")  # shipyard operate's comment on an incident
PAGE_INFO = "pageInfo { hasNextPage endCursor }"

# What the work itself says about who made it: a PR is agent-made when its body ("body") or
# any of its commit messages ("commit") matches one of these, ignoring case. Add another
# agent's footer or trailer here
AGENT_MARKS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("body", re.compile(r"Generated with \[Claude Code\]", re.IGNORECASE)),  # Claude Code's PR footer
    ("commit", re.compile(r"^Co-Authored-By: Claude\b", re.IGNORECASE | re.MULTILINE)),
    ("commit", re.compile(r"^Claude-Session:", re.IGNORECASE | re.MULTILINE)),
)

RELEASES = (
    """
query($owner: String!, $name: String!, $size: Int!, $cursor: String) {
  repository(owner: $owner, name: $name) {
    releases(first: $size, after: $cursor, orderBy: {field: CREATED_AT, direction: DESC}) {
      """
    + PAGE_INFO
    + """
      nodes { tagName publishedAt isDraft isPrerelease }
    }
  }
}
"""
)
COMPARE = (
    """
query($owner: String!, $name: String!, $size: Int!, $cursor: String, $base: String!, $head: String!) {
  repository(owner: $owner, name: $name) {
    base: ref(qualifiedName: $base) {
      compare(headRef: $head) {
        commits(first: $size, after: $cursor) { """
    + PAGE_INFO
    + """ nodes { oid committedDate } }
      }
    }
  }
}
"""
)
LINE = """
query($owner: String!, $name: String!, $branch: String!, $head: String!) {
  repository(owner: $owner, name: $name) {
    line: ref(qualifiedName: $branch) { compare(headRef: $head) { status } }
  }
}
"""
PULLS = (
    """
query($owner: String!, $name: String!, $size: Int!, $cursor: String) {
  repository(owner: $owner, name: $name) {
    pullRequests(states: MERGED, first: $size, after: $cursor, orderBy: {field: UPDATED_AT, direction: DESC}) {
      """
    + PAGE_INFO
    + """
      nodes {
        number updatedAt mergedAt body
        mergeCommit { oid }
        mergedBy { __typename login }
        commits(first: 100) { """
    + PAGE_INFO
    + """ nodes { commit { authoredDate message } } }
        reviews(states: APPROVED, first: 50) { pageInfo { hasNextPage } nodes { author { __typename login } } }
      }
    }
  }
}
"""
)
PULL_COMMITS = (
    """
query($owner: String!, $name: String!, $size: Int!, $cursor: String, $number: Int!) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $number) { commits(first: $size, after: $cursor) { """
    + PAGE_INFO
    + """ nodes { commit { authoredDate message } } } }
  }
}
"""
)
DEPLOYMENTS = (
    """
query($owner: String!, $name: String!, $size: Int!, $cursor: String) {
  repository(owner: $owner, name: $name) {
    deployments(first: $size, after: $cursor, orderBy: {field: CREATED_AT, direction: DESC}) {
      totalCount
      """
    + PAGE_INFO
    + """
      nodes { environment createdAt statuses(first: 100) { pageInfo { hasNextPage } nodes { state } } }
    }
  }
}
"""
)
LABELLED = (
    """
query($owner: String!, $name: String!, $size: Int!, $cursor: String, $label: String!, $since: DateTime!) {
  repository(owner: $owner, name: $name) {
    issues(first: $size, after: $cursor, filterBy: {labels: [$label], since: $since},
           orderBy: {field: UPDATED_AT, direction: DESC}) {
      """
    + PAGE_INFO
    + """
      nodes { number createdAt closedAt comments(first: 50) { """
    + PAGE_INFO
    + """ nodes { body createdAt } } }
    }
  }
}
"""
)
LABELLED_COMMENTS = (
    """
query($owner: String!, $name: String!, $size: Int!, $cursor: String, $number: Int!) {
  repository(owner: $owner, name: $name) {
    issue(number: $number) { comments(first: $size, after: $cursor) { """
    + PAGE_INFO
    + """ nodes { body createdAt } } }
  }
}
"""
)
NOTICES = (
    """
query($owner: String!, $name: String!, $size: Int!, $cursor: String, $since: DateTime!) {
  repository(owner: $owner, name: $name) {
    issues(first: $size, after: $cursor, filterBy: {since: $since}, orderBy: {field: UPDATED_AT, direction: DESC}) {
      """
    + PAGE_INFO
    + """
      nodes {
        number createdAt
        comments(last: 30) { pageInfo { hasPreviousPage startCursor } nodes { body createdAt } }
      }
    }
  }
}
"""
)
NOTICE_COMMENTS = """
query($owner: String!, $name: String!, $size: Int!, $cursor: String!, $number: Int!) {
  repository(owner: $owner, name: $name) {
    issue(number: $number) {
      comments(last: $size, before: $cursor) { pageInfo { hasPreviousPage startCursor } nodes { body createdAt } }
    }
  }
}
"""

# Each query's first page size. Issues and pull requests carry nested connections, so they
# start smaller; a rejected page halves (see sized), and the smaller size sticks
SIZES = {
    RELEASES: 100,
    COMPARE: 100,
    PULLS: 50,
    PULL_COMMITS: 100,
    DEPLOYMENTS: 100,
    LABELLED: 50,
    LABELLED_COMMENTS: 100,
    NOTICES: 50,
    NOTICE_COMMENTS: 100,
}
MIN_PAGE = 10
RESOURCE_LIMITS = "RESOURCE_LIMITS_EXCEEDED"
Variables = dict[str, str | int]
# Runs one GraphQL query with its variables and returns the parsed JSON response
Runner = Callable[[str, Variables], dict[str, Any]]
Sizes = dict[str, int]
Node = dict[str, Any]


class ResourceLimitsExceeded(Exception):
    """GitHub rejected a query as too costly to run; a smaller page may fit"""


def resource_limited(response: dict[str, Any]) -> bool:
    """Every error is GitHub's resource limit, so a smaller page may succeed; any other error is real"""
    errors = response.get("errors")
    return bool(errors) and all(isinstance(e, dict) and e.get("type") == RESOURCE_LIMITS for e in errors)


def gh_graphql(query: str, variables: Variables) -> dict[str, Any]:
    """The default runner: one ``gh api graphql`` call"""
    cmd = ["gh", "api", "graphql", "-f", f"query={query}"]
    for key, value in variables.items():
        # -F sends an int as a number; -f keeps a string a string (a cursor, a numeric repo name)
        cmd += ["-F" if isinstance(value, int) else "-f", f"{key}={value}"]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        # gh exits 1 on a resource limit, with the response on stdout: hand it back to be retried smaller
        try:
            rejected = json.loads(proc.stdout)
        except ValueError:
            rejected = None
        if isinstance(rejected, dict) and resource_limited(rejected):
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


def dig(data: dict[str, Any], path: Sequence[str], what: str) -> dict[str, Any]:
    for key in path:
        if data.get(key) is None:
            fail(f"GitHub returned no {key} for {what}")
        data = data[key]
    return data


def pages(
    run: Runner,
    query: str,
    base: Variables,
    sizes: Sizes,
    path: Sequence[str],
    what: str,
    done: Callable[[Node], bool] | None = None,
) -> list[Node]:
    """Every node of the connection at path, paging forwards; paging stops after a page
    holding a node that ``done`` accepts (the list is ordered, so the rest is older)"""
    nodes: list[Node] = []
    seen: set[str] = set()
    variables = dict(base)
    while True:
        connection = dig(sized(run, query, variables, sizes, what), path, what)
        nodes += connection["nodes"]
        if not connection["pageInfo"]["hasNextPage"] or (done and any(done(n) for n in connection["nodes"])):
            return nodes
        variables["cursor"] = advance(connection["pageInfo"]["endCursor"], seen, what)


def timestamp(text: str) -> dt.datetime:
    """A GitHub ISO 8601 time; its "Z" is rejected by fromisoformat before Python 3.11"""
    try:
        moment = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        moment = None
    if moment is None or moment.tzinfo is None:
        fail(f"gh returned a time without a timezone or not ISO 8601: {text!r}")
    return moment


# -- what GitHub holds -------------------------------------------------------------------------


@dataclass(frozen=True)
class Release:
    tag: str
    published: dt.datetime
    version: tuple[int, int, int] | None  # X.Y.Z for a plain version tag


@dataclass(frozen=True)
class Window:
    start: dt.datetime
    end: dt.datetime

    def __contains__(self, moment: dt.datetime) -> bool:
        return self.start <= moment <= self.end

    @property
    def weeks(self) -> float:
        return (self.end - self.start) / dt.timedelta(weeks=1)


@dataclass
class Data:
    """Everything the measures read, fetched once"""

    releases: list[Release]  # stable releases, newest first
    shipped: dict[str, set[str]]  # an in-window release's tag: the commits it first shipped
    hotfixes: set[str]  # in-window release tags on a release/X.Y branch
    pulls: list[Node]
    deployed: bool  # the repo has any deployment (in the chosen environments)
    deployments: list[Node]  # created in the window, in the chosen environments
    incidents: list[Node]
    blockers: list[Node]
    notices: list[Node]
    bots: frozenset[str] = field(default_factory=frozenset)


def stable_releases(nodes: list[Node]) -> list[Release]:
    found = []
    for n in nodes:
        if n["isDraft"] or n["isPrerelease"] or not n["publishedAt"]:
            continue
        m = STABLE.match(n["tagName"])
        version = (int(m[1]), int(m[2]), int(m[3])) if m else None
        found.append(Release(n["tagName"], timestamp(n["publishedAt"]), version))
    return sorted(found, key=lambda r: r.published, reverse=True)


def previous(release: Release, releases: list[Release]) -> Release | None:
    """The highest stable version below the release's, published before it"""
    if release.version is None:
        return None
    lower = [
        r for r in releases if r.version is not None and r.version < release.version and r.published < release.published
    ]
    return max(lower, key=lambda r: r.version or (0, 0, 0)) if lower else None


def fetch(
    repo: str, window: Window, environments: frozenset[str], incident: str, blocker: str, run: Runner = gh_graphql
) -> Data:
    owner, _, name = repo.partition("/")
    if not owner or not name or "/" in name:
        fail(f"repo must be owner/name, got {repo!r}")
    base: Variables = {"owner": owner, "name": name}
    sizes = dict(SIZES)
    since = window.start.isoformat().replace("+00:00", "Z")

    # Every release: a release's previous version may be older than any date cutoff
    releases = stable_releases(pages(run, RELEASES, base, sizes, ["releases"], "releases"))
    recent = [r for r in releases if r.published in window]
    shipped: dict[str, set[str]] = {}
    hotfixes: set[str] = set()
    oldest = window.start
    for release in recent:
        head = f"refs/tags/{release.tag}"
        prev = previous(release, releases)
        if prev is not None:
            what = f"the commits {release.tag} adds over {prev.tag}"
            variables: Variables = {**base, "base": f"refs/tags/{prev.tag}", "head": head}
            commits = pages(run, COMPARE, variables, sizes, ["base", "compare", "commits"], what)
            shipped[release.tag] = {c["oid"] for c in commits}
            oldest = min([oldest, *(timestamp(c["committedDate"]) for c in commits)])
        if release.version is not None and release.version[2] > 0:
            major, minor, _ = release.version
            variables = {**base, "branch": f"refs/heads/release/{major}.{minor}", "head": head}
            line = repository(run, LINE, variables)["line"]  # one status, nothing to page or shrink
            if line is not None and line["compare"]["status"] in ("IDENTICAL", "BEHIND"):
                hotfixes.add(release.tag)

    # Merged PRs, newest update first, back to the oldest commit a release in the window
    # shipped (a merge's commit date is its merge time; a day covers clock skew)
    cutoff = oldest - dt.timedelta(days=1)
    pulls = pages(run, PULLS, base, sizes, ["pullRequests"], "merged pull requests", lambda n: _older(n, cutoff))
    for pr in pulls:
        pull_commits(run, base, sizes, pr)

    # Newest first, back to the window's start; with --environment, also back to a chosen
    # environment's deployment, so one deployed only before the window still counts as deployed
    deploys = pages(
        run,
        DEPLOYMENTS,
        base,
        sizes,
        ["deployments"],
        "deployments",
        lambda n: timestamp(n["createdAt"]) < window.start and (not environments or n["environment"] in environments),
    )
    chosen = [d for d in deploys if not environments or d["environment"] in environments]
    deployed = bool(chosen) if environments else bool(deploys)
    in_window = [d for d in chosen if timestamp(d["createdAt"]) in window]
    for d in in_window:
        if d["statuses"]["pageInfo"]["hasNextPage"] and not _succeeded(d):
            fail(f"a deployment to {d['environment']} at {d['createdAt']} has over 100 statuses, none a success")

    incidents = labelled(run, base, sizes, incident, since)
    blockers = labelled(run, base, sizes, blocker, since)
    notices = notice_issues(run, base, sizes, since, window)
    return Data(releases, shipped, hotfixes, pulls, deployed, in_window, incidents, blockers, notices)


def pull_commits(run: Runner, base: Variables, sizes: Sizes, pr: Node) -> None:
    """Page the PR's commits forwards while none carries an agent's mark"""
    commits = pr["commits"]
    seen: set[str] = set()
    what = f"the commits of PR #{pr['number']}"
    while commits["pageInfo"]["hasNextPage"] and not agent_made(pr):
        cursor = advance(commits["pageInfo"]["endCursor"], seen, what)
        variables: Variables = {**base, "number": pr["number"], "cursor": cursor}
        page = dig(sized(run, PULL_COMMITS, variables, sizes, what), ["pullRequest", "commits"], what)
        commits["nodes"] = commits["nodes"] + page["nodes"]
        commits["pageInfo"] = page["pageInfo"]


def agent_made(pr: Node) -> bool:
    """The PR's body or one of its commits carries one of AGENT_MARKS"""
    texts = {"body": [pr["body"] or ""], "commit": [c["commit"]["message"] for c in pr["commits"]["nodes"]]}
    return any(pattern.search(text) for where, pattern in AGENT_MARKS for text in texts[where])


def _older(node: Node, cutoff: dt.datetime) -> bool:
    return timestamp(node["updatedAt"]) < cutoff


def _succeeded(deployment: Node) -> bool:
    return any(s["state"] == "SUCCESS" for s in deployment["statuses"]["nodes"])


def labelled(run: Runner, base: Variables, sizes: Sizes, label: str, since: str) -> list[Node]:
    """Issues with the label updated since, each with its comments up to the first
    "healthy again" one (paged forwards only while none is found)"""
    what = f"issues labelled {label}"
    issues = pages(run, LABELLED, {**base, "label": label, "since": since}, sizes, ["issues"], what)
    for issue in issues:
        comments = issue["comments"]
        seen: set[str] = set()
        while comments["pageInfo"]["hasNextPage"] and not any(HEALTHY.match(c["body"]) for c in comments["nodes"]):
            cursor = advance(comments["pageInfo"]["endCursor"], seen, f"comments on #{issue['number']}")
            variables: Variables = {**base, "number": issue["number"], "cursor": cursor}
            page = dig(sized(run, LABELLED_COMMENTS, variables, sizes, what), ["issue", "comments"], what)
            comments["nodes"] = comments["nodes"] + page["nodes"]
            comments["pageInfo"] = page["pageInfo"]
    return issues


def notice_issues(run: Runner, base: Variables, sizes: Sizes, since: str, window: Window) -> list[Node]:
    """Issues updated since, each with its newest comments paged back far enough to hold
    its first "Released in" notice when that one may be in the window"""
    issues = pages(run, NOTICES, {**base, "since": since}, sizes, ["issues"], "issues updated in the window")
    for issue in issues:
        comments = issue["comments"]
        seen: set[str] = set()
        while comments["pageInfo"]["hasPreviousPage"] and comments["nodes"] and not _first_notice_known(issue, window):
            cursor = advance(comments["pageInfo"]["startCursor"], seen, f"comments on #{issue['number']}")
            variables: Variables = {**base, "number": issue["number"], "cursor": cursor}
            what = f"the comments of #{issue['number']}"
            page = dig(sized(run, NOTICE_COMMENTS, variables, sizes, what), ["issue", "comments"], what)
            comments["nodes"] = page["nodes"] + comments["nodes"]
            comments["pageInfo"] = page["pageInfo"]
    return issues


def _first_notice_known(issue: Node, window: Window) -> bool:
    """Whether the comments fetched so far settle the issue's first notice for the window:
    one before the window means the first was too (an in-window notice is a later one);
    with none fetched, every comment back past the window's start holds none"""
    nodes = issue["comments"]["nodes"]
    notices = [timestamp(c["createdAt"]) for c in nodes if NOTICE.match(c["body"])]
    if notices:
        return min(notices) < window.start
    return timestamp(nodes[0]["createdAt"]) < window.start


# -- the measures ------------------------------------------------------------------------------


@dataclass(frozen=True)
class Measure:
    name: str
    value: float | None  # None: no data
    text: str  # the value as the table shows it
    detail: str
    unit: str  # of value: "per week", "hours", or "ratio"
    extra: dict[str, Any] = field(default_factory=dict)

    def json(self) -> dict[str, Any]:
        return {"measure": self.name, "value": self.value, "unit": self.unit, "text": self.text, **self.extra}


NO_DATA = "no data"
SHOWN = 5  # release tags a detail names before "..."


def span(hours: float) -> str:
    return f"{hours:.1f} h" if hours < 48 else f"{hours / 24:.1f} d"


def hours(start: dt.datetime, end: dt.datetime) -> float:
    return (end - start) / dt.timedelta(hours=1)


def p90(values: list[float]) -> float:
    """The nearest-rank 90th percentile"""
    ordered = sorted(values)
    return ordered[math.ceil(0.9 * len(ordered)) - 1]


def durations(name: str, values: list[float], detail: str, empty: str) -> Measure:
    if not values:
        return Measure(name, None, NO_DATA, empty, "hours")
    median = statistics.median(values)
    text = f"median {span(median)}, p90 {span(p90(values))}"
    return Measure(name, median, text, detail, "hours", {"p90": p90(values), "count": len(values)})


def in_window_releases(data: Data, window: Window) -> list[Release]:
    return [r for r in data.releases if r.published in window]


def deploy_frequency(data: Data, window: Window) -> Measure:
    name = "Deploy frequency"
    if data.deployed:
        done = [d for d in data.deployments if _succeeded(d)]
        if not done:
            return Measure(name, None, NO_DATA, "no deployment reached success in the window", "per week")
        counts: dict[str, int] = {}
        for d in done:
            counts[d["environment"]] = counts.get(d["environment"], 0) + 1
        rate = len(done) / window.weeks
        detail = ", ".join(f"{env} {n}" for env, n in sorted(counts.items()))
        extra = {"source": "deployments", "count": len(done), "environments": counts}
        return Measure(name, rate, f"{rate:.1f}/week ({len(done)} deploys)", detail, "per week", extra)
    releases = in_window_releases(data, window)
    if not releases:
        return Measure(name, None, NO_DATA, "no deployments, and no stable release in the window", "per week")
    rate = len(releases) / window.weeks
    shown = " ".join(r.tag for r in releases[:SHOWN]) + (" ..." if len(releases) > SHOWN else "")
    detail = f"stable releases, no deployments: {shown}"
    extra = {"source": "releases", "count": len(releases)}
    return Measure(name, rate, f"{rate:.1f}/week ({len(releases)} releases)", detail, "per week", extra)


def lead_time(data: Data, window: Window) -> Measure:
    first_shipped: dict[str, Release] = {}
    for release in sorted(in_window_releases(data, window), key=lambda r: r.published, reverse=True):
        for oid in data.shipped.get(release.tag, ()):
            first_shipped[oid] = release  # oldest release wins: the loop runs newest first
    values = []
    for pr in data.pulls:
        merge = (pr.get("mergeCommit") or {}).get("oid")
        commits = pr["commits"]["nodes"]
        if merge in first_shipped and commits:
            values.append(hours(timestamp(commits[0]["commit"]["authoredDate"]), first_shipped[merge].published))
    shipped = sum(1 for r in in_window_releases(data, window) if r.tag in data.shipped)
    detail = f"{len(values)} PRs in {shipped} releases"
    return durations("Lead time for changes", values, detail, "no merged PR shipped by a stable release in the window")


def change_failure_rate(data: Data, window: Window) -> Measure:
    name = "Change failure rate"
    opened = [i for i in (data.incidents if data.deployed else data.blockers) if timestamp(i["createdAt"]) in window]
    issues = " ".join(f"#{i['number']}" for i in sorted(opened, key=lambda i: i["number"]))
    if data.deployed:
        total = sum(1 for d in data.deployments if _succeeded(d))
        failures, named, source = len(opened), issues, "deployments"
        detail = f"{len(opened)} incidents / {total} deploys"
    else:
        releases = in_window_releases(data, window)
        hot = [r.tag for r in releases if r.tag in data.hotfixes]
        total, failures, source = len(releases), len(opened) + len(hot), "releases"
        named = " ".join(x for x in (issues, *hot) if x)
        detail = f"{len(opened)} blocker issues + {len(hot)} hotfixes / {total} releases"
    extra: dict[str, Any] = {"source": source}
    detail += f": {named}" if named else ""
    if total == 0:
        return Measure(name, None, NO_DATA, f"no {source} in the window", "ratio", extra)
    rate = failures / total
    return Measure(name, rate, f"{rate:.0%}", detail, "ratio", {**extra, "failures": failures, "total": total})


def restored_at(issue: Node) -> dt.datetime | None:
    moments = [timestamp(c["createdAt"]) for c in issue["comments"]["nodes"] if HEALTHY.match(c["body"])]
    if issue["closedAt"]:
        moments.append(timestamp(issue["closedAt"]))
    return min(moments) if moments else None


def time_to_restore(data: Data, window: Window) -> Measure:
    values, still = [], 0
    for issue in data.incidents:
        done = restored_at(issue)
        if done is None:
            still += 1
        elif done in window:
            values.append(hours(timestamp(issue["createdAt"]), done))
    name = "Time to restore"
    if not values:
        return Measure(name, None, NO_DATA, f"no incident restored in the window, {still} open", "hours")
    median = statistics.median(values)
    detail = f"{len(values)} incidents restored, {still} open"
    return Measure(name, median, f"median {span(median)}", detail, "hours", {"count": len(values), "open": still})


def issue_to_release(data: Data, window: Window) -> Measure:
    values = []
    for issue in data.notices:
        notices = [timestamp(c["createdAt"]) for c in issue["comments"]["nodes"] if NOTICE.match(c["body"])]
        if notices and min(notices) in window:
            values.append(hours(timestamp(issue["createdAt"]), min(notices)))
    detail = f'{len(values)} issues told "Released in"'
    return durations("Issue to release", values, detail, 'no "Released in" notice posted in the window')


def person(actor: Node | None, bots: frozenset[str]) -> bool:
    if actor is None or actor["__typename"] != "User":
        return False
    login: str = actor["login"]
    return not login.endswith("[bot]") and login not in bots


def approved_by_person(pr: Node, bots: frozenset[str]) -> bool:
    """A person approved the PR in a GitHub review"""
    reviews = pr["reviews"]
    approver = any(person(r["author"], bots) for r in reviews["nodes"])
    if reviews["pageInfo"]["hasNextPage"] and not approver:
        fail(f"PR #{pr['number']} has over 50 approvals, none by a person")
    return approver


def merged_in(data: Data, window: Window) -> list[Node]:
    return [p for p in data.pulls if p["mergedAt"] and timestamp(p["mergedAt"]) in window]


def human_touch(data: Data, window: Window) -> Measure:
    name = "Human touch"
    merged = merged_in(data, window)
    if not merged:
        return Measure(name, None, NO_DATA, "no PR merged in the window", "ratio")
    by_person = approved = touched = 0
    for pr in merged:
        merger = person(pr["mergedBy"], data.bots)
        approver = approved_by_person(pr, data.bots)
        by_person += merger
        approved += approver
        touched += merger or approver
    rate = touched / len(merged)
    detail = (
        f"{touched} of {len(merged)} merges: {by_person} merged by a person, {approved} approved by one"
        " (an agent merging with a person's token counts as the person)"
    )
    extra = {"merges": len(merged), "merged_by_person": by_person, "approved_by_person": approved}
    return Measure(name, rate, f"{rate:.0%}", detail, "ratio", extra)


def agent_share(data: Data, window: Window) -> Measure:
    name = "Agent share"
    merged = merged_in(data, window)
    if not merged:
        return Measure(name, None, NO_DATA, "no PR merged in the window", "ratio")
    made = [pr for pr in merged if agent_made(pr)]
    gated = sum(approved_by_person(pr, data.bots) for pr in made)
    rate = len(made) / len(merged)
    detail = f"{len(made)} of {len(merged)} merges agent-made; a person approved {gated} of them in a review"
    extra = {"merges": len(merged), "agent_made": len(made), "approved_by_person": gated}
    return Measure(name, rate, f"{rate:.0%}", detail, "ratio", extra)


def measures(data: Data, window: Window) -> list[Measure]:
    return [
        f(data, window)
        for f in (
            deploy_frequency,
            lead_time,
            change_failure_rate,
            time_to_restore,
            issue_to_release,
            human_touch,
            agent_share,
        )
    ]


def render(repo: str, days: int, window: Window, found: list[Measure], form: str) -> str:
    if form == "json":
        return json.dumps(
            {
                "repo": repo,
                "days": days,
                "start": window.start.isoformat(),
                "end": window.end.isoformat(),
                "measures": [m.json() for m in found],
            },
            indent=2,
            sort_keys=True,
        )
    if form == "markdown":
        lines = [
            f"**Factory metrics: {repo}, the {days} days to {window.end:%Y-%m-%d}**",
            "",
            "| Measure | Value | Detail |",
            "|---|---|---|",
            *(f"| {m.name} | {m.text} | {m.detail} |" for m in found),
        ]
        return "\n".join(lines)
    head = f"{repo}: the {days} days to {window.end:%Y-%m-%d %H:%M} UTC"
    return "\n".join([head, *(f"{m.name:<22} {m.text:<32} {m.detail}" for m in found)])


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("repo", help="owner/name")
    parser.add_argument("--days", type=int, default=30, help="the window, in days before now")
    parser.add_argument("--environment", action="append", default=[], help="count only these environments")
    parser.add_argument("--incident-label", default="incident", help="[operate] incident_label")
    parser.add_argument("--blocker-label", default="release-blocker")
    parser.add_argument("--bot", action="append", default=[], help="a machine user's login, not a person")
    form = parser.add_mutually_exclusive_group()
    form.add_argument("--json", action="store_true")
    form.add_argument("--markdown", action="store_true", help="a table to post on the roadmap issue")
    args = parser.parse_args()
    if args.days < 1:
        parser.error("--days must be at least 1")
    end = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)  # noqa: UP017 (3.10 has no dt.UTC)
    window = Window(end - dt.timedelta(days=args.days), end)
    data = fetch(args.repo, window, frozenset(args.environment), args.incident_label, args.blocker_label)
    data.bots = frozenset(args.bot)
    form_name = "json" if args.json else "markdown" if args.markdown else "table"
    print(render(args.repo, args.days, window, measures(data, window), form_name))
    return 0


if __name__ == "__main__":
    sys.exit(main())
