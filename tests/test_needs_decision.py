"""Spec 005, Waiting on GitHub and Trust: the needs-decision rule and the trust filter in
triage_state.py and watch_state.py, and what the gate makes of a repo whose work waits"""

import datetime as dt
import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from shipmill.gate import Action, Launch, decide, fingerprint, parse_findings

ROOT = Path(__file__).resolve().parents[1]
SKILLS = ROOT / "skills"
REPO = ("o", "r")
MARKER = "<!-- shipmill:needs-decision -->"
QUESTION = f"{MARKER}\n@amy Decision needed: which flag?\n\n1. --x (recommended): simpler\n2. --y: costs more"
BOT = "shipmill-o[bot]"
NOW = dt.datetime(2026, 10, 6, 12, 0, tzinfo=dt.UTC)
DAY = dt.timedelta(hours=24)


def load(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses look their module up by name
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def ts() -> ModuleType:
    return load("triage_state", SKILLS / "github-issue-triage" / "scripts" / "triage_state.py")


@pytest.fixture(scope="module")
def ws() -> ModuleType:
    return load("watch_state", SKILLS / "github-ship-watch" / "scripts" / "watch_state.py")


def author(login: str) -> dict[str, str]:
    """GraphQL's author: a Bot's login comes without "[bot]" """
    if login.endswith("[bot]"):
        return {"__typename": "Bot", "login": login.removesuffix("[bot]")}
    return {"__typename": "User", "login": login}


def comment(body: str, by: str = "amy", association: str = "OWNER") -> dict[str, Any]:
    return {"body": body, "createdAt": "2026-10-06T10:00:00Z", "author": author(by), "authorAssociation": association}


def issue(
    *comments: dict[str, Any],
    labels: tuple[str, ...] = ("needs-decision",),
    by: str = "amy",
    association: str = "OWNER",
) -> dict[str, Any]:
    """An open issue as triage_state.py's query returns it, comments oldest first"""
    return {
        "number": 7,
        "title": "t",
        "body": "",
        "author": author(by),
        "authorAssociation": association,
        "labels": {"nodes": [{"name": n} for n in labels]},
        "comments": {"nodes": list(comments)},
        "timelineItems": {"nodes": []},
    }


def state(ts: ModuleType, item: dict[str, Any], bot_login: str | None = None, trusted_only: bool = False) -> str:
    """As triage_state.py's main reads an open issue"""
    gated = ts.gated(item, bot_login, trusted_only)
    found: str = (gated or ts.classify_open(item, "Triage:", "postponed", {}, None, REPO))[0]
    return found


# -- S-005-7: triage_state.py's NEEDS_DECISION


def test_s005_7_an_unanswered_question_reads_needs_decision_and_is_no_action(ts: ModuleType) -> None:
    item = issue(comment("Triage: implement"), comment(QUESTION))
    assert state(ts, item) == "NEEDS_DECISION"
    assert "NEEDS_DECISION" not in ts.ACTION  # so it never makes the script exit 1


def test_s005_7_a_reply_reads_the_state_it_would_without_the_label(ts: ModuleType) -> None:
    answered = issue(comment("Triage: implement"), comment(QUESTION), comment("1"))
    unlabelled = issue(comment("Triage: implement"), comment(QUESTION), labels=())
    assert state(ts, answered) == state(ts, unlabelled) == "NEEDS_PR"
    assert state(ts, issue(comment(QUESTION), comment("go with 2"))) == "NEW"


def test_s005_7_needs_decision_masks_other_states_only_while_it_waits(ts: ModuleType) -> None:
    postponed = ("needs-decision", "postponed")
    assert state(ts, issue(comment("Triage: postpone"), comment(QUESTION), labels=postponed)) == "NEEDS_DECISION"
    replied = issue(comment("Triage: postpone"), comment(QUESTION), comment("ok"), labels=postponed)
    assert state(ts, replied) == "POSTPONED"


# -- S-005-8: the question and the reply


def test_s005_8_without_bot_login_the_question_is_the_newest_marker_comment(ts: ModuleType) -> None:
    assert state(ts, issue(comment(QUESTION), comment("2"), comment(QUESTION))) == "NEEDS_DECISION"
    # the marker counts only as the first line: a quote of it further down is no question
    quoted = issue(comment(QUESTION), comment(f"About this:\n{MARKER}\nI pick 1"))
    assert state(ts, quoted) == "NEW"
    # a comment carrying the marker never answers: it is a newer question
    assert state(ts, issue(comment(QUESTION), comment(f"{MARKER}\nstill?"))) == "NEEDS_DECISION"


@pytest.mark.parametrize("association", ["OWNER", "MEMBER", "COLLABORATOR"])
def test_s005_8_a_trusted_comment_is_a_reply(ts: ModuleType, association: str) -> None:
    item = issue(comment(QUESTION), comment("1", by="bob", association=association))
    assert state(ts, item) == "NEW"
    assert state(ts, item, bot_login=BOT) == "NEEDS_DECISION"  # with an App, a reply follows the bot's comment


@pytest.mark.parametrize("association", ["CONTRIBUTOR", "FIRST_TIME_CONTRIBUTOR", "FIRST_TIMER", "NONE", "MANNEQUIN"])
def test_s005_8_any_other_author_association_never_replies(ts: ModuleType, association: str) -> None:
    assert state(ts, issue(comment(QUESTION), comment("1", by="eve", association=association))) == "NEEDS_DECISION"
    asked = comment("Decision needed: which flag?", by=BOT, association="NONE")
    item = issue(asked, comment("1", by="eve", association=association))
    assert state(ts, item, bot_login=BOT) == "NEEDS_DECISION"


def test_s005_8_with_bot_login_the_question_is_the_bots_newest_comment(ts: ModuleType) -> None:
    asked = comment("Decision needed: which flag?", by=BOT, association="NONE")
    assert state(ts, issue(asked), bot_login=BOT) == "NEEDS_DECISION"
    assert state(ts, issue(asked, comment("1")), bot_login=BOT) == "NEW"
    # the bot's own later comment is never a reply, whatever its association
    later = comment("Still waiting", by=BOT, association="MEMBER")
    assert state(ts, issue(asked, comment("1"), later), bot_login=BOT) == "NEEDS_DECISION"
    # logins compare as GitHub does, without case
    assert state(ts, issue(asked), bot_login="Shipmill-O[bot]") == "NEEDS_DECISION"


def test_s005_8_a_labelled_issue_with_no_question_waits(ts: ModuleType) -> None:
    assert state(ts, issue()) == "NEEDS_DECISION"
    assert state(ts, issue(comment("parking this until Friday"))) == "NEEDS_DECISION"
    assert state(ts, issue(comment("parking"), comment("still parked")), bot_login=BOT) == "NEEDS_DECISION"


def test_s005_8_the_rule_reads_the_comments_raw(ts: ModuleType) -> None:
    rows = [("amy", "OWNER", QUESTION), ("eve", "CONTRIBUTOR", "1")]
    assert ts.waits_on_decision(rows, None)
    assert not ts.waits_on_decision([*rows, ("bob", "COLLABORATOR", "2")], None)
    assert ts.waits_on_decision([(BOT, "NONE", "q"), (BOT, "OWNER", "again")], BOT)
    assert ts.waits_on_decision([], None)


# -- S-005-10: the trust filter


@pytest.mark.parametrize("association", ["CONTRIBUTOR", "FIRST_TIME_CONTRIBUTOR", "NONE"])
def test_s005_10_trusted_only_reads_an_outsiders_issue_as_untrusted(ts: ModuleType, association: str) -> None:
    item = issue(labels=(), by="eve", association=association)
    assert state(ts, item) == "NEW"  # without the filter, mode 1 works on it
    assert state(ts, item, trusted_only=True) == "UNTRUSTED"
    assert "UNTRUSTED" not in ts.ACTION
    labelled = issue(comment(QUESTION), by="eve", association=association)
    assert state(ts, labelled, trusted_only=True) == "UNTRUSTED"


@pytest.mark.parametrize("association", ["OWNER", "MEMBER", "COLLABORATOR"])
def test_s005_10_trusted_authors_issues_read_as_usual(ts: ModuleType, association: str) -> None:
    assert state(ts, issue(labels=(), by="bob", association=association), trusted_only=True) == "NEW"


def test_s005_10_the_bots_own_issue_is_trusted(ts: ModuleType) -> None:
    item = issue(labels=(), by=BOT, association="NONE")
    assert state(ts, item, trusted_only=True) == "UNTRUSTED"
    assert state(ts, item, bot_login=BOT, trusted_only=True) == "NEW"


def test_s005_10_a_deleted_author_is_untrusted(ts: ModuleType) -> None:
    item = issue(labels=())
    item["author"], item["authorAssociation"] = None, "NONE"
    assert state(ts, item, trusted_only=True) == "UNTRUSTED"


# -- watch_state.py's intake rows


def pr(number: int, *, draft: bool = False, fork: bool = False, labels: tuple[str, ...] = ()) -> dict[str, Any]:
    """One `gh pr list --json number,isDraft,isCrossRepository,labels` row"""
    return {"number": number, "isDraft": draft, "isCrossRepository": fork, "labels": [{"name": n} for n in labels]}


def triage(*rows: tuple[int, str]) -> str:
    return "\n".join(json.dumps({"number": n, "state": s, "title": "t", "note": ""}) for n, s in rows)


Comments = list[tuple[str, str, str]]


def comments(by_pr: dict[int, Comments]) -> Any:
    """A fake for the comments of a pull request; it fails on one the test didn't expect read"""

    def read(number: int) -> Comments:
        if number not in by_pr:
            raise AssertionError(f"read the comments of #{number}")
        return by_pr[number]

    return read


def rows(ws: ModuleType, found: list[Any]) -> dict[str, dict[str, Any]]:
    return {r.state: r.json() for r in found}


def test_s005_9_waiting_items_leave_issues_and_prs_open_for_a_needs_decision_row(ws: ModuleType) -> None:
    out = triage((12, "NEW"), (7, "NEEDS_DECISION"), (9, "NEEDS_PR"))
    prs = [pr(3), pr(4, labels=("needs-decision",)), pr(5, labels=("needs-decision",)), pr(6, draft=True)]
    asked = {4: [("amy", "OWNER", QUESTION)], 5: [("amy", "OWNER", QUESTION), ("bob", "MEMBER", "1")]}
    found = rows(ws, ws.intake("o/r", 1, out, prs, comments(asked)))
    assert found["ISSUES"]["detail"] == "NEEDS_PR #9; NEW #12"
    assert found["PRS_OPEN"]["detail"] == "#3 #5"  # #5 was answered; #6 is a draft
    assert found["NEEDS_DECISION"] == {"state": "NEEDS_DECISION", "subject": "o/r", "detail": "#4 #7", "agent": False}
    assert "NEEDS_DECISION" in ws.ACTION and "NEEDS_DECISION" not in ws.AGENT


def test_s005_9_a_waiting_pr_follows_the_bot_login(ws: ModuleType) -> None:
    asked = {4: [(BOT, "NONE", "Decision needed"), (BOT, "NONE", "still")]}
    found = rows(ws, ws.intake("o/r", 0, "", [pr(4, labels=("needs-decision",))], comments(asked), BOT))
    assert found["NEEDS_DECISION"]["detail"] == "#4" and "PRS_OPEN" not in found


def test_s005_9_a_repo_whose_only_work_waits_reads_quiet_at_the_gate(ws: ModuleType) -> None:
    prs = [pr(4, labels=("needs-decision",))]
    found = ws.intake("o/r", 0, triage((7, "NEEDS_DECISION")), prs, comments({4: [("amy", "OWNER", QUESTION)]}))
    text = "\n".join(json.dumps(r.json(), sort_keys=True) for r in found)
    findings = parse_findings(text)
    assert [f.state for f in findings] == ["NEEDS_DECISION"]
    assert decide(findings, [], None, NOW, DAY).action is Action.QUIET
    assert decide(findings, [], None, NOW, DAY, prs=True).action is Action.QUIET


def test_s005_9_a_waiting_item_does_not_change_the_gates_fingerprint(ws: ModuleType) -> None:
    def work(code: int, out: str, prs: list[dict[str, Any]], asked: dict[int, Comments], last: Launch | None) -> Any:
        text = "\n".join(json.dumps(r.json()) for r in ws.intake("o/r", code, out, prs, comments(asked)))
        return decide(parse_findings(text), [], last, NOW, DAY, prs=True)

    before = work(1, triage((12, "NEW")), [pr(3)], {}, None)
    last = Launch(fingerprint(before.work), "s1", NOW - dt.timedelta(minutes=15))
    # #7 and #4 now wait on a decision: the findings that started s1 are unchanged
    after = work(
        1,
        triage((12, "NEW"), (7, "NEEDS_DECISION")),
        [pr(3), pr(4, labels=("needs-decision",))],
        {4: [("amy", "OWNER", QUESTION)]},
        last,
    )
    assert fingerprint(after.work) == fingerprint(before.work)
    assert after.action is Action.UNCHANGED


def test_s005_10_trusted_only_lists_fork_prs_as_untrusted(ws: ModuleType) -> None:
    prs = [pr(3), pr(8, fork=True), pr(9, fork=True, labels=("needs-decision",))]
    out = triage((11, "UNTRUSTED"), (12, "NEW"))
    found = rows(ws, ws.intake("o/r", 1, out, prs, comments({}), None, True))
    assert found["PRS_OPEN"]["detail"] == "#3"
    assert found["UNTRUSTED"] == {"state": "UNTRUSTED", "subject": "o/r", "detail": "#8 #9 #11", "agent": False}
    assert "UNTRUSTED" not in ws.ACTION and "UNTRUSTED" not in ws.AGENT
    # mode 1 doesn't filter: a fork's pull request is open work
    assert rows(ws, ws.intake("o/r", 0, "", prs[:2], comments({})))["PRS_OPEN"]["detail"] == "#3 #8"


def test_existing_intake_rows_keep_their_shape(ws: ModuleType) -> None:
    found = ws.intake("o/r", 1, triage((12, "NEW"), (9, "NEEDS_PR"), (5, "TRIAGED")), [pr(3), pr(4)], comments({}))
    assert [r.json() for r in found] == [
        {"state": "ISSUES", "subject": "o/r", "detail": "NEEDS_PR #9; NEW #12", "agent": True},
        {"state": "PRS_OPEN", "subject": "o/r", "detail": "#3 #4", "agent": False},
    ]


# -- S-005-17: the reference and the headless rules


def folded(path: Path) -> str:
    return " ".join(path.read_text(encoding="utf-8").split())


def test_s005_17_the_reference_defines_the_protocol() -> None:
    ref = folded(SKILLS / "github-issue-triage" / "references" / "needs-decision.md")
    first = ref.split("```", 2)[1].split(" ", 1)[1].strip()  # the comment template, after its fence's language
    assert first.startswith(f"{MARKER} @<login> Decision needed: <the question")
    assert "1. <option> (recommended):" in first and "2. <option>:" in first
    for rule in (
        "The marker is the comment's first line",
        "Add the `needs-decision` label",
        "Leave the item",
        "A denied tool call is a decision",
        "Removes the `needs-decision` label before it acts",
    ):
        assert rule in ref, rule


@pytest.mark.parametrize("skill", ["github-issue-triage", "github-issue-resolve", "github-pr-triage"])
def test_s005_17_each_skill_carries_a_headless_rule_linking_it(skill: str) -> None:
    text = folded(SKILLS / skill / "SKILL.md")
    link = (
        "references/needs-decision.md"
        if skill == "github-issue-triage"
        else ("../github-issue-triage/references/needs-decision.md")
    )
    assert f"]({link})" in text
    assert "**Headless:**" in text
