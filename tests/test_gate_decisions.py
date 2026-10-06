"""Spec 005's notifications: a headless gate without an App notifies for each item that
waits on a needs-decision reply, and records the wait in needs-decision.json"""

import datetime as dt
import json
import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest

from shipmill.agents import AgentsConfig, Mode
from shipmill.app import AppCheck
from shipmill.autonomy import Hold
from shipmill.errors import ReleaseError
from shipmill.gate import (
    DECISIONS,
    WAITING,
    Action,
    Decision,
    Finding,
    Launch,
    Process,
    gate,
    save_launch,
    state_dir,
    tick_lines,
    tick_record,
)
from shipmill.gitrepo import Git

from .test_app import APP_ID, REPO
from .test_gate import ISSUES, NO_PRUNE, NOW, FakeClaude, FakeNotifier, bg, on_hold
from .test_gate_headless import LOGIN, PID, SESSION, STARTED, app_check_for, headless, interactive, login, same

HOUR = dt.timedelta(hours=1)
TITLE = f"shipmill {REPO}"


def waits(*items: int) -> Finding:
    """watch_state.py's NEEDS_DECISION row (S-005-9)"""
    return Finding("NEEDS_DECISION", REPO, " ".join(f"#{n}" for n in items), False)


def body(item: int) -> str:
    return f"#{item} waits on your decision: https://github.com/{REPO}/issues/{item}"


@pytest.fixture
def checkout(tmp_path: Path) -> Git:
    root = tmp_path / "demo"
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    repo = Git(root)
    repo.run("remote", "add", "origin", "https://github.com/romamo/demo.git")
    return repo


def tick(
    git: Git,
    agents: AgentsConfig,
    rows: list[Finding],
    notifier: FakeNotifier,
    now: dt.datetime = NOW,
    dry_run: bool = False,
    claude: FakeClaude | None = None,
    hold: Callable[[], Hold] = Hold,
    app: AppCheck | None = None,
) -> Decision:
    decision, *_ = gate(
        git,
        REPO,
        lambda: agents,
        claude or FakeClaude(),
        lambda read: list(rows),
        now,
        hold,
        notifier,
        NO_PRUNE,
        dry_run=dry_run,
        login=login,
        started=same(STARTED),
        app=app,
        new_session=lambda: SESSION,
    )
    return decision


def record_of(git: Git) -> dict[str, object]:
    data: dict[str, object] = json.loads((state_dir(git) / DECISIONS).read_text(encoding="utf-8"))
    return data


def quiet(notify: bool = True, remind_hours: int = 4) -> AgentsConfig:
    return AgentsConfig(
        prompt="/t", prs=False, retry_hours=24, mode=Mode.HEADLESS, notify=notify, remind_hours=remind_hours
    )


# S-005-12


def test_s005_12_a_newly_waiting_item_is_notified_once_and_recorded(checkout: Git) -> None:
    notifier = FakeNotifier()
    decision = tick(checkout, quiet(), [waits(7, 3)], notifier)
    assert decision.action is Action.QUIET  # a waiting item is no work
    assert notifier.sent == [(TITLE, body(3)), (TITLE, body(7))]
    assert record_of(checkout) == {
        "3": {"since": NOW.isoformat(), "notified": NOW.isoformat()},
        "7": {"since": NOW.isoformat(), "notified": NOW.isoformat()},
    }
    assert tick_lines(decision, None, (), (), dry_run=False)[1:] == [
        "  notified #3 (waiting 0h)",
        "  notified #7 (waiting 0h)",
    ]


