#!/usr/bin/env python3
"""Write and show the links of a chain of work across issues and repos (spec 017).

Usage:
  chains.py link <owner/repo#N> --blocked-by <owner/repo#M>
  chains.py link <owner/repo#P> --child <owner/repo#C>
  chains.py show <owner/repo#N>... [--json] [--trusted-only] [--bot-login LOGIN]

link  writes a link in both forms triage_state.py reads:
      --blocked-by  GitHub's native relation (GraphQL addBlockedBy: N is blocked by M)
                    and a "Depends on owner/repo#M" line appended to N's body
      --child       C as a sub-issue of P (GraphQL addSubIssue) and a
                    "Depends on owner/repo#C" line appended to P's body
      Each half is written only when it is missing: the relation when the forge doesn't
      record it yet, the line when no "Depends on" line of the body names the issue
      already (as owner/repo#M, its URL, or a plain #M of the same repo). The body is kept
      byte for byte, with the line appended after it. When both are there it prints
      "already linked" and changes nothing.

      When the forge refuses the native relation (no issue dependencies or sub-issues
      there, a cross-owner link it doesn't allow, a child that has another parent), the
      line is still written and it prints "text only: <the error's first line>": the line
      holds the issue by itself. A refusal is a schema without the field or mutation, or
      an UNPROCESSABLE or FORBIDDEN error; any other error fails the run.

      Exit 0 when the link is in place (or text only), 2 on a malformed reference, an
      issue that doesn't exist or is a pull request, an issue linked to itself (each
      named, before any write), or any other gh failure, including a rate limit, a
      timeout, or an internal error. In a gate session gh already writes as the App (D-14).

show prints the chain an issue belongs to, one row per item, each once (by owner/repo#N,
      whatever case it is spelled in), so a cycle ends where it closes:
      - every issue it waits on, recursively: a "Depends on" line of its body (as
        triage_state.py reads one), an issue GitHub records it blocked by (blockedBy), and
        its sub-issues
      - every issue that waits on it, recursively: one GitHub records it blocking, and its
        parent
      Each row: the reference, the title, the executor ("agent", or "person: @login,..."
      for an issue labelled human, "person: none" with no assignee; "pull request" for a
      pull request a "Depends on" line names), and the state: CLOSED (an issue closed in
      any way, a pull request merged or closed), BLOCKED with the holds still open, READY
      (an open issue nothing open holds), or OPEN (a pull request still open). A hold that
      can't be read counts as open. A READY issue whose repo has no [agents] table in
      .github/shipmill.toml on its default branch, or no such file (404), ends in NO_GATE:
      nothing will take it up. The config is read once per repo, only for a repo with a
      READY issue. An item that can't be read (no access, no such issue) is one row,
      owner/repo#N UNREADABLE, and the chain goes on past what could be read. Several
      references print the union of their chains, each item once, the GitHub reads shared.
      With --trusted-only, an issue whose author is neither the --bot-login nor trusted by
      an origin, the repo of one of the references the walk starts from, is one row,
      owner/repo#N UNTRUSTED: its state still holds what waits on it, but nothing it says
      is read or followed (D-16). An origin trusts the author of one of its own issues who
      is an OWNER, MEMBER, or COLLABORATOR there; an issue in any other repo, where an
      outsider may own their own, is trusted only when its author is an origin's owner or
      its collaborator (one "gh api repos/<origin>/collaborators/<login>" per origin and
      login, 204 trusted, 404 not; a deleted author or another App's bot never). When
      the forge refuses the relation fields (a GitHub Enterprise Server without issue
      dependencies or sub-issues), the walk reads "Depends on" lines alone and prints one
      "note: native relations unavailable: <first error line>" line to stderr.
      --json prints the rows as one JSON object, {"rows": [...]}, each row with ref, title,
      executor, state, holds (the open ones), waits_on and waited_on_by (every item it is
      linked to, open or closed), and gate ("NO_GATE" or null).

      Exit 1 when any row is NO_GATE, else 0; 2 on a malformed reference, a starting
      reference that can't be read or is a pull request, an issue it follows with more
      than 100 labels, assignees, or relations of one kind (not paged; an UNTRUSTED one's
      are never read), a config that can't be parsed, or any gh failure other than an
      unreadable item, a missing config, or a 404 from the collaborator check.

Needs the gh CLI, authenticated. Python 3.10+, standard library only.
"""

from __future__ import annotations

