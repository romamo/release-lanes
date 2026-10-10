"""Spec 017, #345: the docs a session follows for chains, and a chain of four items across
three repos, the third a person's, driven to its end through the real triage_state.py and
chains.py over a scripted forge, with no network"""

import contextlib
import importlib.util
import io
import json
import os
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / "skills" / "github-issue-triage"
SCRIPTS = SKILL / "scripts"
TRIAGE = SCRIPTS / "triage_state.py"
BOT = "shipmill-gate"  # as GraphQL spells the App's bot; --bot-login adds "[bot]"
GATED = '[agents]\nprompt = "/github-issue-triage {repo}"\napp_id = 1\n'
HANDOFF = "<!-- shipmill:handoff -->"
PERSON_STATES = ("NO_ASSIGNEE", "HANDOFF_DUE", "WITH_PERSON", "VERIFY_DUE", "VERIFY_CLOSED")


def doc(*parts: str) -> str:
    return ROOT.joinpath(*parts).read_text(encoding="utf-8")


def test_s017_22_the_triage_docs_document_links_a_person_s_item_and_parents() -> None:
    skill = doc("skills", "github-issue-triage", "SKILL.md")
    rubric = doc("skills", "github-issue-triage", "references", "triage-rubric.md")
    comments = doc("skills", "github-issue-triage", "references", "comments.md")
    gate = doc("skills", "github-issue-triage", "references", "spec-gate.md")
    # the link forms and chains.py link
    for text in (skill, rubric):
        for form in ("Depends on owner/repo#N", "blockedBy", "sub-issue", "chains.py link", "--blocked-by", "--child"):
            assert form in text, form
    assert "already linked" in rubric and "text only:" in rubric
    assert "chains.py link" in gate and "--child" in gate and "--blocked-by" in gate
    assert "addSubIssue(input:{issueId:$p" not in gate  # the GraphQL snippet chains.py link replaced
    # the human label, each state, and the session's action for each
    assert "`human`" in skill and "`human`" in rubric and "`human`" in gate
    for state in PERSON_STATES:
        assert state in skill and state in rubric, state
    for state in ("NO_ASSIGNEE", "HANDOFF_DUE", "VERIFY_DUE", "VERIFY_CLOSED"):
        assert f"**{state}**" in comments, state
    # the hand-off's contents: the marker, the mentions, the closed holds, the quoted steps,
    # the check, and how to report
    handoff = comments.split("**HANDOFF_DUE**", 1)[1].split("**VERIFY_DUE**", 1)[0]
    for part in (HANDOFF, "@{assignee}", "closed", "> {the issue's steps", "## Check", "Reply `done` here"):
        assert part in handoff, part
    # verification against the ## Check, and the needs-decision fallback
    assert "<!-- shipmill:verified -->" in comments and "<!-- shipmill:verified -->" in rubric
    person = rubric.split("## A person's item", 1)[1].split("\n## ", 1)[0]
    assert person.count("Runs the `## Check`") == 2  # VERIFY_DUE and VERIFY_CLOSED
    for text in (rubric, comments):
        assert "needs-decision" in text and "close it on the report" in text.lower()
        assert "it isn't done" in text.lower()
    # the question follows needs-decision.md: its marker, and the recommended option first
    question = comments.split("<!-- shipmill:needs-decision -->", 1)[1].split("```", 1)[0]
    assert "\n1. " in question and "(recommended)" in question.split("\n2. ", 1)[0]
    # parents
    assert "children K/N closed" in skill and "children K/N closed" in rubric and "children K/N closed" in gate
    assert "## A parent's close" in comments
    # docs/flow.md shows a person's item in a chain
    flow = doc("docs", "flow.md")
    chain = flow.split("## A chain across repos and people", 1)[1].split("\n## ", 1)[0]
    assert "a person (`human`" in chain and "HANDOFF_DUE" in chain and "VERIFY_DUE" in chain
    rows = [line for line in chain.splitlines() if line.startswith("| ") and line[2].isdigit()]
    executors = ["a person" if "a person" in row else "an agent" if "an agent" in row else "?" for row in rows]
    assert executors == ["an agent", "an agent", "a person", "an agent"]  # between two agents' items
    for state in PERSON_STATES:
        assert f"| {state} |" in flow, state


# -- the forge --------------------------------------------------------------------------


@dataclass
class Comment:
    body: str
    at: str
    login: str
    bot: bool = False
    association: str = "NONE"

    def node(self) -> dict[str, Any]:
        author = {"__typename": "Bot" if self.bot else "User", "login": self.login}
        return {"body": self.body, "createdAt": self.at, "author": author, "authorAssociation": self.association}


