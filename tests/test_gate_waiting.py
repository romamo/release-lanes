"""Spec 003: the gate records how long a session waits on you and notifies you"""

import datetime as dt
import json
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

import pytest

from shipmill.agents import AgentsConfig
from shipmill.autonomy import Hold
from shipmill.errors import ReleaseError
from shipmill.gate import WAITING, Action, Decision, Session, Waiting, gate, state_dir, tick_lines, tick_record
from shipmill.gitrepo import Git
from shipmill.notify import NO_NOTIFIER, OSASCRIPT, Desktop, NotifyFailed, run

from .test_gate import ISSUES, NOW, FakeClaude, FakeNotifier, bg, on_hold

HOUR = dt.timedelta(hours=1)
REPO = "romamo/demo"


@pytest.fixture
def checkout(tmp_path: Path) -> Git:
    root = tmp_path / "demo"
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    git = Git(root)
    git.run("remote", "add", "origin", f"https://github.com/{REPO}.git")
    return git


def cfg(notify: bool = True, remind_hours: int = 4) -> AgentsConfig:
    return AgentsConfig(prompt="/t", prs=False, retry_hours=24, notify=notify, remind_hours=remind_hours)


def tick(
    git: Git,
    sessions: Sequence[Session],
    notifier: FakeNotifier,
    now: dt.datetime = NOW,
    config: AgentsConfig | None = None,
    dry_run: bool = False,
) -> tuple[Decision, tuple[Waiting, ...]]:
    claude = FakeClaude(list(sessions))
    agents = config or cfg()
    decision, launched, waiting = gate(
        git, REPO, lambda: agents, claude, lambda: [], now, Hold, notifier, dry_run=dry_run
    )
    assert (launched, claude.launched, claude.stopped) == (None, [], [])
    return decision, waiting


def record(git: Git) -> dict[str, dict[str, str | None]]:
    data: dict[str, dict[str, str | None]] = json.loads((state_dir(git) / WAITING).read_text(encoding="utf-8"))
    return data


def write_record(git: Git, text: str) -> Path:
    path = state_dir(git) / WAITING
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def test_s003_3_a_tick_with_no_blocked_session_reads_nothing_and_drops_the_record(checkout: Git) -> None:
    write_record(checkout, json.dumps({"old": {"since": NOW.isoformat(), "notified": None}}))

    def unread() -> AgentsConfig:
        raise AssertionError("the config should not be read")

    notifier = FakeNotifier(fail="no send should run")
    claude = FakeClaude([bg("a", "busy", "working"), bg("c", "idle", "done")])
    decision, launched, waiting = gate(checkout, REPO, unread, claude, lambda: [], NOW, Hold, notifier)
    assert (decision.action, launched, waiting, claude.stopped) == (Action.RUNNING, None, (), [])
    assert not (state_dir(checkout) / WAITING).exists()

    reads = []

    def counted() -> AgentsConfig:
        reads.append(1)
        return cfg()

    claude = FakeClaude([bg("c", "idle", "done")])
    decision, _, waiting = gate(checkout, REPO, counted, claude, lambda: [ISSUES], NOW, Hold, notifier, dry_run=True)
    assert (decision.action, waiting, len(reads), notifier.sent) == (Action.LAUNCH, (), 1, [])


def test_s003_4_the_first_blocked_tick_records_the_wait_and_notifies(checkout: Git) -> None:
    notifier = FakeNotifier()
    decision, waiting = tick(checkout, [bg("b", "idle", "blocked")], notifier)
    assert decision.action is Action.WAITING
    assert notifier.sent == [(f"shipmill {REPO}", "session b waits on you (0h): claude attach b")]
    assert record(checkout) == {"b": {"since": NOW.isoformat(), "notified": NOW.isoformat()}}
    assert [w.line(False) for w in waiting] == ["notified b (waiting 0h)"]


def test_s003_5_a_reminder_waits_for_remind_hours(checkout: Git) -> None:
    notifier = FakeNotifier()
    blocked = [bg("b", "idle", "blocked")]
    tick(checkout, blocked, notifier, config=cfg(remind_hours=4))
    _, waiting = tick(checkout, blocked, notifier, NOW + 3 * HOUR, cfg(remind_hours=4))
    assert len(notifier.sent) == 1
    assert [w.line(False) for w in waiting] == ["b waiting 3h"]
    assert record(checkout)["b"]["notified"] == NOW.isoformat()

    later = NOW + 4 * HOUR + dt.timedelta(minutes=10)
    _, waiting = tick(checkout, blocked, notifier, later, cfg(remind_hours=4))
    assert notifier.sent[1] == (f"shipmill {REPO}", "session b waits on you (4h): claude attach b")
    assert record(checkout) == {"b": {"since": NOW.isoformat(), "notified": later.isoformat()}}
    assert waiting[0].notified and waiting[0].waited_hours == 4