import argparse
import base64
import importlib.util
import json
import re
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType
from typing import Any, NoReturn

# What a "Depends on" line is and the issues it names, and the holds GitHub records, as
# triage_state.py reads them
from triage_state import DEPENDENCY, DEPENDS, TRUSTED, dependency_refs, login, native_holds, ref_state, same_login

Variables = dict[str, str | int]
# Runs one GraphQL query with its variables and returns the parsed response, errors and
# all; a failure with no GraphQL response (auth, network) exits 2
Runner = Callable[[str, Variables], dict[str, Any]]
# Reads a repo's .github/shipmill.toml on its default branch: its text, or None when there
# is no such file; any other failure exits 2
Contents = Callable[[str], str | None]
# Whether a login is a collaborator of a repo (owner/name): True or False, exit 2 on any
# failure other than GitHub's answer
Standing = Callable[[str, str], bool]
Execute = Callable[[list[str]], subprocess.CompletedProcess[str]]

REFERENCE = re.compile(r"^(?P<owner>[\w.-]+)/(?P<name>[\w.-]+)#(?P<num>[1-9]\d*)$")
LOGIN = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}")  # a user's login, as a URL path segment
# GraphQL errors that are no refusal of the relation: the run fails instead
TRANSIENT = frozenset({"RATE_LIMITED"})
# The forge refusing the relation: a schema without the field or mutation (GitHub
# Enterprise Server without issue dependencies or sub-issues), or the forge declining
# this link (cross-owner, a child with another parent, a token not allowed to). Any other
# error (a timeout, an internal error, a node not found) fails the run instead
REFUSAL_TYPES = frozenset({"UNPROCESSABLE", "FORBIDDEN"})
UNDEFINED = re.compile(r"doesn't exist on type|isn't a defined input type")
PAGE = 100

RESOLVE = """
query($owner: String!, $name: String!, $number: Int!) {
  repository(owner: $owner, name: $name) {
    issueOrPullRequest(number: $number) { __typename ... on Issue { id body } }
  }
}
"""
BLOCKED_BY = """
query($id: ID!, $size: Int!, $cursor: String) {
  node(id: $id) {
    ... on Issue { blockedBy(first: $size, after: $cursor) { pageInfo { hasNextPage endCursor } nodes { id } } }
  }
}
"""
PARENT = """
query($id: ID!) {
  node(id: $id) { ... on Issue { parent { id } } }
}
"""
ADD_BLOCKED_BY = """
mutation($issue: ID!, $blocking: ID!) {
  addBlockedBy(input: {issueId: $issue, blockingIssueId: $blocking}) { issue { id } }
}
"""
ADD_SUB_ISSUE = """
mutation($issue: ID!, $sub: ID!) {
  addSubIssue(input: {issueId: $issue, subIssueId: $sub}) { issue { id } }
}
"""
UPDATE_BODY = """
mutation($id: ID!, $body: String!) {
  updateIssue(input: {id: $id, body: $body}) { issue { id } }
}
"""

# -- show: the items of a chain, read CHUNK at a time in one query, each under an alias
CHUNK = 20
LINKS = 100  # a connection's one page; an issue with more fails rather than read short
HUMAN_LABEL = "human"  # a person's item: its assignees do it (spec 017)
CONFIG = ".github/shipmill.toml"
UNREADABLE_TYPES = frozenset({"NOT_FOUND", "FORBIDDEN"})  # no such repo or issue, or no access
HELD = "pageInfo { hasNextPage } nodes { number state stateReason repository { nameWithOwner } }"
LINKED = "pageInfo { hasNextPage } nodes { number repository { nameWithOwner } }"
# The forge's relations, asked for until it refuses them (a GitHub Enterprise Server
# without issue dependencies or sub-issues): then the walk reads text holds alone
NATIVE_SHOW = ("blockedBy", "subIssues", "blocking", "parent")
NATIVE_ITEM = f"""
        blockedBy(first: {LINKS}) {{ {HELD} }}
        subIssues(first: {LINKS}) {{ {HELD} }}
        blocking(first: {LINKS}) {{ {LINKED} }}
        parent {{ number repository {{ nameWithOwner }} }}"""
NATIVE_NAMED = re.compile(r"\b(?:" + "|".join(NATIVE_SHOW) + r")\b")
WATCH_STATE = Path(__file__).resolve().parents[2] / "github-ship-watch" / "scripts" / "watch_state.py"


