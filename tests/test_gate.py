"""The gate decides with code whether a repo needs a Claude Code session"""

import datetime as dt
import json
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from shipyard.errors import ReleaseError
from shipyard.gate import (
    RECORD,
    Action,
    Finding,
    Launch,
    Session,
    decide,
    fingerprint,
    gate,
    parse_findings,
    parse_launched,
    parse_sessions,
    prompt,
    skills_dir,
    state_dir,
)
from shipyard.gitrepo import Git

NOW = dt.datetime(2026, 10, 4, 12, 0, tzinfo=dt.UTC)
DAY = dt.timedelta(hours=24)
ISSUES = Finding("ISSUES", "romamo/demo", "NEW #12 #14; NEEDS_PR #9")
QUIET_ROWS = [Finding("BOT_OK", "release.yml", ""), Finding("PRS_OPEN", "romamo/demo", "#3")]


def bg(id: str, status: str | None, state: str | None) -> Session:
    return Session(id, f"shipyard romamo/demo {id}", status, state)


@dataclass
class FakeClaude:
    listed: list[Session] = field(default_factory=list)
    launched: list[tuple[str, str]] = field(default_factory=list)
    stopped: list[str] = field(default_factory=list)

    def sessions(self, workspace: Path, repo: str) -> list[Session]:
        return list(self.listed)

    def launch(self, workspace: Path, name: str, text: str) -> str:
        self.launched.append((name, text))
        return f"s{len(self.launched)}"

    def stop(self, session: str) -> None:
        self.stopped.append(session)


@pytest.fixture
def checkout(tmp_path: Path) -> Git:
    root = tmp_path / "demo"
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    git = Git(root)
    git.run("remote", "add", "origin", "https://github.com/romamo/demo.git")
    return git


def test_nothing_to_do_is_quiet() -> None:
    assert decide(QUIET_ROWS, [], None, NOW, DAY).action is Action.QUIET


def test_findings_with_no_session_launch() -> None:
    decision = decide([*QUIET_ROWS, ISSUES], [], None, NOW, DAY)
    assert (decision.action, decision.work) == (Action.LAUNCH, (ISSUES,))


def test_a_working_session_holds_the_repo() -> None:
    decision = decide([ISSUES], [bg("a", "busy", "working")], None, NOW, DAY)
    assert decision.action is Action.RUNNING and "claude attach a" in decision.reason


def test_a_session_waiting_on_the_user_holds_the_repo() -> None:
    sessions = [bg("a", "idle", "done"), bg("b", "idle", "blocked")]
    decision = decide([ISSUES], sessions, None, NOW, DAY)
    assert decision.action is Action.WAITING and "claude attach b" in decision.reason


def test_finished_sessions_are_stopped_before_a_launch() -> None:
    sessions = [bg("a", "idle", "done"), bg("b", "idle", "failed")]
    decision = decide([ISSUES], sessions, None, NOW, DAY)
    assert (decision.action, decision.stop) == (Action.LAUNCH, ("a", "b"))


def test_unchanged_findings_wait_for_the_retry_window() -> None:
    last = Launch(fingerprint([ISSUES]), "s1", NOW - dt.timedelta(hours=3))
    assert decide([ISSUES], [], last, NOW, DAY).action is Action.UNCHANGED
    assert decide([ISSUES], [], last, NOW + DAY, DAY).action is Action.LAUNCH


def test_changed_findings_launch_at_once() -> None:
    last = Launch(fingerprint([ISSUES]), "s1", NOW - dt.timedelta(minutes=15))
    newer = Finding("ISSUES", "romamo/demo", "NEW #12 #14 #15; NEEDS_PR #9")
    assert decide([newer], [], last, NOW, DAY).action is Action.LAUNCH


def test_open_prs_count_only_when_asked() -> None:
    assert decide(QUIET_ROWS, [], None, NOW, DAY, frozenset({"PRS_OPEN"})).action is Action.LAUNCH


def test_the_fingerprint_ignores_order() -> None:
    other = Finding("UNANNOUNCED", "v1.2.0", "#4 (since v1.1.0)")
    assert fingerprint([ISSUES, other]) == fingerprint([other, ISSUES])


