"""The github-ship-watch skill's metrics, from fixture data and a fake GraphQL runner"""

from __future__ import annotations

import datetime as dt
import importlib.util
import sys
from collections.abc import Callable
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "skills" / "github-ship-watch" / "scripts" / "metrics.py"
END = dt.datetime(2026, 10, 1, tzinfo=dt.timezone.utc)  # noqa: UP017 (these tests run on 3.10 too)


@pytest.fixture(scope="module")
def mx() -> ModuleType:
    spec = importlib.util.spec_from_file_location("metrics", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def day(n: float) -> str:
    """n days before END, as GitHub writes a time"""
    return (END - dt.timedelta(days=n)).isoformat().replace("+00:00", "Z")


def window(mx: ModuleType, days: int = 30) -> Any:
    return mx.Window(END - dt.timedelta(days=days), END)


def release(tag: str, published: str, *, pre: bool = False) -> dict[str, Any]:
    return {"tagName": tag, "publishedAt": published, "isDraft": False, "isPrerelease": pre}


def pull(
    number: int,
    merged: str,
    merge: str = "",
    first: str = "",
    by: tuple[str, str] = ("User", "alice"),
    approvers: tuple[tuple[str, str], ...] = (),
    body: str = "",
    messages: tuple[str, ...] = ("Fix a thing",),
    more: str | None = None,
) -> dict[str, Any]:
    """messages: the PR's commit messages, oldest first; more: a cursor to more commits"""
    commits = [{"commit": {"authoredDate": first or merged, "message": m}} for m in messages]
    return {
        "number": number,
        "updatedAt": merged,
        "mergedAt": merged,
        "body": body,
        "mergeCommit": {"oid": merge or f"m{number}"},
        "mergedBy": {"__typename": by[0], "login": by[1]},
        "commits": {"pageInfo": {"hasNextPage": more is not None, "endCursor": more}, "nodes": commits},
        "reviews": {
            "pageInfo": {"hasNextPage": False},
            "nodes": [{"author": {"__typename": t, "login": login}} for t, login in approvers],
        },
    }


def issue(number: int, created: str, closed: str | None = None, *comments: tuple[str, str]) -> dict[str, Any]:
    """Comments as (createdAt, body), oldest first"""
    return {
        "number": number,
        "createdAt": created,
        "closedAt": closed,
        "comments": {
            "pageInfo": {"hasNextPage": False, "endCursor": None},
            "nodes": [{"createdAt": at, "body": body} for at, body in comments],
        },
    }


def deployment(env: str, created: str, *states: str) -> dict[str, Any]:
    return {
        "environment": env,
        "createdAt": created,
        "statuses": {"pageInfo": {"hasNextPage": False}, "nodes": [{"state": s} for s in states]},
    }


def data(mx: ModuleType, **fields: Any) -> Any:
    base: dict[str, Any] = {
        "releases": [],
        "shipped": {},
        "hotfixes": set(),
        "pulls": [],
        "deployed": False,
        "deployments": [],
        "incidents": [],
        "blockers": [],
        "notices": [],
    }
    base.update(fields)
    if base["releases"] and isinstance(base["releases"][0], dict):
        base["releases"] = mx.stable_releases(base["releases"])
    return mx.Data(**base)


def by_name(mx: ModuleType, d: Any, days: int = 30) -> dict[str, Any]:
    return {m.name: m for m in mx.measures(d, window(mx, days))}


def test_an_empty_window_reads_no_data_never_zero(mx: ModuleType) -> None:
    old = [release("v1.0.0", day(60))]
    found = by_name(mx, data(mx, releases=old, pulls=[pull(1, day(50))]))
    for m in found.values():
        assert m.value is None, m.name
        assert m.text == "no data", m.name
        assert m.json()["value"] is None


def test_a_repo_without_deployments_counts_stable_releases(mx: ModuleType) -> None:
    releases = [
        release("v1.1.0", day(2)),
        release("v1.1.0rc1", day(5), pre=True),
        release("v1.0.1", day(9)),
        release("v1.0.0", day(40)),
    ]
    m = by_name(mx, data(mx, releases=releases))["Deploy frequency"]
    assert m.extra == {"source": "releases", "count": 2}
    assert m.value == pytest.approx(2 / (30 / 7))
    assert "v1.1.0 v1.0.1" in m.detail


def test_deployments_count_per_environment_only_when_they_reached_success(mx: ModuleType) -> None:
    deploys = [
        deployment("prod", day(1), "IN_PROGRESS", "SUCCESS", "FAILURE"),  # a later health failure still deployed
        deployment("prod", day(3), "IN_PROGRESS", "FAILURE"),
        deployment("staging", day(4), "SUCCESS"),
    ]
    found = by_name(mx, data(mx, deployed=True, deployments=deploys, releases=[release("v1.0.0", day(2))]))
    m = found["Deploy frequency"]
    assert m.extra["source"] == "deployments"
    assert m.extra["environments"] == {"prod": 1, "staging": 1}
    assert found["Change failure rate"].text == "0%"  # two deploys, no incident: a real zero


def test_deployed_but_none_in_the_window_is_no_data(mx: ModuleType) -> None:
    m = by_name(mx, data(mx, deployed=True))["Deploy frequency"]
    assert m.value is None


def test_lead_time_runs_from_the_first_commit_to_the_first_release_that_shipped_it(mx: ModuleType) -> None:
    releases = [release("v1.2.0", day(1)), release("v1.1.0", day(5)), release("v1.0.0", day(40))]
    # c1 is first shipped by v1.1.0; a later release listing it again doesn't move it
    shipped = {"v1.1.0": {"c1", "c2"}, "v1.2.0": {"c1", "c3"}}
    pulls = [
        pull(1, day(6), "c1", first=day(7)),  # 2 days to v1.1.0
        pull(2, day(5.5), "c2", first=day(6)),  # 1 day
        pull(3, day(2), "c3", first=day(5)),  # 4 days to v1.2.0
        pull(4, day(0.5), "c4"),  # merged, not released yet
    ]
    m = by_name(mx, data(mx, releases=releases, shipped=shipped, pulls=pulls))["Lead time for changes"]
    assert m.value == pytest.approx(48.0)
    assert m.extra == {"p90": pytest.approx(96.0), "count": 3}
    assert m.detail == "3 PRs in 2 releases"


def test_the_previous_release_is_the_next_lower_version_not_the_last_published(mx: ModuleType) -> None:
    releases = mx.stable_releases(
        [release("v2.1.1", day(1)), release("v1.9.9", day(3)), release("v2.1.0", day(20)), release("v2.0.0", day(50))]
    )
    assert mx.previous(releases[0], releases).tag == "v2.1.0"
    assert mx.previous(releases[-1], releases) is None


def test_change_failure_without_deployments_counts_blockers_and_hotfixes(mx: ModuleType) -> None:
    releases = [release("v1.0.2", day(1)), release("v1.0.1", day(10)), release("v1.0.0", day(20))]
    blockers = [issue(7, day(12)), issue(3, day(45))]  # #3 opened before the window
    d = data(mx, releases=releases, hotfixes={"v1.0.1"}, blockers=blockers, incidents=[issue(9, day(2))])
    m = by_name(mx, d)["Change failure rate"]
    assert m.value == pytest.approx(2 / 3)
    assert m.detail == "1 blocker issues + 1 hotfixes / 3 releases: #7 v1.0.1"


def test_change_failure_with_deployments_counts_incidents(mx: ModuleType) -> None:
    deploys = [deployment("prod", day(n), "SUCCESS") for n in (1, 2, 3, 4)]
    d = data(mx, deployed=True, deployments=deploys, incidents=[issue(9, day(2))], blockers=[issue(7, day(3))])
    m = by_name(mx, d)["Change failure rate"]
    assert m.value == pytest.approx(0.25)
    assert m.detail == "1 incidents / 4 deploys: #9"


def test_time_to_restore_takes_the_healthy_comment_or_the_close_whichever_is_first(mx: ModuleType) -> None:
    incidents = [
        issue(1, day(10), day(5), (day(9), "prod is healthy again on v1.0.1 (200 in 30 ms).")),  # 1 day
        issue(2, day(6), day(3), (day(4), "a person's note")),  # closed: 3 days
        issue(3, day(2)),  # still open
        issue(4, day(50), day(40)),  # restored before the window
    ]
    m = by_name(mx, data(mx, incidents=incidents))["Time to restore"]
    assert m.value == pytest.approx(48.0)
    assert m.extra == {"count": 2, "open": 1}


def test_an_earlier_window_counts_what_was_open_at_its_end(mx: ModuleType) -> None:
    incidents = [
        issue(1, day(12), day(9)),  # restored in the earlier window: 3 days
        issue(2, day(9), day(3)),  # restored after it: open at its end
        issue(3, day(2)),  # opened after it
    ]
    earlier = mx.Window(END - dt.timedelta(days=14), END - dt.timedelta(days=7))
    m = mx.time_to_restore(data(mx, incidents=incidents), earlier)
    assert m.value == pytest.approx(72.0)
    assert m.extra == {"count": 1, "open": 1}


def test_until_reads_a_date_or_a_time_with_its_offset(mx: ModuleType) -> None:
    assert mx.until("2026-09-24", END) == dt.datetime(2026, 9, 24, tzinfo=dt.timezone.utc)  # noqa: UP017
    assert mx.until("2026-09-24T12:00:00+02:00", END) == dt.datetime(2026, 9, 24, 10, tzinfo=dt.timezone.utc)  # noqa: UP017
    assert mx.until("2026-09-24T10:00:00Z", END) == dt.datetime(2026, 9, 24, 10, tzinfo=dt.timezone.utc)  # noqa: UP017


@pytest.mark.parametrize(
    ("text", "error"),
    [("2026-09-24T10:00:00", "needs a time's offset"), ("last week", "must be a date"), ("2026-10-02", "future")],
)
def test_until_refuses_a_naive_malformed_or_future_time(
    mx: ModuleType, capsys: pytest.CaptureFixture[str], text: str, error: str
) -> None:
    with pytest.raises(SystemExit) as refused:
        mx.until(text, END)
    assert refused.value.code == 2
    assert error in capsys.readouterr().err


def test_issue_to_release_runs_to_the_first_released_in_notice(mx: ModuleType) -> None:
    notices = [
        issue(1, day(10), day(8), (day(7), "Released in v1.0.0rc1."), (day(2), "Released in v1.0.0.")),  # 3 days
        issue(2, day(60), day(50), (day(40), "Released in v0.9.0.")),  # told before the window
        issue(3, day(5), None, (day(4), "Not Released in anything")),
    ]
    m = by_name(mx, data(mx, notices=notices))["Issue to release"]
    assert m.value == pytest.approx(72.0)
    assert m.extra["count"] == 1


def test_human_touch_counts_a_person_merging_or_approving(mx: ModuleType) -> None:
    bot = ("Bot", "github-actions")
    pulls = [
        pull(1, day(1)),  # a person merged
        pull(2, day(2), by=bot, approvers=(("User", "bob"),)),  # a bot merged what a person approved
        pull(3, day(3), by=bot, approvers=(("Bot", "copilot"),)),  # no person
        pull(4, day(4), by=("User", "renovate[bot]")),  # a bot login on a user
        pull(5, day(5), by=("User", "release-robot")),  # a machine user named by --bot
        pull(6, day(50)),  # merged before the window
    ]
    d = data(mx, pulls=pulls, bots=frozenset({"release-robot"}))
    m = by_name(mx, d)["Human touch"]
    assert m.value == pytest.approx(2 / 5)
    assert m.extra == {"merges": 5, "merged_by_person": 1, "approved_by_person": 1}
    assert "an agent merging with a person's token counts as the person" in m.detail


def test_agent_share_reads_the_footer_and_trailers_and_the_human_gate(mx: ModuleType) -> None:
    footer = "Summary\n\n🤖 generated with [claude code](https://claude.com/claude-code)"
    trailer = "Fix it\n\nco-authored-by: Claude Opus 5.5 <noreply@anthropic.com>"
    session = "Fix it\n\nCLAUDE-SESSION: https://claude.ai/code/session_x"
    pulls = [
        pull(1, day(1), body=footer, approvers=(("User", "bob"),)),  # agent-made, a person approved
        pull(2, day(2), messages=("Start", trailer)),  # a later commit's trailer
        pull(3, day(3), messages=(session,), approvers=(("Bot", "copilot"),)),  # a bot's approval is no gate
        pull(4, day(4), body="Mentions Co-Authored-By: Claude mid-line", approvers=(("User", "bob"),)),
        pull(5, day(5), messages=("Thanks to Claude-Session: none",)),  # not a trailer at a line's start
        pull(6, day(50), body=footer),  # merged before the window
    ]
    m = by_name(mx, data(mx, pulls=pulls))["Agent share"]
    assert m.value == pytest.approx(3 / 5)
    assert m.extra == {"merges": 5, "agent_made": 3, "approved_by_person": 1}
    assert m.detail == "3 of 5 merges agent-made; a person approved 1 of them in a review"


def test_agent_share_is_zero_without_a_mark_and_no_data_without_merges(mx: ModuleType) -> None:
    assert by_name(mx, data(mx, pulls=[pull(1, day(1))]))["Agent share"].text == "0%"
    assert by_name(mx, data(mx))["Agent share"].value is None


def test_the_markdown_form_is_a_table(mx: ModuleType) -> None:
    w = window(mx)
    text = mx.render("o/r", 30, w, mx.measures(data(mx), w), "markdown")
    assert "| Measure | Value | Detail |" in text
    assert "| Deploy frequency | no data |" in text


# -- fetch, against a fake GraphQL runner ---------------------------------------------------------


def page(nodes: list[Any], cursor: str | None = None, **extra: Any) -> dict[str, Any]:
    return {"pageInfo": {"hasNextPage": cursor is not None, "endCursor": cursor}, "nodes": nodes, **extra}


def ok(repository: dict[str, Any]) -> dict[str, Any]:
    return {"data": {"repository": repository}}


class FakeGitHub:
    """Answers each query the script sends from fixture pages, rejecting pages over limit"""

    def __init__(self, mx: ModuleType, limit: int = 1000) -> None:
        self.mx = mx
        self.limit = limit
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.answers: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {
            mx.RELEASES: self.releases,
            mx.COMPARE: self.compare,
            mx.LINE: self.line,
            mx.PULLS: self.pulls,
            mx.PULL_COMMITS: self.pull_commits,
            mx.DEPLOYMENTS: self.deployments,
            mx.LABELLED: lambda v: ok({"issues": page([])}),
            mx.NOTICES: lambda v: ok({"issues": page([])}),
        }

    def __call__(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        name = next(k for k, v in vars(self.mx).items() if v is query and k.isupper())
        self.calls.append((name, dict(variables)))
        if variables.get("size", 0) > self.limit:
            return {"errors": [{"type": "RESOURCE_LIMITS_EXCEEDED", "message": "too costly"}]}
        return self.answers[query](variables)

    def releases(self, v: dict[str, Any]) -> dict[str, Any]:
        if "cursor" not in v:
            return ok({"releases": page([release("v1.0.1", day(2))], "r1")})
        return ok({"releases": page([release("v1.0.0", day(40))])})

    def compare(self, v: dict[str, Any]) -> dict[str, Any]:
        assert (v["base"], v["head"]) == ("refs/tags/v1.0.0", "refs/tags/v1.0.1")
        return ok({"base": {"compare": {"commits": page([{"oid": "c1", "committedDate": day(3)}])}}})

    def line(self, v: dict[str, Any]) -> dict[str, Any]:
        assert v["branch"] == "refs/heads/release/1.0"
        return ok({"line": {"compare": {"status": "BEHIND"}}})

    def pulls(self, v: dict[str, Any]) -> dict[str, Any]:
        return ok({"pullRequests": page([pull(1, day(3), "c1", first=day(4), more="p1")])})

    def pull_commits(self, v: dict[str, Any]) -> dict[str, Any]:
        assert (v["number"], v["cursor"]) == (1, "p1")
        commit = {"commit": {"authoredDate": day(3), "message": "Last\n\nClaude-Session: https://x"}}
        return ok({"pullRequest": {"commits": page([commit])}})

    def deployments(self, v: dict[str, Any]) -> dict[str, Any]:
        return ok({"deployments": page([], totalCount=0)})


def test_fetch_pages_shrinks_rejected_pages_and_finds_a_hotfix(mx: ModuleType) -> None:
    gh = FakeGitHub(mx, limit=30)
    w = window(mx)
    d = mx.fetch("o/r", w, frozenset(), "incident", "release-blocker", run=gh)
    assert [r.tag for r in d.releases] == ["v1.0.1", "v1.0.0"]  # the second page was read
    assert d.shipped == {"v1.0.1": {"c1"}}
    assert d.hotfixes == {"v1.0.1"}
    assert not d.deployed
    sizes = [v["size"] for name, v in gh.calls if name == "RELEASES"]
    assert sizes == [100, 50, 25, 25]  # halved until accepted, and the smaller size stuck
    found = by_name(mx, d)
    assert found["Lead time for changes"].value == pytest.approx(48.0)
    assert found["Agent share"].extra["agent_made"] == 1  # the trailer was on the PR's second page of commits
    assert found["Change failure rate"].value == pytest.approx(1.0)  # the one release was a hotfix


def test_a_page_rejected_at_the_floor_fails(mx: ModuleType, capsys: pytest.CaptureFixture[str]) -> None:
    gh = FakeGitHub(mx, limit=5)
    with pytest.raises(SystemExit) as exc:
        mx.fetch("o/r", window(mx), frozenset(), "incident", "release-blocker", run=gh)
    assert exc.value.code == 2
    assert "rejected releases even at 10 per page" in capsys.readouterr().err


def test_a_malformed_repo_fails(mx: ModuleType) -> None:
    with pytest.raises(SystemExit):
        mx.fetch("no-slash", window(mx), frozenset(), "incident", "release-blocker", run=FakeGitHub(mx))


class DeployedLongAgo(FakeGitHub):
    """Recent deployments to staging only; production's last one is on the second page"""

    def deployments(self, v: dict[str, Any]) -> dict[str, Any]:
        if "cursor" not in v:
            nodes = [deployment("staging", day(1), "SUCCESS"), deployment("staging", day(40), "SUCCESS")]
            return ok({"deployments": page(nodes, "d1", totalCount=3)})
        return ok({"deployments": page([deployment("production", day(60), "SUCCESS")], totalCount=3)})


def test_an_environment_deployed_only_before_the_window_still_counts_as_deployed(mx: ModuleType) -> None:
    gh = DeployedLongAgo(mx)
    d = mx.fetch("o/r", window(mx), frozenset({"production"}), "incident", "release-blocker", run=gh)
    assert d.deployed
    assert d.deployments == []
    assert by_name(mx, d)["Deploy frequency"].value is None  # no data, not the releases


def backward(nodes: list[Any], cursor: str | None = None) -> dict[str, Any]:
    """A comments page read newest first: cursor leads to older comments"""
    return {"pageInfo": {"hasPreviousPage": cursor is not None, "startCursor": cursor}, "nodes": nodes}


def comment(at: str, body: str) -> dict[str, Any]:
    return {"createdAt": at, "body": body}


class NoticedBefore(FakeGitHub):
    """#1 was told "Released in" before the window and again inside it, its first notice a
    page older than its newest comments; #2's only notice is in the window, a page back"""

    def __init__(self, mx: ModuleType) -> None:
        super().__init__(mx)
        self.answers[mx.NOTICES] = self.notices
        self.answers[mx.NOTICE_COMMENTS] = self.notice_comments

    def notices(self, v: dict[str, Any]) -> dict[str, Any]:
        newest = [comment(day(45), "Thanks"), comment(day(3), "Released in v1.1.0.")]
        told = [comment(day(5), "Released in v2.0.0."), comment(day(4), "Thanks")]
        nodes = [
            {"number": 1, "createdAt": day(90), "comments": backward(newest, "c1")},
            {"number": 2, "createdAt": day(9), "comments": backward(told, "c2")},
        ]
        return ok({"issues": page(nodes)})

    def notice_comments(self, v: dict[str, Any]) -> dict[str, Any]:
        older = {
            "c1": backward([comment(day(80), "Released in v1.0.0.")], "c0"),
            "c2": backward([comment(day(8), "Triage: implement")]),
        }
        return ok({"issue": {"comments": older[v["cursor"]]}})


def test_issue_to_release_fetches_a_notice_older_than_the_window(mx: ModuleType) -> None:
    gh = NoticedBefore(mx)
    d = mx.fetch("o/r", window(mx), frozenset(), "incident", "release-blocker", run=gh)
    paged = [v["cursor"] for name, v in gh.calls if name == "NOTICE_COMMENTS"]
    assert paged == ["c1", "c2"]  # #1 stops at its notice from before the window, not at its first page
    m = by_name(mx, d)["Issue to release"]
    assert (m.value, m.extra["count"]) == (pytest.approx(96.0), 1)  # #2 only: day 9 to day 5