@dataclass
class Ticket:
    repo: str
    number: int
    title: str
    body: str = ""
    labels: tuple[str, ...] = ()
    assignees: tuple[str, ...] = ()
    comments: list[Comment] = field(default_factory=list)
    blocked_by: list[str] = field(default_factory=list)  # refs, as GitHub records them
    state: str = "OPEN"
    closed_at: str | None = None
    closed_by: tuple[str, bool] | None = None  # (login, bot)
    closer_pr: int | None = None

    @property
    def ref(self) -> str:
        return f"{self.repo}#{self.number}"

    @property
    def id(self) -> str:
        return f"I_{self.ref}"


class World:
    """GitHub across several repos: issues, their comments, native blockedBy relations, and
    closes, each event a minute after the last. It answers triage_state.py through a stand-in
    gh on PATH (the answers written to a file before each run) and chains.py through its
    injected runner, from the same state"""

    def __init__(self, tmp: Path, ch: ModuleType) -> None:
        self.ch = ch
        self.tickets: dict[str, Ticket] = {}
        self.minute = 0
        self.tmp = tmp
        self.ids: dict[str, str] = {}
        self.log: list[tuple[str, str, str]] = []  # (login, what, ref): everything anyone did

    def now(self) -> str:
        self.minute += 1
        return f"2026-10-10T{10 + self.minute // 60:02d}:{self.minute % 60:02d}:00Z"

    def open(self, ref: str, title: str, **fields: Any) -> Ticket:
        repo, _, number = ref.partition("#")
        ticket = Ticket(repo, int(number), title, **fields)
        self.tickets[ref] = ticket
        self.ids[ticket.id] = ref
        self.log.append(("maria", "open", ref))
        return ticket

    def comment(self, ref: str, body: str, login: str, bot: bool = False, association: str = "NONE") -> None:
        self.tickets[ref].comments.append(Comment(body, self.now(), login, bot, association))
        self.log.append((login + ("[bot]" if bot else ""), "comment", ref))

    def close(self, ref: str, login: str, bot: bool = False, pr: int | None = None) -> None:
        ticket = self.tickets[ref]
        ticket.state, ticket.closed_at, ticket.closed_by, ticket.closer_pr = "CLOSED", self.now(), (login, bot), pr
        self.log.append((login + ("[bot]" if bot else ""), "close", ref))

    # -- triage_state.py's queries, answered by the stand-in gh

    def native(self, ref: str) -> dict[str, Any]:
        ticket = self.tickets[ref]
        return {
            "number": ticket.number,
            "state": ticket.state,
            "stateReason": "COMPLETED" if ticket.state == "CLOSED" else None,
            "closedAt": ticket.closed_at,
            "repository": {"nameWithOwner": ticket.repo},
        }

    @staticmethod
    def labels(ticket: Ticket) -> dict[str, Any]:
        return {"pageInfo": {"hasNextPage": False}, "nodes": [{"name": n} for n in ticket.labels]}

    def open_node(self, ticket: Ticket) -> dict[str, Any]:
        page = {"pageInfo": {"hasNextPage": False, "endCursor": None}}
        return {
            "number": ticket.number,
            "title": ticket.title,
            "body": ticket.body,
            "author": {"__typename": "User", "login": "maria"},
            "authorAssociation": "OWNER",
            "labels": self.labels(ticket),
            "assignees": {"nodes": [{"login": a} for a in ticket.assignees]},
            "comments": {"nodes": [c.node() for c in ticket.comments]},
            "timelineItems": {**page, "nodes": []},
            "blockedBy": {**page, "nodes": [self.native(r) for r in ticket.blocked_by]},
            "subIssues": {**page, "nodes": []},
        }

    def closed_node(self, ticket: Ticket) -> dict[str, Any]:
        assert ticket.closed_by is not None
        who, bot = ticket.closed_by
        closer = (
            None
            if ticket.closer_pr is None
            else {"__typename": "PullRequest", "number": ticket.closer_pr, "merged": True}
        )
        return {
            "number": ticket.number,
            "title": ticket.title,
            "stateReason": "COMPLETED",
            "body": ticket.body,
            "labels": self.labels(ticket),
            "comments": {"nodes": [c.node() for c in ticket.comments[-5:]]},  # comments(last: 5)
            "refs": {"pageInfo": {"hasNextPage": False, "endCursor": None}, "nodes": []},
            "timelineItems": {
                "nodes": [
                    {
                        "createdAt": ticket.closed_at,
                        "actor": {"__typename": "Bot" if bot else "User", "login": who},
                        "closer": closer,
                    }
                ]
            },
        }

    def answers(self) -> dict[str, Any]:
        repos = sorted({t.repo for t in self.tickets.values()})
        first = {}
        for repo in repos:
            mine = sorted((t for t in self.tickets.values() if t.repo == repo), key=lambda t: -t.number)
            first[repo] = {
                "open": {
                    "pageInfo": {"hasNextPage": False, "endCursor": None},
                    "nodes": [self.open_node(t) for t in mine if t.state == "OPEN"],
                },
                "tags": {"pageInfo": {"hasPreviousPage": False, "startCursor": None}, "nodes": []},
                "closed": {"nodes": [self.closed_node(t) for t in mine if t.state == "CLOSED"]},
            }
        items = {ref: {"__typename": "Issue", **self.native(ref)} for ref in self.tickets}
        comments = {
            ref: {"pageInfo": {"hasPreviousPage": False, "startCursor": None}, "nodes": [c.node() for c in t.comments]}
            for ref, t in self.tickets.items()
        }
        return {"first": first, "items": items, "comments": comments}

    def triage(self, repo: str) -> tuple[int, dict[str, tuple[str, str]]]:
        """triage_state.py on repo, as a gate session runs it: its exit and each row's state
        and note by reference"""
        data = self.tmp / "gh.json"
        data.write_text(json.dumps(self.answers()), encoding="utf-8")
        fakes = self.tmp / "fakes"
        if not fakes.exists():
            fakes.mkdir()
            gh = fakes / "gh"
            gh.write_text(FAKE_GH.format(python=sys.executable), encoding="utf-8")
            gh.chmod(0o700)
        env = {"PATH": os.pathsep.join([str(fakes), "/usr/bin", "/bin"]), "FAKE_GH_DATA": str(data)}
        cmd = [sys.executable, str(TRIAGE), repo, "--json", "--bot-login", f"{BOT}[bot]", "--trusted-only"]
        proc = subprocess.run(cmd, capture_output=True, text=True, check=False, env=env)
        assert proc.returncode in (0, 1), proc.stderr
        rows = [json.loads(line) for line in proc.stdout.splitlines()]
        return proc.returncode, {f"{repo}#{r['number']}": (r["state"], r["note"]) for r in rows}

    # -- chains.py's runner

    def __call__(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        ch = self.ch
        if query == ch.RESOLVE:
            ticket = self.tickets[f"{variables['owner']}/{variables['name']}#{variables['number']}"]
            node = {"__typename": "Issue", "id": ticket.id, "body": ticket.body}
            return {"data": {"repository": {"issueOrPullRequest": node}}}
        if query == ch.BLOCKED_BY:
            ticket = self.tickets[self.ids[variables["id"]]]
            page = {"pageInfo": {"hasNextPage": False, "endCursor": None}}
            nodes = [{"id": self.tickets[r].id} for r in ticket.blocked_by]
            return {"data": {"node": {"blockedBy": {**page, "nodes": nodes}}}}
        if query == ch.ADD_BLOCKED_BY:
            self.tickets[self.ids[variables["issue"]]].blocked_by.append(self.ids[variables["blocking"]])
            return {"data": {"addBlockedBy": {"issue": {"id": variables["issue"]}}}}
        if query == ch.UPDATE_BODY:
            self.tickets[self.ids[variables["id"]]].body = variables["body"]
            return {"data": {"updateIssue": {"issue": {"id": variables["id"]}}}}
        count = 0
        while f"o{count}" in variables:
            count += 1
        assert query == ch.items_query(count), "an unexpected query"
        data = {}
        for i in range(count):
            ticket = self.tickets[f"{variables[f'o{i}']}/{variables[f'n{i}']}#{variables[f'k{i}']}"]
            data[f"i{i}"] = {"issueOrPullRequest": self.item(ticket)}
        return {"data": data}

    def item(self, ticket: Ticket) -> dict[str, Any]:
        def linked(refs: list[str]) -> dict[str, Any]:
            return {"pageInfo": {"hasNextPage": False}, "nodes": [self.native(r) for r in refs]}

        blocking = [t.ref for t in self.tickets.values() if ticket.ref in t.blocked_by]
        return {
            "__typename": "Issue",
            "number": ticket.number,
            "title": ticket.title,
            "state": ticket.state,
            "stateReason": "COMPLETED" if ticket.state == "CLOSED" else None,
            "body": ticket.body,
            "author": {"__typename": "User", "login": "maria"},
            "authorAssociation": "OWNER",
            "repository": {"nameWithOwner": ticket.repo},
            "labels": self.labels(ticket),
            "assignees": {"pageInfo": {"hasNextPage": False}, "nodes": [{"login": a} for a in ticket.assignees]},
            "blockedBy": linked(ticket.blocked_by),
            "subIssues": linked([]),
            "blocking": linked(blocking),
            "parent": None,
        }

    def chains(self, *argv: str) -> tuple[int, str]:
        """chains.py on this forge, every repo gated: its exit and what it printed"""
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code: int = self.ch.main(list(argv), run=self, contents=lambda repo: GATED, standing=self.standing)
        return code, out.getvalue()

    @staticmethod
    def standing(repo: str, login: str) -> bool:
        return login == "maria"  # the maintainer collaborates on each repo

    def show(self, ref: str) -> list[tuple[str, str, str, str | None]]:
        code, out = self.chains("show", ref, "--json", "--trusted-only", "--bot-login", f"{BOT}[bot]")
        assert code == 0
        return [(r["ref"], r["executor"], r["state"], r["gate"]) for r in json.loads(out)["rows"]]


# The stand-in gh: triage_state.py's first query per repo, its query for the states of the
# issues named on Depends on lines, and a closed issue's comments
FAKE_GH = """#!{python}
import json, os, re, sys
args = dict(a.split("=", 1) for a in sys.argv[1:] if "=" in a)
query = args["query"]
fake = json.loads(open(os.environ["FAKE_GH_DATA"]).read())
if "issueOrPullRequest" in query:
    data = {{}}
    for alias, owner, name, number in re.findall(
        r'(r\\d+): repository\\(owner: "([^"]+)", name: "([^"]+)"\\) {{ issueOrPullRequest\\(number: (\\d+)\\)', query
    ):
        data[alias] = {{"issueOrPullRequest": fake["items"][owner + "/" + name + "#" + number]}}
    print(json.dumps({{"data": data}}))
elif "issue(number" in query:
    ref = args["owner"] + "/" + args["name"] + "#" + args["number"]
    print(json.dumps({{"data": {{"repository": {{"issue": {{"comments": fake["comments"][ref]}}}}}}}}))
else:
    print(json.dumps({{"data": {{"repository": fake["first"][args["owner"] + "/" + args["name"]]}}}}))
"""


@pytest.fixture(scope="module")
def ch() -> ModuleType:
    sys.path.insert(0, str(SCRIPTS))  # chains imports triage_state, as when run from its folder
    try:
        spec = importlib.util.spec_from_file_location("chains_e2e", SCRIPTS / "chains.py")
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
    finally:
        sys.path.remove(str(SCRIPTS))
    return module


# -- the sessions, as the docs tell them to act ------------------------------------------

STEPS = "Reload the web server so the new rule applies:\n\n    sudo systemctl reload nginx\n"
CHECK = "`curl -sI https://acme.example/new` returns 200"
REPOS = ("acme/app", "acme/srv", "acme/site")


def handoff(world: World, ref: str, holds: list[str]) -> str:
    """comments.md, A person's item, HANDOFF_DUE: the marker, every assignee, the holds that
    closed, the steps quoted, the ## Check quoted, and how to report"""
    ticket = world.tickets[ref]
    steps, _, check = ticket.body.partition("## Check")
    quote = "\n".join(f"> {line}" if line else ">" for line in steps.strip().splitlines())
    check_quote = "\n".join(f"> {line}" for line in check.strip().splitlines() if not line.startswith("Depends on"))
    who = " ".join(f"@{a}" for a in ticket.assignees)
    return (
        f"{HANDOFF}\n{who}, it's your turn: {', and '.join(holds)} closed.\n\n"
        f"What to do, from this issue:\n\n{quote}\n\nWhen it's done, this must hold:\n\n{check_quote}\n\n"
        "Reply `done` here when it's finished, or close the issue.\n"
    )


def gate_tick(world: World) -> dict[str, tuple[str, str]]:
    """Each repo's triage, as its gate session runs it; every row by reference"""
    rows: dict[str, tuple[str, str]] = {}
    for repo in REPOS:
        _, found = world.triage(repo)
        rows |= found
    return rows


def test_s017_23_a_chain_across_three_repos_advances_with_the_person_acting_only_on_their_item(
    tmp_path: Path, ch: ModuleType
) -> None:
    world = World(tmp_path, ch)
    server: dict[str, bool] = {"reloaded": False}  # what the ## Check reads, outside GitHub
    page, rule, reload, link = "acme/app#1", "acme/srv#2", "acme/app#3", "acme/site#4"
    world.open(page, "Build the page")
    world.open(rule, "Remove the rule that hides the page", body=f"Depends on {page}\n")
    world.open(
        reload, "Reload the web server", body=f"{STEPS}\n## Check\n\n{CHECK}\n", labels=("human",), assignees=("ana",)
    )
    world.open(link, "Link to the new page")
    # The triage session's verdicts on the agents' items, then the chain's links in both forms
    for ref in (page, rule, link):
        world.comment(ref, "Triage: **implement**. I'll link the PR here.", BOT, bot=True)
    # the second's body names the first already: only the relation is added
    assert world.chains("link", rule, "--blocked-by", page) == (0, f"linked: {rule} is blocked by {page}\n")
    assert world.chains("link", reload, "--blocked-by", rule) == (
        0,
        f"linked: {reload} is blocked by {rule}\nadded to {reload}: Depends on {rule}\n",
    )
    assert world.chains("link", link, "--blocked-by", reload)[0] == 0
    assert world.chains("link", link, "--blocked-by", reload) == (0, "already linked\n")
    assert world.tickets[reload].body.endswith(f"\n\nDepends on {rule}")

    # Only the first item is ready; the chain reads the same from any of its items
    rows = gate_tick(world)
    assert {ref: state for ref, (state, _) in rows.items()} == {
        page: "NEEDS_PR",
        rule: "BLOCKED",
        reload: "BLOCKED",
        link: "BLOCKED",
    }
    chain = [
        (page, "agent", "READY", None),
        (rule, "agent", "BLOCKED", None),
        (reload, "person: @ana", "BLOCKED", None),
        (link, "agent", "BLOCKED", None),
    ]
    assert sorted(world.show(reload)) == sorted(chain)
    assert sorted(world.show(page)) == sorted(chain)

    # 1. acme/app's session takes up the first item; its PR merges and closes it
    world.close(page, BOT, bot=True, pr=10)
    rows = gate_tick(world)
    assert rows[rule][0] == "UNBLOCKED"  # the second turns ready in its own repo
    assert rows[reload][0] == "BLOCKED" and rows[link][0] == "BLOCKED"
    assert page not in rows  # a close by a merged PR owes nothing
    assert (rule, "agent", "READY", None) in world.show(rule)

    # 2. acme/srv's session takes up the second; it closes, and the person's turn starts
    world.close(rule, BOT, bot=True, pr=11)
    rows = gate_tick(world)
    assert rows[reload] == ("HANDOFF_DUE", f"for @ana {rule}:closed")
    assert rows[link][0] == "BLOCKED"
    assert (reload, "person: @ana", "READY", None) in world.show(link)
    world.comment(reload, handoff(world, reload, [rule]), BOT, bot=True)
    mention = world.tickets[reload].comments[-1].body
    assert mention.startswith(HANDOFF + "\n@ana, it's your turn: acme/srv#2 closed.")
    assert "> Reload the web server" in mention and f"> {CHECK}" in mention

    # It waits on the person, with no action for any gate: nobody mentions them again
    for _ in range(3):
        for repo in REPOS:
            code, found = world.triage(repo)
            assert code == 0, found
        assert world.triage("acme/app")[1][reload][0] == "WITH_PERSON"

    # 3. The person does it and reports, the only thing they do in the chain
    server["reloaded"] = True
    world.comment(reload, "done, reloaded at 10:40", "ana")
    rows = gate_tick(world)
    assert rows[reload] == ("VERIFY_DUE", "done reported by @ana")
    assert rows[link][0] == "BLOCKED"

    # 4. acme/app's session runs the ## Check against the world, not the report, and closes it
    assert server["reloaded"], "the check would fail: the session hands it back instead"
    world.comment(
        reload, f"Checked: {CHECK.split('`')[1]} returned 200, as the ## Check asks. Closing as done.", BOT, bot=True
    )
    world.close(reload, BOT, bot=True)
    rows = gate_tick(world)
    assert reload not in rows  # closed by the session: no VERIFY_CLOSED, no SUSPECT_CLOSE
    assert rows[link][0] == "UNBLOCKED"  # the fourth is taken up in its own repo
    assert (link, "agent", "READY", None) in world.show(link)

    # 5. acme/site's session takes it up; the chain is done and every gate is quiet
    world.close(link, BOT, bot=True, pr=12)
    for repo in REPOS:
        assert world.triage(repo) == (0, {})
    assert all(state == "CLOSED" for _, _, state, _ in world.show(page))

    # The person acted on the third item only, once
    assert [(what, ref) for who, what, ref in world.log if who == "ana"] == [("comment", reload)]
    assert {who for who, _, _ in world.log} == {"maria", f"{BOT}[bot]", "ana"}