def item_fields(native: bool) -> str:
    """An item's fields; without native, a forge's that has no issue relations"""
    return f"""
      __typename
      ... on Issue {{
        number title state stateReason body
        author {{ __typename login }} authorAssociation
        repository {{ nameWithOwner }}
        labels(first: {LINKS}) {{ pageInfo {{ hasNextPage }} nodes {{ name }} }}
        assignees(first: {LINKS}) {{ pageInfo {{ hasNextPage }} nodes {{ login }} }}{NATIVE_ITEM if native else ""}
      }}
      ... on PullRequest {{ number title state repository {{ nameWithOwner }} }}
"""


def items_query(count: int, native: bool = True) -> str:
    """One query reading count items, the i-th under alias i<i> with variables $o<i>,
    $n<i>, $k<i>"""
    fields = item_fields(native)
    params = ", ".join(f"$o{i}: String!, $n{i}: String!, $k{i}: Int!" for i in range(count))
    aliases = "\n".join(
        f"  i{i}: repository(owner: $o{i}, name: $n{i}) {{ issueOrPullRequest(number: $k{i}) {{{fields}  }} }}"
        for i in range(count)
    )
    return f"query({params}) {{\n{aliases}\n}}\n"


@dataclass(frozen=True)
class Ref:
    """An issue reference, owner/repo#N, spelled as given"""

    owner: str
    name: str
    number: int

    def __str__(self) -> str:
        return f"{self.owner}/{self.name}#{self.number}"

    def same_repo(self, owner: str, name: str) -> bool:
        return self.owner.lower() == owner.lower() and self.name.lower() == name.lower()  # GitHub ignores case

    def same(self, other: Ref) -> bool:
        return self.same_repo(other.owner, other.name) and self.number == other.number

    @property
    def repo(self) -> str:
        return f"{self.owner}/{self.name}"

    @property
    def key(self) -> tuple[str, str, int]:
        """One issue however its owner and repo are cased"""
        return (self.owner.lower(), self.name.lower(), self.number)


@dataclass(frozen=True)
class Issue:
    ref: Ref
    id: str
    body: str


class Refused(Exception):
    """The forge refused to read or write the native relation; its first error line"""


def fail(message: str) -> NoReturn:
    sys.stderr.write(f"error: {message}\n")
    raise SystemExit(2)


def gh_graphql(query: str, variables: Variables) -> dict[str, Any]:
    """The default runner: one ``gh api graphql`` call. gh exits 1 on GraphQL errors with
    the response on stdout: that response comes back for the caller to classify"""
    cmd = ["gh", "api", "graphql", "-f", f"query={query}"]
    for key, value in variables.items():
        cmd += ["-F" if isinstance(value, int) else "-f", f"{key}={value}"]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    try:
        response = json.loads(proc.stdout)
    except ValueError:
        response = None
    if isinstance(response, dict) and (proc.returncode == 0 or response.get("errors")):
        return response
    sys.stderr.write(proc.stderr or f"error: gh api graphql exited {proc.returncode}\n")
    raise SystemExit(2)


def parse_ref(text: str) -> Ref:
    m = REFERENCE.match(text.strip())
    if m is None:
        fail(f"malformed reference {text!r}: want owner/repo#N")
    return Ref(m["owner"], m["name"], int(m["num"]))


def first_line(errors: list[Any]) -> str:
    error = errors[0] if errors and isinstance(errors[0], dict) else {}
    message = str(error.get("message") or json.dumps(errors))
    return (message.strip().splitlines() or [message])[0]


def errors_of(response: dict[str, Any]) -> list[Any]:
    errors = response.get("errors") or []
    if errors and any(isinstance(e, dict) and e.get("type") in TRANSIENT for e in errors):
        fail(f"gh: {first_line(errors)}")
    return list(errors)


def resolve(run: Runner, ref: Ref) -> Issue:
    """The issue ref names; exits 2 naming it when it doesn't exist or is a pull request"""
    response = run(RESOLVE, {"owner": ref.owner, "name": ref.name, "number": ref.number})
    errors = errors_of(response)
    if errors and all(isinstance(e, dict) and e.get("type") == "NOT_FOUND" for e in errors):
        fail(f"no issue {ref}: {first_line(errors)}")
    if errors:
        fail(f"reading {ref}: {first_line(errors)}")
    node = ((response.get("data") or {}).get("repository") or {}).get("issueOrPullRequest")
    if node is None:
        fail(f"no issue {ref}")
    if node.get("__typename") != "Issue":
        fail(f"{ref} is a pull request, not an issue")
    return Issue(ref, str(node["id"]), str(node["body"]))