def test_s003_6_a_failed_send_is_reported_and_retried_next_tick(checkout: Git) -> None:
    blocked = [bg("b", "idle", "blocked")]
    decision, waiting = tick(checkout, blocked, FakeNotifier(fail="notify-send exited 1"))
    assert decision.action is Action.WAITING
    assert [w.line(False) for w in waiting] == ["notify failed for b: notify-send exited 1"]
    assert (waiting[0].notified, waiting[0].error) == (False, "notify-send exited 1")
    assert record(checkout)["b"]["notified"] is None

    notifier = FakeNotifier()
    tick(checkout, blocked, notifier, NOW + dt.timedelta(minutes=5))
    assert len(notifier.sent) == 1
    assert record(checkout)["b"] == {"since": NOW.isoformat(), "notified": (NOW + dt.timedelta(minutes=5)).isoformat()}


def test_s003_6_a_send_fails_on_a_missing_command_an_exit_or_a_timeout(tmp_path: Path) -> None:
    with pytest.raises(NotifyFailed, match="not found"):
        run([str(tmp_path / "no-such-notifier"), "t", "b"], 5)
    with pytest.raises(NotifyFailed, match="exited 3: nope"):
        run([sys.executable, "-c", "import sys; sys.stderr.write('nope'); sys.exit(3)"], 5)
    with pytest.raises(NotifyFailed, match="ran longer than 0.2s"):
        run([sys.executable, "-c", "import time; time.sleep(5)"], 0.2)
    run([sys.executable, "-c", "pass"], 5)


def test_s003_6_the_send_timeout_is_30_seconds() -> None:
    calls: list[tuple[list[str], float]] = []
    Desktop.detect("linux", lambda _: "/usr/bin/notify-send", lambda cmd, t: calls.append((list(cmd), t))).send(
        "t", "b"
    )
    assert calls == [(["notify-send", "t", "b"], 30.0)]


def test_s003_7_notify_false_records_the_wait_and_sends_nothing(checkout: Git) -> None:
    notifier = FakeNotifier(fail="no send should run")
    blocked = [bg("b", "idle", "blocked")]
    for hours in (0, 5, 30):
        _, waiting = tick(checkout, blocked, notifier, NOW + hours * HOUR, cfg(notify=False))
        assert (waiting[0].notified, waiting[0].error) == (False, None)
    assert record(checkout) == {"b": {"since": NOW.isoformat(), "notified": None}}


def test_s003_8_macos_runs_osascript_with_the_text_as_arguments() -> None:
    calls: list[list[str]] = []
    notifier = Desktop.detect("darwin", lambda _: "/usr/bin/notify-send", lambda cmd, t: calls.append(list(cmd)))
    title, body = 'shipmill "x" & do shell script "rm"', "session a'b waits"
    notifier.send(title, body)
    assert calls == [[*OSASCRIPT, title, body]]
    assert calls[0][1:7] == [
        "-e",
        "on run argv",
        "-e",
        "display notification (item 2 of argv) with title (item 1 of argv)",
        "-e",
        "end run",
    ]


def test_s003_8_elsewhere_runs_notify_send_or_fails_without_a_notifier() -> None:
    calls: list[list[str]] = []
    found = Desktop.detect("linux", lambda name: f"/usr/bin/{name}", lambda cmd, t: calls.append(list(cmd)))
    found.send("shipmill romamo/demo", "session a waits on you (0h): claude attach a")
    assert calls == [["notify-send", "shipmill romamo/demo", "session a waits on you (0h): claude attach a"]]

    none = Desktop.detect("linux", lambda _: None, lambda cmd, t: calls.append(list(cmd)))
    with pytest.raises(NotifyFailed, match=r"^no notifier \(osascript or notify-send\)$"):
        none.send("t", "b")
    assert len(calls) == 1 and NO_NOTIFIER == "no notifier (osascript or notify-send)"


