"""The gate decides with code whether a repo needs a Claude Code session"""

import datetime as dt
import json
import subprocess
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from shipmill.agents import AgentsConfig
from shipmill.errors import ReleaseError
from shipmill.gate import (
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
    refresh,
    skills_dir,
    state_dir,
)
from shipmill.gitrepo import Git
from shipmill.policy import Policy

from .conftest import POLICY

NOW = dt.datetime(2026, 10, 4, 12, 0, tzinfo=dt.UTC)
DAY = dt.timedelta(hours=24)
ISSUES = Finding("ISSUES", "romamo/demo", "NEW #12 #14; NEEDS_PR #9")
QUIET_ROWS = [Finding("BOT_OK", "release.yml", ""), Finding("PRS_OPEN", "romamo/demo", "#3")]


def cfg(text: str = "/t", prs: bool = False) -> AgentsConfig:
    return AgentsConfig(prompt=text, prs=prs, retry_hours=24)


def bg(id: str, status: str | None, state: str | None) -> Session:
    return Session(id, f"shipmill romamo/demo {id}", status, state)


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
        {"id": "a", "kind": "background", "name": "shipmill romamo/demo 2026-10-04 12:00", "status": "busy"},
        {"id": "b", "kind": "background", "name": "shipmill romamo/demo-two 2026-10-04", "state": "working"},
        {"id": "c", "kind": "interactive", "name": "shipmill romamo/demo", "status": "busy"},
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
    decision, launched = gate(checkout, "romamo/demo", lambda: cfg("/triage {repo}"), claude, lambda: [ISSUES], NOW)
    assert (decision.action, launched, claude.stopped) == (Action.LAUNCH, "s1", ["old"])
    name, text = claude.launched[0]
    assert name == "shipmill romamo/demo 2026-10-04 12:00" and text.startswith("/triage romamo/demo")
    assert json.loads((state_dir(checkout) / RECORD).read_text())["session"] == "s1"

    later = NOW + dt.timedelta(minutes=15)
    claude.listed = [bg("s1", "idle", "done")]
    decision, launched = gate(checkout, "romamo/demo", lambda: cfg("/triage {repo}"), claude, lambda: [ISSUES], later)
    assert (decision.action, launched, len(claude.launched)) == (Action.UNCHANGED, None, 1)


def test_a_busy_session_skips_the_state_read(checkout: Git) -> None:
    def unread() -> list[Finding]:
        raise AssertionError("the state read should not run")

    claude = FakeClaude([bg("a", "busy", "working")])
    decision, _ = gate(checkout, "romamo/demo", lambda: cfg("/triage {repo}"), claude, unread, NOW)
    assert decision.action is Action.RUNNING


def test_a_dry_run_launches_nothing(checkout: Git) -> None:
    claude = FakeClaude([bg("old", "idle", "done")])
    decision, launched = gate(checkout, "romamo/demo", lambda: cfg("/t"), claude, lambda: [ISSUES], NOW, dry_run=True)
    assert (decision.action, launched, claude.launched, claude.stopped) == (Action.LAUNCH, None, [], [])
    assert not (state_dir(checkout) / RECORD).exists()


def test_a_checkout_of_another_repo_is_refused(checkout: Git) -> None:
    with pytest.raises(ReleaseError, match="not romamo/other"):
        gate(checkout, "romamo/other", lambda: cfg("/t"), FakeClaude(), lambda: [], NOW)


def test_a_malformed_record_fails(checkout: Git) -> None:
    record = state_dir(checkout) / RECORD
    record.parent.mkdir(parents=True)
    record.write_text("{}", encoding="utf-8")
    with pytest.raises(ReleaseError, match="malformed"):
        gate(checkout, "romamo/demo", lambda: cfg("/t"), FakeClaude(), lambda: [ISSUES], NOW)


def test_prs_in_the_config_make_open_prs_work(checkout: Git) -> None:
    rows = [Finding("PRS_OPEN", "romamo/demo", "#3")]
    decision, _ = gate(checkout, "romamo/demo", lambda: cfg(), FakeClaude(), lambda: rows, NOW, dry_run=True)
    assert decision.action is Action.QUIET
    decision, _ = gate(checkout, "romamo/demo", lambda: cfg(prs=True), FakeClaude(), lambda: rows, NOW, dry_run=True)
    assert decision.action is Action.LAUNCH