def refusal(error: object) -> bool:
    """Whether a GraphQL error is the forge refusing the relation (see REFUSAL_TYPES)"""
    if not isinstance(error, dict):
        return False
    extensions = error.get("extensions")
    if isinstance(extensions, dict) and extensions.get("code") == "undefinedField":
        return True
    return error.get("type") in REFUSAL_TYPES or UNDEFINED.search(str(error.get("message", ""))) is not None


def native(run: Runner, query: str, variables: Variables) -> dict[str, Any]:
    """The node a relation query or mutation returns; Refused when the forge refuses it,
    exit 2 on any other error"""
    response = run(query, variables)
    errors = errors_of(response)
    if errors and all(refusal(e) for e in errors):
        raise Refused(first_line(errors))
    if errors:
        fail(f"gh: {first_line(errors)}")
    data: dict[str, Any] = response.get("data") or {}
    return data


def blocked_by(run: Runner, issue: Issue, blocking: Issue) -> bool:
    """Whether the forge records issue as blocked by blocking"""
    seen: set[str] = set()
    variables: Variables = {"id": issue.id, "size": PAGE}
    while True:
        node = native(run, BLOCKED_BY, variables).get("node") or {}
        if "blockedBy" not in node:
            raise Refused(f"{issue.ref} has no blockedBy")
        page = node["blockedBy"]
        if any(n.get("id") == blocking.id for n in page["nodes"]):
            return True
        cursor = page["pageInfo"]["endCursor"] if page["pageInfo"]["hasNextPage"] else None
        if cursor is None:
            return False
        if cursor in seen:
            fail(f"GitHub reported more blockedBy issues of {issue.ref}, but the page cursor did not advance")
        seen.add(cursor)
        variables = {**variables, "cursor": cursor}


def is_child(run: Runner, parent: Issue, child: Issue) -> bool:
    """Whether the forge records child as a sub-issue of parent"""
    node = native(run, PARENT, {"id": child.id}).get("node") or {}
    if "parent" not in node:
        raise Refused(f"{child.ref} has no parent field")
    return (node["parent"] or {}).get("id") == parent.id


def names(body: str, home: Ref, target: Ref) -> bool:
    """Whether a "Depends on" line of a body in home's repo names target: owner/repo#M, its
    URL, or a plain #M when target is in the same repo"""
    for line in DEPENDS.finditer(body):
        for m in DEPENDENCY.finditer(line["refs"]):
            owner, name = (m["owner"], m["name"]) if m["owner"] is not None else (home.owner, home.name)
            if target.same_repo(owner, name) and int(m["num"]) == target.number:
                return True
    return False


def appended(body: str, line: str) -> str:
    """body, byte for byte, with line after it: on a line of its own, after a blank line
    unless the body ends with one or with another "Depends on" line, and in the body's own
    line ending"""
    if body == "":
        return line
    newline = "\r\n" if "\r\n" in body else "\n"
    text = body if body.endswith("\n") else body + newline
    last = text.splitlines()[-1]
    if last.strip() and DEPENDS.fullmatch(last) is None:
        text += newline
    return text + line


def link(run: Runner, target: Ref, other: Ref, child: bool) -> int:
    if target.same(other):
        fail(f"{target} can't be linked to itself")
    issue, upstream = resolve(run, target), resolve(run, other)
    lines: list[str] = []
    refused: str | None = None
    try:
        if child:
            have_native = is_child(run, issue, upstream)
            relation = f"{other} is a sub-issue of {target}"
            mutation, variables = ADD_SUB_ISSUE, {"issue": issue.id, "sub": upstream.id}
        else:
            have_native = blocked_by(run, issue, upstream)
            relation = f"{target} is blocked by {other}"
            mutation, variables = ADD_BLOCKED_BY, {"issue": issue.id, "blocking": upstream.id}
        if not have_native:
            native(run, mutation, dict(variables))
            lines.append(f"linked: {relation}")
    except Refused as exc:
        have_native, refused = False, str(exc)
    have_text = names(issue.body, target, other)
    if not have_text:
        line = f"Depends on {other}"
        response = run(UPDATE_BODY, {"id": issue.id, "body": appended(issue.body, line)})
        errors = errors_of(response)
        if errors:
            fail(f"editing the body of {target}: {first_line(errors)}")
        lines.append(f"added to {target}: {line}")
    if refused is not None:
        lines.append(f"text only: {refused}")
    if have_native and have_text:
        lines.append("already linked")
    print("\n".join(lines))
    return 0