def test_s003_9_an_entry_is_dropped_once_its_session_is_no_longer_blocked(checkout: Git) -> None:
    notifier = FakeNotifier()
    tick(checkout, [bg("a", "idle", "blocked"), bg("b", "idle", "blocked")], notifier)
    later = NOW + HOUR
    tick(checkout, [bg("a", "idle", "blocked"), bg("b", "busy", "working")], notifier, later)
    assert set(record(checkout)) == {"a"}

    again = NOW + 2 * HOUR
    _, waiting = tick(checkout, [bg("a", "idle", "blocked"), bg("b", "idle", "blocked")], notifier, again)
    assert record(checkout)["b"] == {"since": again.isoformat(), "notified": again.isoformat()}
    assert [body for _, body in notifier.sent].count("session b waits on you (0h): claude attach b") == 2
    assert [w.session for w in waiting if w.notified] == ["b"]

    tick(checkout, [bg("a", "idle", "done")], notifier, again)
    assert not (state_dir(checkout) / WAITING).exists()


@pytest.mark.parametrize(
    "text",
    [
        "not json",
        "[]",
        '{"b": {"since": "2026-10-05T19:00:00+00:00"}}',
        '{"b": {"since": "2026-10-05T19:00:00+00:00", "notified": null, "extra": 1}}',
        '{"b": {"since": "yesterday", "notified": null}}',
        '{"b": {"since": "2026-10-05T19:00:00", "notified": null}}',
        '{"b": {"since": "2026-10-05T19:00:00+00:00", "notified": 5}}',
        '{"b": "2026-10-05T19:00:00+00:00"}',
    ],
)
def test_s003_9_a_malformed_record_fails_naming_its_path(checkout: Git, text: str) -> None:
    path = write_record(checkout, text)
    with pytest.raises(ReleaseError, match=f"{path} is malformed .*delete it"):
        tick(checkout, [bg("b", "idle", "blocked")], FakeNotifier())


def test_s003_14_a_dry_run_sends_nothing_and_writes_no_record(checkout: Git) -> None:
    notifier = FakeNotifier(fail="no send should run")
    decision, waiting = tick(checkout, [bg("b", "idle", "blocked")], notifier, dry_run=True)
    assert decision.action is Action.WAITING
    assert not (state_dir(checkout) / WAITING).exists()
    lines = tick_lines(decision, None, waiting, dry_run=True)
    assert lines[1] == "  would notify b (waiting 0h)"
    assert tick_record(decision, None, waiting, dry_run=True)["waiting"] == [
        {
            "session": "b",
            "name": "shipmill romamo/demo b",
            "since": NOW.isoformat(),
            "waited_hours": 0,
            "notified": True,
            "stopped": False,
            "error": None,
        }
    ]

    path = write_record(checkout, json.dumps({"b": {"since": NOW.isoformat(), "notified": None}}))
    before = path.read_text(encoding="utf-8")
    tick(checkout, [], notifier, dry_run=True)
    tick(checkout, [bg("b", "idle", "blocked")], notifier, NOW + HOUR, dry_run=True)
    assert path.read_text(encoding="utf-8") == before


def test_s003_15_json_lists_each_blocked_session(checkout: Git) -> None:
    notifier = FakeNotifier()
    tick(checkout, [bg("a", "idle", "blocked")], notifier)
    sessions = [bg("a", "idle", "blocked"), bg("b", "idle", "blocked"), bg("c", "busy", "working")]
    decision, waiting = tick(
        checkout, sessions, FakeNotifier(fail="boom"), NOW + 2 * HOUR + 59 * dt.timedelta(minutes=1)
    )
    data = json.loads(json.dumps(tick_record(decision, None, waiting, dry_run=False)))
    assert data["action"] == "WAITING"
    assert data["waiting"] == [
        {
            "session": "a",
            "name": "shipmill romamo/demo a",
            "since": NOW.isoformat(),
            "waited_hours": 2,
            "notified": False,
            "stopped": False,
            "error": None,
        },
        {
            "session": "b",
            "name": "shipmill romamo/demo b",
            "since": (NOW + 2 * HOUR + 59 * dt.timedelta(minutes=1)).isoformat(),
            "waited_hours": 0,
            "notified": False,
            "stopped": False,
            "error": "boom",
        },
    ]
    quiet, _ = tick(checkout, [], notifier)
    assert tick_record(quiet, None, (), dry_run=False)["waiting"] == []


def test_a_held_tick_still_notifies(checkout: Git) -> None:
    """D-13: a hold stops the gate starting sessions, not the waiting step"""
    notifier = FakeNotifier()
    claude = FakeClaude([bg("b", "idle", "blocked")])
    decision, _, waiting = gate(checkout, REPO, cfg, claude, lambda: [ISSUES], NOW, on_hold, notifier)
    assert decision.action is Action.HELD
    assert len(notifier.sent) == 1 and waiting[0].notified
    assert tick_lines(decision, None, waiting, dry_run=False)[1] == "  notified b (waiting 0h)"
