#!/usr/bin/env python3
"""Write the links of a chain of work across issues and repos (spec 017).

Usage:
  chains.py link <owner/repo#N> --blocked-by <owner/repo#M>
  chains.py link <owner/repo#P> --child <owner/repo#C>

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
      holds the issue by itself.

Exit 0 when the link is in place (or text only), 2 on a malformed reference, an issue
that doesn't exist or is a pull request, an issue linked to itself (each named, before
any write), or any other gh failure, including a rate limit. In a gate session gh already
writes as the App (D-14). Needs the gh CLI, authenticated. Python 3.10+, standard library
only.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, NoReturn

# What a "Depends on" line is, and the issues it names, as triage_state.py reads them
from triage_state import DEPENDENCY, DEPENDS

Variables = dict[str, str | int]
# Runs one GraphQL query with its variables and returns the parsed response, errors and
# all; a failure with no GraphQL response (auth, network) exits 2
Runner = Callable[[str, Variables], dict[str, Any]]

REFERENCE = re.compile(r"^(?P<owner>[\w.-]+)/(?P<name>[\w.-]+)#(?P<num>[1-9]\d*)$")
# GraphQL errors that are no refusal of the relation: the run fails instead
TRANSIENT = frozenset({"RATE_LIMITED"})
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


def native(run: Runner, query: str, variables: Variables) -> dict[str, Any]:
    """The node a relation query or mutation returns; Refused when the forge refuses it"""
    response = run(query, variables)
    errors = errors_of(response)
    if errors:
        raise Refused(first_line(errors))
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


def main(argv: Sequence[str] | None = None, run: Runner = gh_graphql) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    link_parser = commands.add_parser("link", help="write a link as a native relation and a Depends on line")
    link_parser.add_argument("issue", help="owner/repo#N: the issue that waits (or the parent)")
    how = link_parser.add_mutually_exclusive_group(required=True)
    how.add_argument("--blocked-by", metavar="OWNER/REPO#M", help="the issue it waits on")
    how.add_argument("--child", metavar="OWNER/REPO#C", help="the issue to put under it as a sub-issue")
    args = parser.parse_args(argv)
    target = parse_ref(args.issue)
    other = parse_ref(args.child if args.child is not None else args.blocked_by)
    return link(run, target, other, child=args.child is not None)


if __name__ == "__main__":
    sys.exit(main())