# -- show ----------------------------------------------------------------------------------

Key = tuple[str, str, int]
UNREADABLE = "UNREADABLE"
UNTRUSTED = "UNTRUSTED"
OPEN_HOLDS = frozenset({"OPEN", UNREADABLE})  # a hold that can't be read may still be open


@dataclass
class Reader:
    """How the walk reads items: the runner; with trusted_only, an issue whose author the
    origins (the repos of the references the walk starts from, owner/name) don't trust is
    read for its state alone and never followed (D-16, see trusts); standing looks up an
    author's standing in an origin, each (origin, login) once; native turns False, for the
    rest of the run, once the forge refuses the relation fields"""

    run: Runner
    standing: Standing
    trusted_only: bool = False
    bot_login: str | None = None
    origins: tuple[str, ...] = ()
    native: bool = True
    standings: dict[tuple[str, str], bool] = field(default_factory=dict)

    def trusts(self, ref: Ref, author: str, association: str) -> bool:
        """D-16 against the origins, not the item's own repo, where an outsider owns their
        own: the bot_login, or an author trusted by any origin. In an origin, the item's
        authorAssociation answers; elsewhere the author's login is the origin's owner or
        the origin's collaborator (one standing lookup, cached). A deleted author, or an
        App's bot other than bot_login, is trusted by no other repo"""
        if self.bot_login is not None and same_login(author, self.bot_login):
            return True
        home = [o for o in self.origins if ref.same_repo(*o.split("/"))]
        if home and association in TRUSTED:
            return True
        if author == "ghost" or author.endswith("[bot]"):
            return False
        for origin in self.origins:
            if origin in home:
                continue
            if same_login(author, origin.split("/")[0]):
                return True
            if LOGIN.fullmatch(author) is None:
                fail(f"{ref}: GitHub gave its author as no login it can check")  # the text not echoed
            known = (origin.lower(), author.lower())
            if known not in self.standings:
                self.standings[known] = self.standing(origin, author)
            if self.standings[known]:
                return True
        return False


@dataclass(frozen=True)
class Item:
    """An item of a chain as GitHub reads it. kind is Issue, PullRequest, UNTRUSTED (an issue
    by an untrusted author: its state alone), or UNREADABLE (ref then as asked, the rest
    empty); state is ref_state's; holds are the items it waits on, each with the state GitHub
    gave along (a native relation) or None (a "Depends on" line); waited_on_by, the issues it
    blocks and its parent"""

    ref: Ref
    kind: str
    title: str = ""
    state: str = ""
    executor: str = ""
    holds: tuple[tuple[Ref, str | None], ...] = ()
    waited_on_by: tuple[Ref, ...] = ()


def ref_of(node: dict[str, Any]) -> Ref:
    owner, _, name = str(node["repository"]["nameWithOwner"]).partition("/")
    return Ref(owner, name, int(node["number"]))


def connection(node: dict[str, Any], field: str, ref: Ref) -> list[dict[str, Any]]:
    """A connection's nodes; one with more than a page fails rather than read short"""
    found = node[field]
    if found["pageInfo"]["hasNextPage"]:
        fail(f"{ref} has more than {LINKS} {field}, which chains.py does not page")
    nodes: list[dict[str, Any]] = found["nodes"]
    return nodes


def parse_item(node: dict[str, Any], reader: Reader) -> Item:
    ref = ref_of(node)
    title = str(node["title"])
    if node["__typename"] == "PullRequest":
        return Item(ref, "PullRequest", title, ref_state(node), "pull request")
    association = str(node.get("authorAssociation") or "NONE")
    if reader.trusted_only and not reader.trusts(ref, login(node.get("author")), association):
        return Item(ref, UNTRUSTED, state=ref_state(node))  # nothing it says is read or followed
    labels = {n["name"] for n in connection(node, "labels", ref)}
    logins = [str(n["login"]) for n in connection(node, "assignees", ref)]
    executor = "agent"
    if HUMAN_LABEL in labels:
        executor = "person: " + (",".join(f"@{login}" for login in logins) or "none")
    holds: dict[Key, tuple[Ref, str | None]] = {}
    for owner, name, number in dependency_refs(node, (ref.owner, ref.name)):
        hold = Ref(owner, name, number)
        holds.setdefault(hold.key, (hold, None))
    for relation in ("blockedBy", "subIssues"):
        if relation in node:  # absent when the forge refused the relations
            connection(node, relation, ref)  # fails past a page; native_holds reads them
    for (owner, name, number), (_, state, _) in native_holds(node).items():
        hold = Ref(owner, name, number)
        holds[hold.key] = (holds.get(hold.key, (hold, None))[0], state)  # the text's spelling, GitHub's state
    holds.pop(ref.key, None)
    waiting = [ref_of(n) for n in connection(node, "blocking", ref)] if "blocking" in node else []
    if node.get("parent"):
        waiting.append(ref_of(node["parent"]))
    downstream = {r.key: r for r in waiting if r.key != ref.key}
    return Item(ref, "Issue", title, ref_state(node), executor, tuple(holds.values()), tuple(downstream.values()))