def test_s005_12_it_repeats_only_after_remind_hours(checkout: Git) -> None:
    notifier = FakeNotifier()
    tick(checkout, quiet(remind_hours=2), [waits(7)], notifier)
    later = tick(checkout, quiet(remind_hours=2), [waits(7)], notifier, now=NOW + 2 * HOUR - dt.timedelta(minutes=1))
    assert len(notifier.sent) == 1
    assert [(a.item, a.notified, a.waited_hours) for a in later.decisions] == [(7, False, 1)]
    assert tick_lines(later, None, (), (), dry_run=False)[1:] == []  # an unnotified wait prints no line
    again = tick(checkout, quiet(remind_hours=2), [waits(7)], notifier, now=NOW + 2 * HOUR)
    assert notifier.sent == [(TITLE, body(7))] * 2
    assert tick_lines(again, None, (), (), dry_run=False)[1:] == ["  notified #7 (waiting 2h)"]
    assert record_of(checkout) == {"7": {"since": NOW.isoformat(), "notified": (NOW + 2 * HOUR).isoformat()}}


def test_s005_12_a_new_item_beside_a_notified_one_is_notified_alone(checkout: Git) -> None:
    notifier = FakeNotifier()
    tick(checkout, quiet(), [waits(7)], notifier)
    tick(checkout, quiet(), [waits(7, 9)], notifier, now=NOW + HOUR)
    assert notifier.sent == [(TITLE, body(7)), (TITLE, body(9))]


def test_s005_12_with_notify_false_it_sends_none_and_still_records(checkout: Git) -> None:
    notifier = FakeNotifier()
    decision = tick(checkout, quiet(notify=False), [waits(7)], notifier)
    assert notifier.sent == []
    assert record_of(checkout) == {"7": {"since": NOW.isoformat(), "notified": None}}
    assert [(a.item, a.notified, a.error) for a in decision.decisions] == [(7, False, None)]


def test_s005_12_a_launching_tick_notifies_too(checkout: Git) -> None:
    notifier = FakeNotifier()
    decision = tick(checkout, quiet(), [ISSUES, waits(7)], notifier)
    assert decision.action is Action.LAUNCH
    assert notifier.sent == [(TITLE, body(7))]


@pytest.mark.parametrize("case", ["held", "waiting", "running"])
def test_s005_12_a_tick_that_reads_no_state_neither_notifies_nor_touches_the_file(checkout: Git, case: str) -> None:
    path = state_dir(checkout) / DECISIONS
    path.parent.mkdir(parents=True)
    path.write_text("not json", encoding="utf-8")  # never read: it would exit 2
    claude = FakeClaude([bg("b", "waiting", "blocked")] if case == "waiting" else [])
    if case == "running":
        save_launch(state_dir(checkout) / "gate.json", Launch("f", str(SESSION), NOW, Process(PID, STARTED)))
    notifier = FakeNotifier()
    agents = AgentsConfig(prompt="/t", prs=False, retry_hours=24, mode=Mode.HEADLESS, notify=True)
    decision = tick(checkout, agents, [waits(7)], notifier, claude=claude, hold=on_hold if case == "held" else Hold)
    expected = {"held": Action.HELD, "waiting": Action.WAITING, "running": Action.RUNNING}[case]
    assert decision.action is expected
    assert [body for _, body in notifier.sent if body.startswith("#")] == []
    assert path.read_text(encoding="utf-8") == "not json"
    assert decision.decisions == () and tick_record(decision, None, (), (), False)["decisions"] == []


def test_s005_12_interactive_mode_neither_notifies_nor_writes_the_file(checkout: Git) -> None:
    notifier = FakeNotifier()
    decision = tick(checkout, interactive(), [waits(7)], notifier)
    assert decision.action is Action.QUIET
    assert notifier.sent == [] and decision.decisions == ()
    assert not (state_dir(checkout) / DECISIONS).exists()


# S-005-13


def test_s005_13_an_item_that_no_longer_waits_is_dropped(checkout: Git) -> None:
    notifier = FakeNotifier()
    tick(checkout, quiet(), [waits(3, 7)], notifier)
    tick(checkout, quiet(), [waits(7)], notifier, now=NOW + HOUR)
    assert record_of(checkout) == {"7": {"since": NOW.isoformat(), "notified": NOW.isoformat()}}


