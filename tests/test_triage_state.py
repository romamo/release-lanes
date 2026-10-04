"""The github-issue-triage skill's classification, without gh or the network"""

import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "skills" / "github-issue-triage" / "scripts" / "triage_state.py"
REPO = ("o", "r")


@pytest.fixture(scope="module")
def ts() -> ModuleType:
    spec = importlib.util.spec_from_file_location("triage_state", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def issue(*comments: tuple[str, str], labels: tuple[str, ...] = (), body: str = "") -> dict[str, Any]:
    """Comments as (createdAt, body), oldest first, as GitHub returns them"""
    return {
        "number": 7,
        "body": body,
        "labels": {"nodes": [{"name": n} for n in labels]},
        "comments": {"nodes": [{"createdAt": at, "body": body} for at, body in comments]},
        "timelineItems": {"nodes": []},
    }


def tag(name: str, date: str) -> dict[str, Any]:
    return {"name": name, "target": {"tagger": {"date": date}}}


def classify(ts: ModuleType, item: dict[str, Any], stable: tuple[Any, str] | None = None) -> str:
    state: str = ts.classify_open(item, "Triage:", "postponed", {}, stable, REPO)[0]
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
        "body": "",
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
    assert ts.upstream_refs(item, REPO) == [("other", "lib", 5)]
    states = {("other", "lib", 5): "OPEN"}
    assert ts.classify_open(item, "Triage:", "postponed", states, None, REPO)[0] == "BLOCKED"


def test_a_hold_on_a_spec_pr_lifts_only_when_it_merges(ts: ModuleType) -> None:
    item = issue(("2026-09-01T10:00:00Z", "Triage: **feature**, the spec is o/r#60\n\nOn hold: waits on o/r#60"))
    ref = ("o", "r", 60)
    assert ts.upstream_refs(item, REPO) == [ref]
    # issueOrPullRequest answers for a PR's number as for an issue's: they share one sequence
    for node, state in (
        ({"__typename": "PullRequest", "state": "OPEN"}, "BLOCKED"),
        ({"__typename": "PullRequest", "state": "CLOSED"}, "SPEC_REFUSED"),
        ({"__typename": "PullRequest", "state": "MERGED"}, "UNBLOCKED"),
        ({"__typename": "Issue", "state": "CLOSED"}, "UNBLOCKED"),
    ):
        states = {ref: ts.ref_state(node)}
        assert ts.classify_open(item, "Triage:", "postponed", states, None, REPO)[0] == state, node


def test_a_plain_hash_number_on_a_hold_line_is_the_same_repo(ts: ModuleType) -> None:
    item = issue(("2026-09-01T10:00:00Z", "Triage: **feature**, spec in #60\n\nOn hold: waits on #60 and other/lib#5"))
    assert ts.upstream_refs(item, REPO) == [("o", "r", 60), ("other", "lib", 5)]
    states = {("o", "r", 60): "OPEN", ("other", "lib", 5): "CLOSED"}
    assert ts.classify_open(item, "Triage:", "postponed", states, None, REPO) == (
        "BLOCKED",
        "o/r#60:open other/lib#5:closed",
    )
    # Only a hold line counts, and a URL's #fragment or o/r#N's number is not a plain #N
    quiet = issue(
        (
            "2026-09-01T10:00:00Z",
            "Triage: implement, see #61\nblocked: o/r#62, https://github.com/o/r/pull/62#issuecomment-9",
        )
    )
    assert ts.upstream_refs(quiet, REPO) == [("o", "r", 62)]


def test_a_plain_hash_number_waited_on_holds_until_it_closes(ts: ModuleType) -> None:
    item = issue(("2026-09-01T10:00:00Z", "Triage: clarify\n\nOn hold: waits on #12"))
    assert ts.upstream_refs(item, REPO) == [("o", "r", 12)]
    for state, expected in (("OPEN", "BLOCKED"), ("CLOSED", "UNBLOCKED")):
        assert ts.classify_open(item, "Triage:", "postponed", {("o", "r", 12): state}, None, REPO)[0] == expected


def test_a_plain_hash_number_as_context_on_the_hold_line_holds_nothing(ts: ModuleType) -> None:
    # comments.md's hold template: #PR and #59 are context, o/r#40 is what it waits on
    line = (
        "On hold: #58 goes below the spec's default of 5 rotated files, decided in o/r#40. "
        "#59 stays open, rebased onto main."
    )
    item = issue(("2026-09-01T10:00:00Z", f"Triage: postpone\n\n{line}"))
    assert ts.upstream_refs(item, REPO) == [("o", "r", 40)]
    states = {("o", "r", 40): "CLOSED", ("o", "r", 59): "OPEN", ("o", "r", 58): "OPEN"}
    assert ts.classify_open(item, "Triage:", "postponed", states, None, REPO)[0] == "UNBLOCKED"


REFUSED_SPEC = {("o", "r", 60): "CLOSED_UNMERGED"}
FEATURE = ("2026-09-01T10:00:00Z", "Triage: **feature**, the spec is o/r#60\n\nOn hold: the build waits on o/r#60")


def test_a_refused_spec_pr_asks_for_a_new_decision(ts: ModuleType) -> None:
    item = issue(FEATURE, ("2026-09-03T10:00:00Z", "The spec PR was closed: wrong layer"))
    state, note = ts.classify_open(item, "Triage:", "postponed", REFUSED_SPEC, None, REPO)
    assert (state, note) == ("SPEC_REFUSED", "o/r#60:closed_unmerged")
    assert "SPEC_REFUSED" in ts.ACTION


def test_a_newer_verdict_settles_a_refused_spec_pr(ts: ModuleType) -> None:
    postponed = issue(FEATURE, ("2026-09-04T10:00:00Z", "Triage: postpone to v2"), labels=("postponed",))
    assert ts.classify_open(postponed, "Triage:", "postponed", REFUSED_SPEC, None, REPO)[0] == "POSTPONED"
    revised = issue(FEATURE, ("2026-09-04T10:00:00Z", "Triage: **feature**, revised\n\nOn hold: waits on o/r#70"))
    for state, expected in (("OPEN", "BLOCKED"), ("MERGED", "UNBLOCKED")):
        states = {**REFUSED_SPEC, ("o", "r", 70): state}
        assert ts.classify_open(revised, "Triage:", "postponed", states, None, REPO) == (
            expected,
            f"o/r#70:{state.lower()}",
        )


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


# Back-off (#24): GitHub rejects a costly page with one RESOURCE_LIMITS_EXCEEDED error per
# node it dropped; the fetch asks again at half the size, down to MIN_PAGE


def rejected(nodes: int) -> dict[str, Any]:
    error = {"type": "RESOURCE_LIMITS_EXCEEDED", "message": "Resource limits for this query exceeded."}
    return {"data": {"repository": None}, "errors": [dict(error) for _ in range(nodes)]}


class HeavyGitHub:
    """A repo with ``count`` open issues that rejects any open-issue page bigger than
    ``limit`` (``first_limit`` for the first query). A cursor is the index of the next
    issue, so it holds across page sizes, as GitHub's do"""

    def __init__(self, ts: ModuleType, count: int, limit: int, first_limit: int | None = None) -> None:
        self.names = {ts.QUERY: "QUERY", ts.OPEN_PAGE: "OPEN_PAGE"}
        self.issues = [open_issue(n) for n in range(count, 0, -1)]
        self.limits = {"QUERY": limit if first_limit is None else first_limit, "OPEN_PAGE": limit}
        self.calls: list[tuple[str, int]] = []

    def __call__(self, query: str, variables: dict[str, str | int]) -> dict[str, Any]:
        name, size = self.names[query], variables["size"]
        assert isinstance(size, int)
        self.calls.append((name, size))
        if size > self.limits[name]:
            return rejected(size)
        start = int(variables.get("cursor", 0))
        end = start + size
        page = forward(self.issues[start:end], str(end) if end < len(self.issues) else None)
        if name == "QUERY":
            return {"data": {"repository": {"open": page, "tags": backward([]), "closed": {"nodes": []}}}}
        return {"data": {"repository": {"open": page}}}


def test_a_rejected_first_query_is_asked_again_at_half_the_size(ts: ModuleType) -> None:
    gh = HeavyGitHub(ts, count=120, limit=30)
    data = ts.fetch("o/r", 20, run=gh)
    assert [i["number"] for i in data["open"]["nodes"]] == list(range(120, 0, -1))
    assert {classify(ts, i) for i in data["open"]["nodes"]} == {"NEW"}
    # 100, 50, then 25 fits; later pages start at 25 instead of being rejected at 50 first
    assert gh.calls == [("QUERY", 100), ("QUERY", 50), ("QUERY", 25)] + [("OPEN_PAGE", 25)] * 4


def test_a_rejected_later_page_backs_off_from_its_cursor(ts: ModuleType) -> None:
    gh = HeavyGitHub(ts, count=160, limit=30, first_limit=100)
    data = ts.fetch("o/r", 20, run=gh)
    assert [i["number"] for i in data["open"]["nodes"]] == list(range(160, 0, -1))
    assert gh.calls == [("QUERY", 100), ("OPEN_PAGE", 50)] + [("OPEN_PAGE", 25)] * 3


def test_a_page_rejected_at_the_floor_fails_with_one_line(ts: ModuleType, capsys: pytest.CaptureFixture[str]) -> None:
    gh = HeavyGitHub(ts, count=120, limit=ts.MIN_PAGE - 1)
    with pytest.raises(SystemExit) as exc:
        ts.fetch("o/r", 20, run=gh)
    assert exc.value.code == 2
    assert gh.calls == [("QUERY", 100), ("QUERY", 50), ("QUERY", 25), ("QUERY", 12), ("QUERY", 10)]
    assert capsys.readouterr().err.splitlines() == [
        "error: o/r: GitHub's resource limits rejected the first query (open issues, tags, recent closes)"
        " even at 10 per page"
    ]


def test_an_error_besides_resource_limits_is_not_retried(ts: ModuleType, capsys: pytest.CaptureFixture[str]) -> None:
    calls: list[str] = []

    def run(query: str, variables: dict[str, str | int]) -> dict[str, Any]:
        calls.append(query)
        response = rejected(2)
        response["errors"].append({"type": "NOT_FOUND", "message": "Could not resolve to a Repository"})
        return response

    with pytest.raises(SystemExit) as exc:
        ts.fetch("o/r", 20, run=run)
    assert exc.value.code == 2
    assert len(calls) == 1
    assert "NOT_FOUND" in capsys.readouterr().err


class BackwardGitHub:
    """Issue #7's comments and the repo's tags, both paged backwards by a cursor that is the
    index of the oldest item served, so it holds across sizes (as checked live on pypa/pip).
    A page asked with a cursor at more than ``limit`` is rejected, so the size shrinks
    mid-pagination"""

    def __init__(self, ts: ModuleType, comments: int, tags: int, limit: int) -> None:
        self.names = {ts.QUERY: "QUERY", ts.COMMENTS_PAGE: "COMMENTS_PAGE", ts.TAGS_PAGE: "TAGS_PAGE"}
        self.comments = chatter(comments)
        # Only the oldest tag is stable, so the fetch pages back through all of them
        self.tags = [tag("v1.0.0", "2026-01-01T00:00:00Z")] + [
            tag(f"v1.0.1rc{i}", f"2026-02-{1 + i // 24:02d}T{i % 24:02d}:00:00Z") for i in range(tags - 1)
        ]
        self.cap, self.limit = ts.COMMENTS_CAP, limit
        self.calls: list[tuple[str, int]] = []

    def __call__(self, query: str, variables: dict[str, str | int]) -> dict[str, Any]:
        name, cursor = self.names[query], variables.get("cursor")
        size = variables["size"]
        assert isinstance(size, int)
        self.calls.append((name, size))
        if name == "QUERY":
            # The first query holds the newest 50 comments and the newest 50 tags
            newest = backward(self.tags[-50:], str(len(self.tags) - 50))
            return {"data": {"repository": first_page([open_issue(7, self.comments[-self.cap :])], tags=newest)}}
        if cursor is not None and size > self.limit:
            return rejected(size)
        items = self.comments if name == "COMMENTS_PAGE" else self.tags
        end = len(items) if cursor is None else int(cursor)
        start = max(end - size, 0)
        page = backward(items[start:end], str(start) if start else None)
        if name == "COMMENTS_PAGE":
            return {"data": {"repository": {"issue": {"comments": page}}}}
        return {"data": {"repository": {"tags": page}}}


def test_backward_pages_back_off_mid_pagination(ts: ModuleType) -> None:
    gh = BackwardGitHub(ts, comments=180, tags=160, limit=30)
    data = ts.fetch("o/r", 20, run=gh)
    assert data["open"]["nodes"][0]["comments"]["nodes"] == gh.comments
    assert data["tags"]["nodes"] == gh.tags
    assert ts.latest_stable(data["tags"]["nodes"])[1] == "v1.0.0"
    # Comments: the newest 100 fit; the 80 older are rejected at 100 and 50, then read at 25.
    # Tags: the first query held 50; the 110 older are read at 25 after the same back-off
    comments = [("COMMENTS_PAGE", 100), ("COMMENTS_PAGE", 100), ("COMMENTS_PAGE", 50)] + [("COMMENTS_PAGE", 25)] * 4
    tags = [("TAGS_PAGE", 100), ("TAGS_PAGE", 50)] + [("TAGS_PAGE", 25)] * 5
    assert gh.calls == [("QUERY", 100), *comments, *tags]


# Build issues split from a spec (#71): "Depends on" lines in the issue body are holds


def test_depends_on_lines_name_issues_in_any_form(ts: ModuleType) -> None:
    body = (
        "Builds part of spec S-007.\n\n"
        "Depends on #3\n"
        "- Depends on: other/lib#4, #5\n"
        "depends on https://github.com/o/r/issues/6\n"
        "This depends on #9 only mid-sentence\n"
        "Depends on #{B1}, a placeholder never filled in\n"
    )
    refs = ts.dependency_refs(issue(body=body), ("o", "r"))
    assert refs == [("o", "r", 3), ("o", "r", 5), ("o", "r", 6), ("other", "lib", 4)]


def test_a_body_dependency_reads_a_plain_number_anywhere_on_its_line(ts: ModuleType) -> None:
    # A hold comment counts a plain #N only right after a hold phrase (#68); a "Depends on"
    # line of the body is the dependency itself, so every #N on it counts, in any case
    body = "DEPENDS ON: the parser (#3) and the docs, #4\n"
    assert ts.dependency_refs(issue(body=body), ("o", "r")) == [("o", "r", 3), ("o", "r", 4)]
    comment = issue(("2026-09-01T10:00:00Z", "On hold: depends on the parser (#3)"))
    assert ts.upstream_refs(comment, ("o", "r")) == []


def test_a_build_issue_is_blocked_until_its_dependency_closes(ts: ModuleType) -> None:
    item = issue(("2026-09-01T10:00:00Z", "Triage: implement, a build issue of S-007"), body="Depends on #3\n")
    for state, expected in (("OPEN", "BLOCKED"), ("CLOSED", "UNBLOCKED")):
        states = {("o", "r", 3): state}
        verdict = ts.classify_open(item, "Triage:", "postponed", states, None, ("o", "r"))
        assert verdict == (expected, f"o/r#3:{state.lower()}")
    both = issue(body="Depends on #3\nDepends on other/lib#4\n")
    states = {("o", "r", 3): "CLOSED", ("other", "lib", 4): "OPEN"}
    assert ts.classify_open(both, "Triage:", "postponed", states, None, ("o", "r"))[0] == "BLOCKED"


def test_a_stacked_pr_on_an_open_dependency_reads_in_progress(ts: ModuleType) -> None:
    item = issue(body="Depends on #3\n")
    item["timelineItems"] = {"nodes": [{"willCloseTarget": True, "source": {"number": 8, "state": "OPEN"}}]}
    states = {("o", "r", 3): "OPEN"}
    assert ts.classify_open(item, "Triage:", "postponed", states, None, ("o", "r"))[0] == "IN_PROGRESS"


def test_the_wip_limit_leaves_room_for_the_oldest_ready_issues(ts: ModuleType) -> None:
    rows = [
        {"number": n, "state": s}
        for n, s in ((9, "NEEDS_PR"), (4, "UNBLOCKED"), (5, "IN_PROGRESS"), (6, "IN_PROGRESS"), (7, "BLOCKED"))
    ]
    assert ts.wip_room(rows, 3) == {"wip": 3, "in_progress": 2, "room": 1, "ready": [4, 9]}
    assert ts.wip_room(rows, 2)["room"] == 0
    assert ts.wip_room(rows, 1)["room"] == 0