def unreadable_alias(error: object, count: int) -> int | None:
    """The alias index an error says can't be read (no such repo or issue, or no access to
    it); None for any other error, which fails the run. An error deeper in an item (one of
    its relations) is no unreadable item"""
    if not isinstance(error, dict) or error.get("type") not in UNREADABLE_TYPES:
        return None
    path = error.get("path")
    if not isinstance(path, list) or not 1 <= len(path) <= 2 or not isinstance(path[0], str):
        return None
    alias = re.fullmatch(r"i(\d+)", path[0])
    index = int(alias.group(1)) if alias else count
    return index if index < count else None


def native_refusal(error: object) -> bool:
    """Whether a GraphQL error is the schema refusing one of the relation fields: an
    undefinedField naming one, or a message saying one "doesn't exist on type", as
    triage_state.py's names_native reads it for blockedBy and subIssues"""
    if not isinstance(error, dict):
        return False
    extensions = error.get("extensions")
    message = str(error.get("message", ""))
    if isinstance(extensions, dict) and extensions.get("code") == "undefinedField":
        return extensions.get("fieldName") in NATIVE_SHOW or NATIVE_NAMED.search(message) is not None
    return UNDEFINED.search(message) is not None and NATIVE_NAMED.search(message) is not None


def answer(reader: Reader, count: int, variables: Variables) -> dict[str, Any]:
    """One query's response; when the forge refuses the relation fields, one stderr note,
    then this query and every later one without them (spec 017's text holds alone)"""
    response = reader.run(items_query(count, reader.native), variables)
    errors = response.get("errors") or []
    if reader.native and errors and all(native_refusal(e) for e in errors):
        sys.stderr.write(f"note: native relations unavailable: {first_line(errors)}\n")
        reader.native = False
        response = reader.run(items_query(count, reader.native), variables)
    return response


def read_items(reader: Reader, refs: list[Ref]) -> dict[Key, Item]:
    """The items refs name, CHUNK per query, each under the key it was asked by"""
    found: dict[Key, Item] = {}
    for start in range(0, len(refs), CHUNK):
        chunk = refs[start : start + CHUNK]
        variables: Variables = {}
        for i, ref in enumerate(chunk):
            variables.update({f"o{i}": ref.owner, f"n{i}": ref.name, f"k{i}": ref.number})
        response = answer(reader, len(chunk), variables)
        unreadable: set[int] = set()
        for error in errors_of(response):
            index = unreadable_alias(error, len(chunk))
            if index is None:
                fail(f"gh: {first_line([error])}")
            unreadable.add(index)
        data = response.get("data") or {}
        for i, ref in enumerate(chunk):
            node = (data.get(f"i{i}") or {}).get("issueOrPullRequest")
            if i in unreadable:
                found[ref.key] = Item(ref, UNREADABLE)
            elif node is None:
                fail(f"gh: no item {ref} in the response and no error naming it")
            else:
                found[ref.key] = parse_item(node, reader)
    return found