def test_s005_13_the_file_goes_once_nothing_waits(checkout: Git) -> None:
    tick(checkout, quiet(), [waits(7)], FakeNotifier())
    decision = tick(checkout, quiet(), [], FakeNotifier(), now=NOW + HOUR)
    assert not (state_dir(checkout) / DECISIONS).exists()
    assert tick_record(decision, None, (), (), False)["decisions"] == []


def test_s005_13_a_failed_send_is_printed_and_leaves_notified_unchanged(checkout: Git) -> None:
    decision = tick(checkout, quiet(), [waits(7)], FakeNotifier(fail="osascript exited 1"))
    assert record_of(checkout) == {"7": {"since": NOW.isoformat(), "notified": None}}
    assert tick_lines(decision, None, (), (), dry_run=False)[1:] == ["  notify failed for #7: osascript exited 1"]
    notifier = FakeNotifier()
    tick(checkout, quiet(), [waits(7)], notifier, now=NOW + HOUR)  # tried again on the next tick
    assert notifier.sent == [(TITLE, body(7))]
    assert record_of(checkout) == {"7": {"since": NOW.isoformat(), "notified": (NOW + HOUR).isoformat()}}


@pytest.mark.parametrize(
    "text",
    [
        "not json",
        "[]",
        '{"7": {"since": "2026-10-04T12:00:00+00:00"}}',
        '{"7": {"since": "2026-10-04T12:00:00", "notified": null}}',
        '{"7": {"since": 1, "notified": null}}',
        '{"7": "waiting"}',
        '{"x7": {"since": "2026-10-04T12:00:00+00:00", "notified": null}}',
        '{"07": {"since": "2026-10-04T12:00:00+00:00", "notified": null}}',
        '{"-7": {"since": "2026-10-04T12:00:00+00:00", "notified": null}}',
    ],
)
def test_s005_13_a_malformed_file_exits_2_naming_its_path(checkout: Git, text: str) -> None:
    path = state_dir(checkout) / DECISIONS
    path.parent.mkdir(parents=True)
    path.write_text(text, encoding="utf-8")
    claude = FakeClaude()
    with pytest.raises(ReleaseError, match="is malformed") as caught:
        tick(checkout, quiet(), [ISSUES, waits(7)], FakeNotifier(), claude=claude)
    assert str(path) in str(caught.value)
    assert claude.started == []  # the tick launches nothing


def test_s005_13_a_dry_run_sends_nothing_writes_nothing_and_says_it_would_notify(checkout: Git) -> None:
    notifier = FakeNotifier()
    decision = tick(checkout, quiet(), [waits(7)], notifier, dry_run=True)
    assert notifier.sent == []
    assert not (state_dir(checkout) / DECISIONS).exists()
    assert tick_lines(decision, None, (), (), dry_run=True)[1:] == ["  would notify #7 (waiting 0h)"]


def test_s005_13_a_dry_run_leaves_a_file_with_nothing_waiting(checkout: Git) -> None:
    tick(checkout, quiet(), [waits(7)], FakeNotifier())
    tick(checkout, quiet(), [], FakeNotifier(), now=NOW + HOUR, dry_run=True)
    assert record_of(checkout) == {"7": {"since": NOW.isoformat(), "notified": NOW.isoformat()}}


def test_s005_13_a_watch_row_that_lists_anything_but_numbers_exits_2(checkout: Git) -> None:
    row = Finding("NEEDS_DECISION", REPO, "#7 title", False)
    with pytest.raises(ReleaseError, match="NEEDS_DECISION row lists 'title', not #N"):
        tick(checkout, quiet(), [row], FakeNotifier())


# S-005-14