def test_the_prompt_carries_the_findings() -> None:
    text = prompt("/github-issue-triage {repo} merge when green", "romamo/demo", [ISSUES], NOW)
    assert text.startswith("/github-issue-triage romamo/demo merge when green\n")
    assert "- ISSUES romamo/demo: NEW #12 #14; NEEDS_PR #9" in text


def test_only_this_repos_background_sessions_count() -> None:
    rows = [
        {"id": "a", "kind": "background", "name": "shipyard romamo/demo 2026-10-04 12:00", "status": "busy"},
        {"id": "b", "kind": "background", "name": "shipyard romamo/demo-two 2026-10-04", "state": "working"},
        {"id": "c", "kind": "interactive", "name": "shipyard romamo/demo", "status": "busy"},
        {"id": "d", "kind": "background", "name": "Triage #6", "state": "done"},
    ]
    assert [s.id for s in parse_sessions(json.dumps(rows), "romamo/demo")] == ["a"]


def test_launch_output_gives_the_session_id() -> None:
    out = "Started.\n  claude attach c2277175    open in this terminal\n  claude logs c2277175\n"
    assert parse_launched(out) == "c2277175"
    with pytest.raises(ReleaseError, match="no session id"):
        parse_launched("Workspace not trusted.")


def test_watch_rows_parse() -> None:
    line = json.dumps({"detail": "#3", "state": "PRS_OPEN", "subject": "romamo/demo"})
    assert parse_findings(line) == [Finding("PRS_OPEN", "romamo/demo", "#3")]


def test_the_watch_script_is_found() -> None:
    assert (skills_dir() / "github-ship-watch" / "scripts" / "watch_state.py").is_file()


def test_a_launch_is_recorded_and_not_repeated(checkout: Git) -> None:
    claude = FakeClaude([bg("old", "idle", "done")])
    decision, launched = gate(checkout, "romamo/demo", "/triage {repo}", claude, lambda: [ISSUES], NOW, DAY)
    assert (decision.action, launched, claude.stopped) == (Action.LAUNCH, "s1", ["old"])
    name, text = claude.launched[0]
    assert name == "shipyard romamo/demo 2026-10-04 12:00" and text.startswith("/triage romamo/demo")
    assert json.loads((state_dir(checkout) / RECORD).read_text())["session"] == "s1"

    later = NOW + dt.timedelta(minutes=15)
    claude.listed = [bg("s1", "idle", "done")]
    decision, launched = gate(checkout, "romamo/demo", "/triage {repo}", claude, lambda: [ISSUES], later, DAY)
    assert (decision.action, launched, len(claude.launched)) == (Action.UNCHANGED, None, 1)


def test_a_busy_session_skips_the_state_read(checkout: Git) -> None:
    def unread() -> list[Finding]:
        raise AssertionError("the state read should not run")

    claude = FakeClaude([bg("a", "busy", "working")])
    decision, _ = gate(checkout, "romamo/demo", "/triage {repo}", claude, unread, NOW, DAY)
    assert decision.action is Action.RUNNING


def test_a_dry_run_launches_nothing(checkout: Git) -> None:
    claude = FakeClaude([bg("old", "idle", "done")])
    decision, launched = gate(checkout, "romamo/demo", "/t", claude, lambda: [ISSUES], NOW, DAY, dry_run=True)
    assert (decision.action, launched, claude.launched, claude.stopped) == (Action.LAUNCH, None, [], [])
    assert not (state_dir(checkout) / RECORD).exists()


def test_a_checkout_of_another_repo_is_refused(checkout: Git) -> None:
    with pytest.raises(ReleaseError, match="not romamo/other"):
        gate(checkout, "romamo/other", "/t", FakeClaude(), lambda: [], NOW, DAY)


def test_a_malformed_record_fails(checkout: Git) -> None:
    record = state_dir(checkout) / RECORD
    record.parent.mkdir(parents=True)
    record.write_text("{}", encoding="utf-8")
    with pytest.raises(ReleaseError, match="malformed"):
        gate(checkout, "romamo/demo", "/t", FakeClaude(), lambda: [ISSUES], NOW, DAY)