def walk(reader: Reader, starts: list[Ref]) -> tuple[list[Item], dict[Key, Item]]:
    """The chain of starts, each item once in the order found, and every item read (by the
    key it was asked by and by its own): the chain's, and the holds of the issues that wait
    on a start, read for their states only"""
    items: dict[Key, Item] = {}

    def read(refs: list[Ref]) -> None:
        missing = {r.key: r for r in refs if r.key not in items}
        for key, item in read_items(reader, list(missing.values())).items():
            items[key] = item
            items.setdefault(item.ref.key, item)  # a renamed repo answers under its new name

    read(starts)
    for ref in starts:
        if items[ref.key].kind == UNREADABLE:
            fail(f"can't read {ref}: no such issue, or no access to it")
        if items[ref.key].kind == "PullRequest":
            fail(f"{ref} is a pull request, not an issue")
    chain: dict[Key, Item] = {}
    up, down = list(starts), list(starts)
    seen_up: set[Key] = set()
    seen_down: set[Key] = set()
    while up or down:
        read(up + down)
        later_up: list[Ref] = []
        later_down: list[Ref] = []
        for refs, seen, later, upstream in ((up, seen_up, later_up, True), (down, seen_down, later_down, False)):
            for ref in refs:
                item = items[ref.key]
                if item.ref.key in seen:
                    continue
                seen.add(item.ref.key)
                chain.setdefault(item.ref.key, item)
                later += [hold for hold, _ in item.holds] if upstream else list(item.waited_on_by)
        up, down = later_up, later_down
    read([hold for item in chain.values() for hold, state in item.holds if state is None])
    return list(chain.values()), items


def capture(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, capture_output=True, text=True, check=False)


def gh_contents(repo: str, execute: Execute = capture) -> str | None:
    """The repo's shipmill config on its default branch, through the contents API; None when
    GitHub answers 404 (no such file), exit 2 on any other failure"""
    proc = execute(["gh", "api", f"repos/{repo}/contents/{CONFIG}"])
    if proc.returncode != 0:
        if re.search(r"\bHTTP 404\b", proc.stderr):
            return None
        fail(f"reading {CONFIG} of {repo}: {proc.stderr.strip() or f'gh api exited {proc.returncode}'}")
    try:
        data = json.loads(proc.stdout)
    except ValueError:
        fail(f"reading {CONFIG} of {repo}: gh api printed no JSON")
    if not isinstance(data, dict) or data.get("encoding") != "base64" or not isinstance(data.get("content"), str):
        fail(f"reading {CONFIG} of {repo}: not a base64 file in the contents API's answer")
    try:
        return base64.b64decode(data["content"]).decode("utf-8")
    except ValueError as exc:
        fail(f"reading {CONFIG} of {repo}: {exc}")


def gh_collaborator(repo: str, user: str, execute: Execute = capture) -> bool:
    """Whether user is a collaborator of repo, through the REST check (an owner, an outside
    collaborator, or an org member with access to it): True on GitHub's 204, False on its
    404, exit 2 on any other failure (no access to the check, a rate limit, the network)"""
    proc = execute(["gh", "api", f"repos/{repo}/collaborators/{user}"])
    if proc.returncode == 0:
        return True
    if re.search(r"\bHTTP 404\b", proc.stderr):
        return False
    why = proc.stderr.strip() or f"gh api exited {proc.returncode}"
    fail(f"reading whether {user} is a collaborator of {repo}: {why}")


_watch: list[ModuleType] = []