def write_config(root: Path, text: str) -> None:
    (root / ".github").mkdir(exist_ok=True)
    (root / ".github" / "shipmill.toml").write_text(text, encoding="utf-8")


def test_the_agents_section_loads_without_release_keys(tmp_path: Path) -> None:
    write_config(tmp_path, '[agents]\nprompt = "/github-issue-triage {repo} merge when green"\nprs = true\n')
    assert AgentsConfig.load(tmp_path) == AgentsConfig("/github-issue-triage {repo} merge when green", True, 24)


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ('name = "demo"\n', "no \\[agents\\] section"),
        ('[agents]\nprompt = "  "\n', "prompt must not be empty"),
        ('[agents]\nprompt = "/t"\nretry_hours = 0\n', "retry_hours must be in 1..168"),
        ('[agents]\nprompt = "/t"\nmerge = true\n', "unknown keys \\['merge'\\]"),
        ("[agents]\nprs = true\n", "prompt is required"),
    ],
)
def test_a_bad_agents_section_fails(tmp_path: Path, text: str, message: str) -> None:
    write_config(tmp_path, text)
    with pytest.raises(ReleaseError, match=message):
        AgentsConfig.load(tmp_path)


def test_no_config_file_names_the_gate(tmp_path: Path) -> None:
    with pytest.raises(ReleaseError, match="add an \\[agents\\] section"):
        AgentsConfig.load(tmp_path)


def test_the_release_policy_accepts_and_checks_agents() -> None:
    policy = Policy.parse(tomllib.loads(POLICY + '\n[agents]\nprompt = "/t"\n'), "policy")
    assert policy.agents == AgentsConfig("/t", False, 24)
    assert Policy.parse(tomllib.loads(POLICY), "policy").agents is None
    with pytest.raises(ReleaseError, match="unknown keys"):
        Policy.parse(tomllib.loads(POLICY + '\n[agents]\nprompt = "/t"\nwhen = 1\n'), "policy")


def git_(*args: str, cwd: Path) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout


@pytest.fixture
def gate_checkout(tmp_path: Path) -> tuple[Git, Path]:
    """A detached gate checkout and a second clone that pushes to the same origin"""
    origin = tmp_path / "origin.git"
    git_("init", "-q", "--bare", "-b", "main", str(origin), cwd=tmp_path)
    pusher = tmp_path / "pusher"
    git_("clone", "-q", str(origin), str(pusher), cwd=tmp_path)
    for k, v in (("user.name", "t"), ("user.email", "t@t")):
        git_("config", k, v, cwd=pusher)
    git_("commit", "-q", "--allow-empty", "-m", "one", cwd=pusher)
    git_("push", "-q", "origin", "main", cwd=pusher)
    root = tmp_path / "gate"
    git_("clone", "-q", str(origin), str(root), cwd=tmp_path)
    git_("checkout", "-q", "--detach", cwd=root)
    git_("commit", "-q", "--allow-empty", "-m", "two", cwd=pusher)
    git_("push", "-q", "origin", "main", cwd=pusher)
    return Git(root), pusher


def test_refresh_moves_a_detached_checkout_to_the_default_branch(gate_checkout: tuple[Git, Path]) -> None:
    git, pusher = gate_checkout
    refresh(git)
    assert git.run("rev-parse", "HEAD") == git_("rev-parse", "HEAD", cwd=pusher)
    assert git.run("rev-parse", "--abbrev-ref", "HEAD").strip() == "HEAD"


def test_refresh_refuses_a_working_copy(gate_checkout: tuple[Git, Path]) -> None:
    git, _ = gate_checkout
    git.run("checkout", "-q", "main")
    with pytest.raises(ReleaseError, match="is on a branch"):
        refresh(git)
    git.run("checkout", "-q", "--detach")
    (git.root / "tracked.txt").write_text("x", encoding="utf-8")
    git.run("add", "tracked.txt")
    with pytest.raises(ReleaseError, match="has changes"):
        refresh(git)


def test_a_busy_session_reads_no_config(checkout: Git) -> None:
    def unread() -> AgentsConfig:
        raise AssertionError("the config should not be read")

    claude = FakeClaude([bg("a", "idle", "blocked")])
    decision, _ = gate(checkout, "romamo/demo", unread, claude, lambda: [], NOW, refresh_checkout=True)
    assert decision.action is Action.WAITING
