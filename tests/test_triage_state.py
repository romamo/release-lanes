"""The github-issue-triage skill's classification, without gh or the network"""

import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "skills" / "github-issue-triage" / "scripts" / "triage_state.py"


@pytest.fixture(scope="module")
def ts() -> ModuleType:
    spec = importlib.util.spec_from_file_location("triage_state", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def issue(*comments: tuple[str, str], labels: tuple[str, ...] = ()) -> dict[str, Any]:
    """Comments as (createdAt, body), oldest first, as GitHub returns them"""
    return {
        "number": 7,
        "labels": {"nodes": [{"name": n} for n in labels]},
        "comments": {"nodes": [{"createdAt": at, "body": body} for at, body in comments]},
        "timelineItems": {"nodes": []},
    }


def tag(name: str, date: str) -> dict[str, Any]:
    return {"name": name, "target": {"tagger": {"date": date}}}


def classify(ts: ModuleType, item: dict[str, Any], stable: tuple[Any, str] | None = None) -> str:
    state: str = ts.classify_open(item, "Triage:", "postponed", {}, stable)[0]
    return state


def test_the_latest_triage_verdict_wins(ts: ModuleType) -> None:
    item = issue(
        ("2026-09-01T10:00:00Z", "Triage: clarify, which flag?"),
        ("2026-09-02T10:00:00Z", "Triage: implement"),
    )
    assert classify(ts, item) == "NEEDS_PR"


def test_an_old_implement_verdict_does_not_outlive_a_newer_one(ts: ModuleType) -> None:
    item = issue(
        ("2026-09-01T10:00:00Z", "Triage: implement"),
        ("2026-09-02T10:00:00Z", "Triage: clarify, the reporter's repro fails on main"),
    )
    assert classify(ts, item) == "TRIAGED"


def test_postponed_before_the_stable_tag_is_revisited(ts: ModuleType) -> None:
    item = issue(("2026-09-01T10:00:00Z", "Triage: postpone"), labels=("postponed",))
    stable = ts.latest_stable([tag("v1.2.0", "2026-09-10T10:00:00Z")])
    assert classify(ts, item, stable) == "REVISIT"


def test_a_re_decision_after_the_stable_tag_clears_revisit(ts: ModuleType) -> None:
    item = issue(
        ("2026-09-01T10:00:00Z", "Triage: postpone"),
        ("2026-09-20T10:00:00Z", "Triage: postpone again, still out of scope"),
        labels=("postponed",),
    )
    stable = ts.latest_stable([tag("v1.2.0", "2026-09-10T10:00:00Z")])
    assert classify(ts, item, stable) == "POSTPONED"


def test_a_tag_with_an_offset_compares_by_instant_not_by_text(ts: ModuleType) -> None:
    # 12:00+03:00 is 09:00Z, before the 10:00Z verdict; as text "12:00" sorts after "10:00"
    item = issue(("2026-09-10T10:00:00Z", "Triage: postpone"), labels=("postponed",))
    stable = ts.latest_stable([tag("v1.2.0", "2026-09-10T12:00:00+03:00")])
    assert classify(ts, item, stable) == "POSTPONED"


def test_the_latest_stable_tag_is_picked_by_instant(ts: ModuleType) -> None:
    tags = [tag("v1.2.0", "2026-09-10T12:00:00+03:00"), tag("v1.1.0", "2026-09-10T10:00:00Z")]
    assert ts.latest_stable(tags)[1] == "v1.1.0"


def test_a_time_without_a_timezone_fails(ts: ModuleType) -> None:
    with pytest.raises(SystemExit) as exc:
        ts.timestamp("2026-09-10T10:00:00")
    assert exc.value.code == 2


# Paging (#19): the fetch takes its GraphQL runner as a parameter, so these serve recorded
# pages by query and cursor instead of calling gh


class FakeGitHub:
    """A runner serving canned ``repository`` pages, keyed by (query name, issue, cursor)"""

    def __init__(self, ts: ModuleType, pages: dict[tuple[str, int | None, str | None], dict[str, Any]]) -> None:
        names = ("QUERY", "OPEN_PAGE", "COMMENTS_PAGE", "TIMELINE_PAGE", "REFS_PAGE", "TAGS_PAGE")
        self.names = {getattr(ts, n): n for n in names}
        self.pages = pages
        self.calls: list[tuple[str, int | None, str | None]] = []

    def __call__(self, query: str, variables: dict[str, str | int]) -> dict[str, Any]:
        number, cursor = variables.get("number"), variables.get("cursor")
        assert number is None or isinstance(number, int)
        assert cursor is None or isinstance(cursor, str)
        key = (self.names[query], number, cursor)
        self.calls.append(key)
        return {"data": {"repository": self.pages[key]}}


def forward(nodes: list[dict[str, Any]], cursor: str | None = None) -> dict[str, Any]:
    return {"pageInfo": {"hasNextPage": cursor is not None, "endCursor": cursor}, "nodes": nodes}


def backward(nodes: list[dict[str, Any]], cursor: str | None = None) -> dict[str, Any]:
    return {"pageInfo": {"hasPreviousPage": cursor is not None, "startCursor": cursor}, "nodes": nodes}


def open_issue(
    number: int,
    comments: list[dict[str, Any]] | None = None,
    labels: tuple[str, ...] = (),
    timeline: dict[str, Any] | None = None,
    more_labels: bool = False,
) -> dict[str, Any]:
    return {
        "number": number,
        "title": f"issue {number}",
        "labels": {"pageInfo": {"hasNextPage": more_labels}, "nodes": [{"name": n} for n in labels]},
        "comments": {"nodes": comments or []},
        "timelineItems": timeline or forward([]),
    }


def comment(body: str, at: str = "2026-09-01T10:00:00Z") -> dict[str, Any]:
    return {"body": body, "createdAt": at}


def chatter(count: int) -> list[dict[str, Any]]:
    return [comment(f"+1 ({i})", f"2026-09-02T{i // 60:02d}:{i % 60:02d}:00Z") for i in range(count)]


def mention(pr: int, state: str = "OPEN") -> dict[str, Any]:
    """A same-repo PR that mentions the issue without closing it"""
    return {
        "willCloseTarget": False,
        "isCrossRepository": False,
        "source": {"number": pr, "state": state, "title": "Refactor", "body": "Touches the same code"},
    }


def first_page(
    open_nodes: list[dict[str, Any]],
    open_next: str | None = None,
    tags: dict[str, Any] | None = None,
    closed_nodes: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "open": forward(open_nodes, open_next),
        "tags": tags or backward([]),
        "closed": {"nodes": closed_nodes or []},
    }


def test_under_the_caps_it_is_one_query(ts: ModuleType) -> None:
    gh = FakeGitHub(ts, {("QUERY", None, None): first_page([open_issue(1, chatter(ts.COMMENTS_CAP - 1))])})
    ts.fetch("o/r", 20, run=gh)
    assert gh.calls == [("QUERY", None, None)]


def test_open_issues_past_the_first_100_are_classified(ts: ModuleType) -> None:
    gh = FakeGitHub(
        ts,
        {
            ("QUERY", None, None): first_page([open_issue(200, [comment("Triage: clarify")])], open_next="o1"),
            ("OPEN_PAGE", None, "o1"): {"open": forward([open_issue(1)])},
        },
    )
    data = ts.fetch("o/r", 20, run=gh)
    assert [i["number"] for i in data["open"]["nodes"]] == [200, 1]
    assert classify(ts, data["open"]["nodes"][1]) == "NEW"


def test_a_verdict_and_hold_older_than_50_comments_are_read(ts: ModuleType) -> None:
    hold = comment("Triage: implement\n\nOn hold: waits on other/lib#5", "2026-08-01T10:00:00Z")
    newest = chatter(ts.COMMENTS_CAP)
    gh = FakeGitHub(
        ts,
        {
            ("QUERY", None, None): first_page([open_issue(7, newest)]),
            ("COMMENTS_PAGE", 7, None): {"issue": {"comments": backward(newest[-2:], "c1")}},
            ("COMMENTS_PAGE", 7, "c1"): {"issue": {"comments": backward([hold, *newest[:-2]])}},
        },
    )
    item = ts.fetch("o/r", 20, run=gh)["open"]["nodes"][0]
    assert item["comments"]["nodes"] == [hold, *newest]
    assert ts.upstream_refs(item) == [("other", "lib", 5)]
    states = {("other", "lib", 5): "OPEN"}
    assert ts.classify_open(item, "Triage:", "postponed", states, None)[0] == "BLOCKED"


def test_a_pr_linked_after_50_cross_references_is_in_progress(ts: ModuleType) -> None:
    timeline = forward([mention(100 + i) for i in range(50)], "t1")
    closing = {"willCloseTarget": True, "isCrossRepository": False, "source": {"number": 99, "state": "OPEN"}}
    gh = FakeGitHub(
        ts,
        {
            ("QUERY", None, None): first_page([open_issue(7, [comment("Triage: implement")], timeline=timeline)]),
            ("TIMELINE_PAGE", 7, "t1"): {"issue": {"timelineItems": forward([closing])}},
        },
    )
    item = ts.fetch("o/r", 20, run=gh)["open"]["nodes"][0]
    assert classify(ts, item) == "IN_PROGRESS"


def test_a_merged_pr_past_50_references_clears_a_hand_close(ts: ModuleType) -> None:
    closed = {
        "number": 8,
        "title": "issue 8",
        "stateReason": "COMPLETED",
        "labels": {"pageInfo": {"hasNextPage": False}, "nodes": []},
        "refs": forward([mention(100 + i) for i in range(50)], "r1"),
        "timelineItems": {"nodes": [{"closer": None}]},
    }
    gh = FakeGitHub(
        ts,
        {
            ("QUERY", None, None): first_page([], closed_nodes=[closed]),
            ("REFS_PAGE", 8, "r1"): {"issue": {"refs": forward([mention(99, "MERGED")])}},
        },
    )
    item = ts.fetch("o/r", 20, run=gh)["closed"]["nodes"][0]
    assert ts.classify_closed(item, "release-blocker") is None


def test_more_than_100_labels_is_bad_input(ts: ModuleType, capsys: pytest.CaptureFixture[str]) -> None:
    gh = FakeGitHub(ts, {("QUERY", None, None): first_page([open_issue(7, more_labels=True)])})
    with pytest.raises(SystemExit) as exc:
        ts.fetch("o/r", 20, run=gh)
    assert exc.value.code == 2
    assert "#7 has more than 100 labels" in capsys.readouterr().err


def test_tags_are_paged_back_to_the_newest_stable_tag_and_no_further(ts: ModuleType) -> None:
    dev = [tag(f"v1.3.0.dev{i}", f"2026-09-2{i}T10:00:00Z") for i in range(5)]
    postponed = open_issue(7, [comment("Triage: postpone")], labels=("postponed",))
    gh = FakeGitHub(
        ts,
        {
            ("QUERY", None, None): first_page([postponed], tags=backward(dev, "g1")),
            ("TAGS_PAGE", None, "g1"): {"tags": backward([tag("v1.2.0", "2026-09-10T10:00:00Z")], "g2")},
        },
    )
    data = ts.fetch("o/r", 20, run=gh)
    assert gh.calls[-1] == ("TAGS_PAGE", None, "g1")
    stable = ts.latest_stable(data["tags"]["nodes"])
    assert stable[1] == "v1.2.0"
    assert classify(ts, data["open"]["nodes"][0], stable) == "REVISIT"


def test_a_cursor_that_does_not_advance_fails(ts: ModuleType, capsys: pytest.CaptureFixture[str]) -> None:
    gh = FakeGitHub(
        ts,
        {
            ("QUERY", None, None): first_page([open_issue(2)], open_next="o1"),
            ("OPEN_PAGE", None, "o1"): {"open": forward([open_issue(1)], "o1")},
        },
    )
    with pytest.raises(SystemExit) as exc:
        ts.fetch("o/r", 20, run=gh)
    assert exc.value.code == 2
    assert "did not advance" in capsys.readouterr().err


def test_graphql_errors_fail(ts: ModuleType, capsys: pytest.CaptureFixture[str]) -> None:
    def run(query: str, variables: dict[str, str | int]) -> dict[str, Any]:
        return {"data": None, "errors": [{"message": "Resource limits for this query exceeded."}]}

    with pytest.raises(SystemExit) as exc:
        ts.fetch("o/r", 20, run=run)
    assert exc.value.code == 2
    assert "Resource limits" in capsys.readouterr().err