def watch_module() -> ModuleType:
    """github-ship-watch's watch_state.py, for its reading of [agents]: tomllib on 3.11+,
    its regex fallback on 3.10, one reading for the watch and the chain"""
    if not _watch:
        spec = importlib.util.spec_from_file_location("shipmill_watch_state", WATCH_STATE)
        if spec is None or spec.loader is None:
            fail(f"can't load {WATCH_STATE}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module  # its dataclasses look their module up by name
        spec.loader.exec_module(module)
        _watch.append(module)
    return _watch[0]


def has_gate(text: str, repo: str) -> bool:
    """Whether a config has an [agents] table: a gate takes up the repo's ready items. A
    config the reader refuses exits 2 rather than reading as no gate"""
    ws = watch_module()
    try:
        return bool(ws.agents_table(text, Path(f"{repo}:{CONFIG}")) is not None)
    except ws.Refused as refused:
        sys.stderr.write(f"{refused}\n")
        raise SystemExit(2) from None


def show_rows(chain: list[Item], items: dict[Key, Item], contents: Contents) -> list[dict[str, Any]]:
    """One row per item of the chain; a READY issue's repo config is read once per repo"""

    def named(ref: Ref) -> Ref:
        item = items.get(ref.key)
        return item.ref if item is not None and item.kind != UNREADABLE else ref

    def state_of(ref: Ref, given: str | None) -> str:
        if given is not None:
            return given
        item = items[ref.key]
        return UNREADABLE if item.kind == UNREADABLE else item.state

    # what waits on an item: what GitHub records it blocking or as its parent, and every
    # item of the chain that names it as a hold (a "Depends on" line has no reverse)
    waiting: dict[Key, dict[Key, Ref]] = {}
    for item in chain:
        waiting.setdefault(item.ref.key, {}).update((named(r).key, named(r)) for r in item.waited_on_by)
        for hold, _ in item.holds:
            waiting.setdefault(named(hold).key, {})[item.ref.key] = item.ref
    gates: dict[str, bool] = {}
    rows: list[dict[str, Any]] = []
    for item in chain:
        shown = item.kind not in (UNREADABLE, UNTRUSTED)  # neither's text is shown
        row: dict[str, Any] = {
            "ref": str(item.ref),
            "title": item.title if shown else None,
            "executor": item.executor if shown else None,
            "holds": [],
            "waits_on": [str(named(hold)) for hold, _ in item.holds],
            "waited_on_by": [str(ref) for ref in waiting.get(item.ref.key, {}).values()],
            "gate": None,
        }
        if not shown:
            row["state"] = item.kind
        elif item.kind == "PullRequest":
            row["state"] = "OPEN" if item.state == "OPEN" else "CLOSED"
        elif item.state != "OPEN":
            row["state"] = "CLOSED"
        else:
            row["holds"] = [str(named(hold)) for hold, state in item.holds if state_of(hold, state) in OPEN_HOLDS]
            row["state"] = "BLOCKED" if row["holds"] else "READY"
        if row["state"] == "READY":
            repo = item.ref.repo.lower()
            if repo not in gates:
                text = contents(item.ref.repo)
                gates[repo] = text is not None and has_gate(text, item.ref.repo)
            row["gate"] = None if gates[repo] else "NO_GATE"
        rows.append(row)
    return rows


def show_line(row: dict[str, Any]) -> str:
    if row["state"] in (UNREADABLE, UNTRUSTED):
        return f"{row['ref']} {row['state']}"
    state = row["state"] + (f" ({' '.join(row['holds'])})" if row["holds"] else "")
    return "  ".join([row["ref"], row["title"], row["executor"], state, *([row["gate"]] if row["gate"] else [])])


def show(reader: Reader, contents: Contents, starts: list[Ref], as_json: bool) -> int:
    chain, items = walk(reader, starts)
    rows = show_rows(chain, items, contents)
    if as_json:
        print(json.dumps({"rows": rows}, sort_keys=True))
    else:
        print("\n".join(show_line(row) for row in rows))
    return 1 if any(row["gate"] == "NO_GATE" for row in rows) else 0


def main(
    argv: Sequence[str] | None = None,
    run: Runner = gh_graphql,
    contents: Contents = gh_contents,
    standing: Standing = gh_collaborator,
) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    link_parser = commands.add_parser("link", help="write a link as a native relation and a Depends on line")
    link_parser.add_argument("issue", help="owner/repo#N: the issue that waits (or the parent)")
    how = link_parser.add_mutually_exclusive_group(required=True)
    how.add_argument("--blocked-by", metavar="OWNER/REPO#M", help="the issue it waits on")
    how.add_argument("--child", metavar="OWNER/REPO#C", help="the issue to put under it as a sub-issue")
    show_parser = commands.add_parser("show", help="print the chain an issue belongs to")
    show_parser.add_argument("issues", nargs="+", metavar="OWNER/REPO#N", help="the issue (or issues) to start from")
    show_parser.add_argument("--json", action="store_true", help="the rows as one JSON object")
    show_parser.add_argument(
        "--trusted-only",
        action="store_true",
        help="read an issue the starting repos don't trust the author of for its state alone (D-16)",
    )
    show_parser.add_argument("--bot-login", help="the login shipmill's sessions write as, such as <slug>[bot]")
    args = parser.parse_args(argv)
    if args.command == "show":
        if args.bot_login is not None and not args.bot_login.strip():
            parser.error("--bot-login must not be empty")
        starts = [parse_ref(issue) for issue in args.issues]
        origins: dict[str, str] = {}
        for ref in starts:
            origins.setdefault(ref.repo.lower(), ref.repo)  # each repo once, as first spelled
        reader = Reader(run, standing, args.trusted_only, args.bot_login, tuple(origins.values()))
        return show(reader, contents, starts, args.json)
    target = parse_ref(args.issue)
    other = parse_ref(args.child if args.child is not None else args.blocked_by)
    return link(run, target, other, child=args.child is not None)


if __name__ == "__main__":
    sys.exit(main())