def test_s005_14_with_app_id_no_desktop_notification_for_an_item(checkout: Git, tmp_path: Path) -> None:
    notifier = FakeNotifier()
    decision = tick(checkout, headless(APP_ID), [waits(7)], notifier, app=app_check_for(tmp_path))
    assert decision.action is Action.QUIET
    assert notifier.sent == []
    assert [(a.item, a.notified, a.error) for a in decision.decisions] == [(7, False, None)]
    assert tick_lines(decision, None, (), (), dry_run=False) == ["QUIET: nothing needs an agent"]


@pytest.mark.parametrize("dry_run", [False, True])
def test_s005_14_a_headless_launch_without_app_id_warns_github_wont_notify(checkout: Git, dry_run: bool) -> None:
    decision = tick(checkout, quiet(), [ISSUES], FakeNotifier(), dry_run=dry_run)
    assert decision.action is Action.LAUNCH
    lines = tick_lines(decision, None, (), (), dry_run=dry_run)
    assert lines[1] == f"  no app_id: needs-decision comments post as {LOGIN}, so GitHub won't notify you"


def test_s005_14_an_interactive_launch_prints_no_such_warning(checkout: Git) -> None:
    decision = tick(checkout, interactive(), [ISSUES], FakeNotifier())
    assert decision.action is Action.LAUNCH and decision.asks_as is None
    assert not any("no app_id" in line for line in tick_lines(decision, "s1", (), (), dry_run=False))


# S-005-15


def test_s005_15_json_reports_mode_and_each_waiting_items_decision(checkout: Git) -> None:
    tick(checkout, quiet(remind_hours=8), [waits(3)], FakeNotifier())
    notifier = FakeNotifier(fail="no notifier (osascript or notify-send)")
    later = NOW + 5 * HOUR + dt.timedelta(minutes=59)  # whole hours: 5
    decision = tick(checkout, quiet(remind_hours=8), [waits(3, 7)], notifier, now=later)
    record = json.loads(json.dumps(tick_record(decision, None, (), (), dry_run=False)))
    assert record["mode"] == "headless"
    assert record["decisions"] == [
        {"item": 3, "since": NOW.isoformat(), "waited_hours": 5, "notified": False, "error": None},
        {
            "item": 7,
            "since": later.isoformat(),
            "waited_hours": 0,
            "notified": False,
            "error": "no notifier (osascript or notify-send)",
        },
    ]


def test_s005_15_json_reports_an_empty_list_when_nothing_waits(checkout: Git) -> None:
    decision = tick(checkout, quiet(), [ISSUES], FakeNotifier())
    record = tick_record(decision, None, (), (), dry_run=False)
    assert (record["mode"], record["decisions"]) == ("headless", [])


def test_s005_15_interactive_json_reports_its_mode(checkout: Git) -> None:
    decision = tick(checkout, interactive(), [waits(7)], FakeNotifier())
    record = tick_record(decision, None, (), (), dry_run=False)
    assert (record["mode"], record["decisions"]) == ("interactive", [])


def test_s005_15_a_tick_that_reads_no_config_reports_no_mode(checkout: Git) -> None:
    decision = tick(checkout, quiet(), [waits(7)], FakeNotifier(), hold=on_hold)
    assert decision.action is Action.HELD
    assert tick_record(decision, None, (), (), dry_run=False)["mode"] is None


def test_s005_15_the_session_waiting_step_keeps_its_file_and_output(checkout: Git) -> None:
    claude = FakeClaude([bg("b", "waiting", "blocked")])
    notifier = FakeNotifier()
    decision, _, waiting, _ = gate(
        checkout, REPO, quiet, claude, lambda read: [waits(7)], NOW, Hold, notifier, NO_PRUNE, login=login
    )
    assert decision.action is Action.WAITING
    assert [w.session for w in waiting] == ["b"] and notifier.sent == [
        (TITLE, "session b waits on you (0m): claude attach b")
    ]
    assert (state_dir(checkout) / WAITING).is_file() and not (state_dir(checkout) / DECISIONS).exists()
