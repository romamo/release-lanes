"""Spec 017, #342: a person's item (labelled human) reads HANDOFF_DUE, WITH_PERSON,
VERIFY_DUE, NO_ASSIGNEE, and, closed by hand, VERIFY_CLOSED, without gh or the network"""

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from .test_triage_state import FakeGitHub, backward, first_page, forward, open_issue

SCRIPT = Path(__file__).resolve().parents[1] / "skills" / "github-issue-triage" / "scripts" / "triage_state.py"
REPO = ("o", "r")
BOT = "shipmill-gate[bot]"
HANDOFF = "<!-- shipmill:handoff -->"
VERIFIED = "<!-- shipmill:verified -->"
CHECK = "Reload the server.\n\n## Check\n\n`curl -sI https://example.com/new` returns 200\n"


@pytest.fixture(scope="module")
def ts() -> ModuleType:
    spec = importlib.util.spec_from_file_location("triage_state_human", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def said(body: str, at: str, by: str = "shipmill-gate", association: str = "NONE", bot: bool = True) -> dict[str, Any]:
    """A comment with its author; GraphQL spells a Bot's login without "[bot]" """
    author = {"__typename": "Bot" if bot else "User", "login": by}
    return {"body": body, "createdAt": at, "author": author, "authorAssociation": association}


def by(login: str, association: str = "NONE") -> dict[str, Any]:
    """A person's comment author fields, for said()"""
    return {"by": login, "association": association, "bot": False}


def handoff(at: str = "2026-09-05T10:00:00Z", **who: Any) -> dict[str, Any]:
    return said(f"{HANDOFF}\n@ana it's your turn: reload the server.", at, **who)


def node(ref: str, state: str = "OPEN", closed: str | None = None) -> dict[str, Any]:
    repo, _, number = ref.partition("#")
    return {
        "number": int(number),
        "state": state,
        "stateReason": "COMPLETED" if state == "CLOSED" else None,
        "closedAt": closed,
        "repository": {"nameWithOwner": repo},
    }


def person(
    comments: list[dict[str, Any]] | None = None,
    people: tuple[str, ...] = ("ana",),
    body: str = CHECK,
    blocked_by: list[dict[str, Any]] | None = None,
    labels: tuple[str, ...] = ("human",),
) -> dict[str, Any]:
    item = open_issue(7, comments or [], labels=labels)
    item["body"] = body
    item["assignees"] = {"nodes": [{"login": p} for p in people]}
    item["blockedBy"] = forward(blocked_by or [])
    item["subIssues"] = forward([])
    return item


def classify(
    ts: ModuleType,
    item: dict[str, Any],
    states: dict[tuple[str, str, int], str] | None = None,
    closes: dict[tuple[str, str, int], str] | None = None,
) -> tuple[str, str]:
    verdict: tuple[str, str] = ts.classify_open(
        item, "Triage:", "postponed", states or {}, None, REPO, BOT, closes or {}
    )
    return verdict


def test_s017_10_an_assigned_person_item_with_nothing_holding_it_is_handed_off(ts: ModuleType) -> None:
    # No triage comment: a human item counts as triaged from the start
    assert classify(ts, person()) == ("HANDOFF_DUE", "for @ana")
    assert classify(ts, person(people=("ana", "bo")))[1] == "for @ana @bo"


def test_s017_10_a_depends_on_line_or_a_native_relation_holds_it(ts: ModuleType) -> None:
    text = person(body="Depends on #3\n\n" + CHECK)
    assert classify(ts, text, {("o", "r", 3): "OPEN"}) == ("BLOCKED", "o/r#3:open")
    native = person(blocked_by=[node("other/lib#5")])
    assert classify(ts, native) == ("BLOCKED", "other/lib#5:open(native)")
    # Once its holds closed, it is the person's turn, with the closed holds in the note
    closed = {("o", "r", 3): "2026-09-04T10:00:00Z"}
    assert classify(ts, text, {("o", "r", 3): "CLOSED"}, closed) == ("HANDOFF_DUE", "for @ana o/r#3:closed")
    done = person(blocked_by=[node("other/lib#5", "CLOSED", "2026-09-04T10:00:00Z")])
    assert classify(ts, done) == ("HANDOFF_DUE", "for @ana other/lib#5:closed(native)")


def test_s017_10_a_person_item_never_reads_a_pr_state(ts: ModuleType) -> None:
    item = person()
    item["timelineItems"] = {"nodes": [{"willCloseTarget": True, "source": {"number": 8, "state": "OPEN"}}]}
    assert classify(ts, item)[0] == "HANDOFF_DUE"


def test_s017_11_a_hand_off_after_the_newest_hold_closed_waits_on_the_person(ts: ModuleType) -> None:
    hold = [node("o/r#3", "CLOSED", "2026-09-04T10:00:00Z")]
    item = person([handoff("2026-09-05T10:00:00Z")], blocked_by=hold)
    assert classify(ts, item) == ("WITH_PERSON", "with @ana since 2026-09-05T10:00:00Z")
    assert "WITH_PERSON" not in ts.ACTION
    # However old the hand-off is, with nothing holding the item
    old = person([handoff("2020-01-01T00:00:00Z")])
    assert classify(ts, old)[0] == "WITH_PERSON"


def test_s017_11_a_hand_off_before_the_newest_hold_closed_is_due_again(ts: ModuleType) -> None:
    # Handed off, then a new hold came and closed: the person is told again
    holds = [node("o/r#3", "CLOSED", "2026-09-01T10:00:00Z"), node("o/r#4", "CLOSED", "2026-09-06T10:00:00Z")]
    item = person([handoff("2026-09-05T10:00:00Z")], blocked_by=holds)
    assert classify(ts, item)[0] == "HANDOFF_DUE"
    item["comments"]["nodes"].append(handoff("2026-09-07T10:00:00Z", by="maria", association="OWNER", bot=False))
    assert classify(ts, item)[0] == "WITH_PERSON"


@pytest.mark.parametrize(
    ("report", "who"),
    [
        ("done", by("ana")),  # an assignee needn't be a collaborator
        ("  Done, reloaded at 10:05", by("ANA")),  # any case, past leading whitespace; logins ignore case
        ("DONE", by("maria", "MEMBER")),
        ("done", by("maria", "COLLABORATOR")),
    ],
)
def test_s017_12_a_done_report_after_the_hand_off_asks_for_verification(
    ts: ModuleType, report: str, who: dict[str, Any]
) -> None:
    item = person([handoff(), said(report, "2026-09-06T10:00:00Z", **who)])
    assert classify(ts, item) == ("VERIFY_DUE", f"done reported by @{who['by']}")


@pytest.mark.parametrize(
    ("report", "who", "at"),
    [
        ("done", by("mallory", "CONTRIBUTOR"), "2026-09-06T10:00:00Z"),  # neither assignee nor trusted
        ("done", by("eve"), "2026-09-06T10:00:00Z"),
        ("done", by("ana"), "2026-09-04T10:00:00Z"),  # before the newest hand-off
        ("not done yet", by("ana"), "2026-09-06T10:00:00Z"),
        ("> done\nquoting", by("ana"), "2026-09-06T10:00:00Z"),
    ],
)
def test_s017_12_any_other_comment_changes_nothing(ts: ModuleType, report: str, who: dict[str, Any], at: str) -> None:
    comments = sorted([handoff(), said(report, at, **who)], key=lambda c: str(c["createdAt"]))  # oldest first
    assert classify(ts, person(comments))[0] == "WITH_PERSON"


def test_s017_13_a_hand_off_marker_by_an_untrusted_author_is_ignored(ts: ModuleType) -> None:
    for who in (by("mallory", "CONTRIBUTOR"), by("ana"), {"by": "other-app", "bot": True}):
        item = person([handoff(**who)])
        assert classify(ts, item)[0] == "HANDOFF_DUE", who
    # The trusted forms: the --bot-login, an OWNER, a MEMBER, a COLLABORATOR
    for who in ({}, by("maria", "OWNER"), by("maria", "MEMBER"), by("maria", "COLLABORATOR")):
        assert classify(ts, person([handoff(**who)]))[0] == "WITH_PERSON", who
    # A marker quoted further down is no hand-off
    quoted = person([said(f"See the last one:\n{HANDOFF}", "2026-09-05T10:00:00Z")])
    assert classify(ts, quoted)[0] == "HANDOFF_DUE"


def test_s017_13_a_verified_marker_by_an_untrusted_author_is_ignored(ts: ModuleType) -> None:
    item = closed_by_hand([said(VERIFIED, "2026-09-08T10:00:00Z", **by("mallory", "CONTRIBUTOR"))])
    assert ts.classify_closed(item, "release-blocker", "incident", BOT)[0] == "VERIFY_CLOSED"
    trusted = closed_by_hand([said(VERIFIED, "2026-09-08T10:00:00Z")])
    assert ts.classify_closed(trusted, "release-blocker", "incident", BOT) is None


def test_s017_14_a_person_item_with_no_assignee_reads_no_assignee(ts: ModuleType) -> None:
    assert classify(ts, person(people=())) == ("NO_ASSIGNEE", "labelled human, no assignee")
    # whatever else it would read
    blocked = person(people=(), blocked_by=[node("o/r#3")])
    assert classify(ts, blocked)[0] == "NO_ASSIGNEE"
    assert "NO_ASSIGNEE" in ts.ACTION


def closed_by_hand(
    comments: list[dict[str, Any]] | None = None,
    closer: str = "ana",
    labels: tuple[str, ...] = ("human",),
    body: str = CHECK,
    reason: str = "COMPLETED",
) -> dict[str, Any]:
    """A person's item closed by hand at 2026-09-07T10:00:00Z"""
    return {
        "number": 9,
        "title": "Reload the web server",
        "stateReason": reason,
        "body": body,
        "labels": {"pageInfo": {"hasNextPage": False}, "nodes": [{"name": n} for n in labels]},
        "refs": forward([]),
        "timelineItems": {
            "nodes": [
                {
                    "createdAt": "2026-09-07T10:00:00Z",
                    "actor": {"__typename": "User", "login": closer},
                    "closer": None,
                }
            ]
        },
        "comments": backward(comments or []),
    }


def test_s017_15_a_person_item_closed_by_hand_with_a_check_is_verified(ts: ModuleType) -> None:
    item = closed_by_hand([said(VERIFIED, "2026-09-06T10:00:00Z")])  # verified before this close
    assert ts.classify_closed(item, "release-blocker", "incident", BOT) == (
        "VERIFY_CLOSED",
        "closed by @ana: run its ## Check",
    )
    assert "VERIFY_CLOSED" in ts.ACTION
    # The same close without the label is the usual SUSPECT_CLOSE
    plain = closed_by_hand(labels=())
    assert ts.classify_closed(plain, "release-blocker", "incident", BOT)[0] == "SUSPECT_CLOSE"


def test_s017_15_a_verified_comment_after_the_close_reads_neither(ts: ModuleType) -> None:
    for who in ({}, by("maria", "OWNER")):
        item = closed_by_hand([said(f"{VERIFIED}\nThe check returned 200.", "2026-09-08T10:00:00Z", **who)])
        assert ts.classify_closed(item, "release-blocker", "incident", BOT) is None


def test_s017_15_closes_that_owe_no_check_read_neither(ts: ModuleType) -> None:
    by_bot = closed_by_hand()
    by_bot["timelineItems"]["nodes"][0]["actor"] = {"__typename": "Bot", "login": "shipmill-gate"}
    for item in (by_bot, closed_by_hand(body="Reload the server.\n"), closed_by_hand(reason="NOT_PLANNED")):
        assert ts.classify_closed(item, "release-blocker", "incident", BOT) is None


def test_s017_15_fetch_reads_a_checkable_close_s_comments_back_to_the_close(ts: ModuleType) -> None:
    item = closed_by_hand()
    del item["comments"]  # the first query doesn't ask a closed issue for comments
    after = said(VERIFIED, "2026-09-08T10:00:00Z")
    before = said("working on it", "2026-09-06T10:00:00Z", **by("ana"))
    gh = FakeGitHub(
        ts,
        {
            ("QUERY", None, None): first_page([], closed_nodes=[item]),
            ("COMMENTS_PAGE", 9, None): {"issue": {"comments": backward([after], "c1")}},
            ("COMMENTS_PAGE", 9, "c1"): {"issue": {"comments": backward([before], "c0")}},
        },
    )
    fetched = ts.fetch("o/r", 20, run=gh)["closed"]["nodes"][0]
    # It stops paging at a comment older than the close: "c0" is never asked for
    assert gh.calls == [("QUERY", None, None), ("COMMENTS_PAGE", 9, None), ("COMMENTS_PAGE", 9, "c1")]
    assert fetched["comments"]["nodes"] == [before, after]
    assert ts.classify_closed(fetched, "release-blocker", "incident", BOT) is None


def test_s017_16_wip_counts_no_person_item(ts: ModuleType) -> None:
    rows = [{"number": n, "state": s} for n, s in ((4, "UNBLOCKED"), (5, "IN_PROGRESS"), (7, "NEEDS_PR"))]
    assert ts.wip_room(rows, 3, frozenset({5, 7})) == {"wip": 3, "in_progress": 0, "room": 3, "ready": [4]}


# The script end to end with a stand-in gh on PATH answering every query from one file
FAKE_GH = """#!{python}
import json, os, sys
query = next(a[len("query="):] for a in sys.argv[1:] if a.startswith("query="))
fake = json.loads(open(os.environ["FAKE_GH_DATA"]).read())
print(json.dumps({{"data": {{"repository": fake["comments" if "issue(number" in query else "first"]}}}}))
"""


def run_script(tmp_path: Path, first: dict[str, Any], *flags: str) -> subprocess.CompletedProcess[str]:
    fakes = tmp_path / "fakes"
    fakes.mkdir()
    gh = fakes / "gh"
    gh.write_text(FAKE_GH.format(python=sys.executable), encoding="utf-8")
    gh.chmod(0o700)
    data = tmp_path / "gh.json"
    comments = {"issue": {"comments": backward([])}}
    data.write_text(json.dumps({"first": first, "comments": comments}), encoding="utf-8")
    env = {"PATH": os.pathsep.join([str(fakes), "/usr/bin", "/bin"]), "FAKE_GH_DATA": str(data)}
    cmd = [sys.executable, str(SCRIPT), "o/r", "--json", "--bot-login", BOT, *flags]
    return subprocess.run(cmd, capture_output=True, text=True, check=False, env=env)


@pytest.mark.parametrize(
    ("open_nodes", "closed_nodes", "state"),
    [
        ([person()], [], "HANDOFF_DUE"),
        ([person([handoff(), said("done", "2026-09-06T10:00:00Z", **by("ana"))])], [], "VERIFY_DUE"),
        ([person(people=())], [], "NO_ASSIGNEE"),
        ([], [closed_by_hand()], "VERIFY_CLOSED"),
    ],
)
def test_s017_16_the_script_exits_1_on_each_new_action_state(
    tmp_path: Path, open_nodes: list[dict[str, Any]], closed_nodes: list[dict[str, Any]], state: str
) -> None:
    for item in closed_nodes:
        del item["comments"]
    proc = run_script(tmp_path, first_page(open_nodes, closed_nodes=closed_nodes))
    assert proc.returncode == 1, proc.stderr
    assert [json.loads(line)["state"] for line in proc.stdout.splitlines()] == [state]


def test_s017_11_a_text_hold_another_issue_holds_natively_keeps_its_close_time(tmp_path: Path) -> None:
    # #3 is a text hold of the person's item and a native one of #8, so the script asks the
    # forge for it only through #8's relations: its close still dates the hand-off
    item = person([handoff("2026-09-05T10:00:00Z")], body="Depends on #3\n\n" + CHECK)
    other = open_issue(8)
    other["blockedBy"] = forward([node("o/r#3", "CLOSED", "2026-09-06T10:00:00Z")])
    other["subIssues"] = forward([])
    proc = run_script(tmp_path, first_page([item, other]))
    rows = {json.loads(line)["number"]: json.loads(line)["state"] for line in proc.stdout.splitlines()}
    assert rows[7] == "HANDOFF_DUE", proc.stderr


def test_s017_16_with_person_is_no_action_and_wip_counts_no_person_item(tmp_path: Path) -> None:
    waiting = person([handoff()])
    # A person's item with an open PR linked would read IN_PROGRESS were it an agent's
    waiting["timelineItems"] = forward([{"willCloseTarget": True, "source": {"number": 8, "state": "OPEN"}}])
    proc = run_script(tmp_path, first_page([waiting]), "--wip", "1")
    assert proc.returncode == 0, proc.stderr
    rows = [json.loads(line) for line in proc.stdout.splitlines()]
    assert rows[0]["state"] == "WITH_PERSON"
    assert rows[1] == {"in_progress": 0, "ready": [], "room": 1, "wip": 1}
