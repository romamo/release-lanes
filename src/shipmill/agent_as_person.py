"""Spec 012: the AGENT_AS_PERSON check. With [agents] app_id set, agents write as the App, so
an issue, pull request, or issue comment of the last 7 days that carries an agent's marker but
a person (a `User`, not a `Bot`) wrote went out under that person's login. One GraphQL search
reads them all; only URLs are shown, never titles or bodies (untrusted text)"""

import datetime as dt
import json
from dataclasses import dataclass
from typing import Any

from shipmill import cli_command
from shipmill.errors import ReleaseError
from shipmill.gate import StateRunner

NAME = "AGENT_AS_PERSON"
WINDOW = dt.timedelta(days=7)
SHOWN = 3  # URLs named; the count says how many more
ITEMS = 100  # issues and pull requests updated in the window, newest first
COMMENTS = 100  # the newest comments on each
GENERATED = "Generated with [Claude Code]"  # anywhere in the body
FIRST_LINES = ("Triage:", "<!-- shipmill:needs-decision -->")  # the body's first line starts with one

_FIELDS = "url createdAt body author { __typename }"
QUERY = f"""query($q: String!) {{
  search(query: $q, type: ISSUE, first: {ITEMS}) {{
    nodes {{
      ... on Issue {{ {_FIELDS} comments(last: {COMMENTS}) {{ nodes {{ {_FIELDS} }} }} }}
      ... on PullRequest {{ {_FIELDS} comments(last: {COMMENTS}) {{ nodes {{ {_FIELDS} }} }} }}
    }}
  }}
}}"""


@dataclass(frozen=True, slots=True)
class Flagged:
    """The flagged items' URLs, newest first"""

    urls: tuple[str, ...]

    def summary(self) -> str:
        """The count and up to SHOWN URLs"""
        shown = " ".join(self.urls[:SHOWN])
        more = f" and {len(self.urls) - SHOWN} more" if len(self.urls) > SHOWN else ""
        return f"{len(self.urls)} item(s) of the last 7 days carry an agent's marker but a person wrote them: {shown}{more}"

    @staticmethod
    def fix(command: str) -> str:
        return f"post the agents' GitHub writes through `{command} gh ...` so they run as the App"

    def detail(self) -> str:
        """doctor's one line: the summary and the fix"""
        return f"{self.summary()}; fix: {self.fix(cli_command())}"


def marked(body: str) -> bool:
    """An agent's marker: the Claude Code line anywhere, or a Triage: or needs-decision first line"""
    first = body.lstrip().partition("\n")[0]
    return GENERATED in body or first.startswith(FIRST_LINES)


def search(repo: str, since: dt.datetime) -> str:
    return f"repo:{repo} updated:>={since.astimezone(dt.UTC):%Y-%m-%dT%H:%M:%SZ}"


def command(repo: str, since: dt.datetime) -> list[str]:
    return ["gh", "api", "graphql", "-f", f"query={QUERY}", "-f", f"q={search(repo, since)}"]


def _refuse(what: object) -> ReleaseError:
    return ReleaseError(f"gh api graphql printed {str(what)[:300]} for the {NAME} check")


def _get(holder: Any, key: str) -> Any:
    return holder.get(key) if isinstance(holder, dict) else None


def _node(node: Any, since: dt.datetime) -> tuple[dt.datetime, str] | None:
    """The node's time and URL when it is flagged, else None"""
    if not isinstance(node, dict) or not {"url", "createdAt", "body", "author"} <= node.keys():
        raise _refuse(node)
    url, created, body, author = node["url"], node["createdAt"], node["body"], node["author"]
    if not isinstance(url, str) or not isinstance(created, str) or not isinstance(body, str):
        raise _refuse(node)
    if author is not None and not (isinstance(author, dict) and isinstance(author.get("__typename"), str)):
        raise _refuse(node)
    try:
        when = dt.datetime.fromisoformat(created)
    except ValueError:
        raise _refuse(node) from None
    if when.tzinfo is None:
        raise _refuse(node)
    person = author is not None and author["__typename"] == "User"  # a deleted account (None) is no one's
    return (when, url) if person and when >= since and marked(body) else None


def parse(found: Any, since: dt.datetime) -> Flagged:
    """The search's answer: the issues, pull requests, and comments created since `since`,
    with a marker and a User author"""
    if not isinstance(found, dict) or found.get("errors"):
        raise _refuse(found)
    nodes = _get(_get(_get(found, "data"), "search"), "nodes")
    if not isinstance(nodes, list):
        raise _refuse(found)
    hits: list[tuple[dt.datetime, str]] = []
    for node in nodes:
        comments = _get(_get(node, "comments"), "nodes")
        if not isinstance(comments, list):
            raise _refuse(node)
        for each in [node, *comments]:
            if (hit := _node(each, since)) is not None:
                hits.append(hit)
    return Flagged(tuple(url for _, url in sorted(hits, reverse=True)))


def read(repo: str, run: StateRunner, now: dt.datetime) -> Flagged:
    """One `gh api graphql` search for the repo's items updated in the last 7 days"""
    since = now - WINDOW
    try:
        proc = run(command(repo, since))
    except OSError as exc:  # gh missing
        raise ReleaseError(f"can't run gh: {exc}") from None
    if proc.returncode != 0:
        raise ReleaseError(f"gh api graphql failed (exit {proc.returncode}): {proc.stderr.strip()[-500:]}")
    try:
        found = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise ReleaseError(f"gh api graphql printed no JSON for the {NAME} check: {exc}") from None
    return parse(found, since)
