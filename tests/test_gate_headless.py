"""Headless gate sessions (spec 005, D-17): `claude -p`, detached and tracked by its process"""

import datetime as dt
import importlib.util
import json
import os
import plistlib
import signal
import subprocess
import sys
import time
import tomllib
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from shipmill.agents import AgentsConfig, Mode
from shipmill.app import AppCheck, app_check
from shipmill.autonomy import Hold
from shipmill.config import Table
from shipmill.errors import ReleaseError
from shipmill.gate import (
    HEADLESS_REFUSED,
    HEADLESS_TOOLS,
    RECORD,
    SESSIONS,
    Action,
    ClaudeCli,
    Decision,
    Finding,
    Launch,
    Process,
    SessionEnv,
    StateRead,
    Waiting,
    fingerprint,
    gate,
    gate_paragraph,
    headless_paragraph,
    host_login,
    load_launch,
    prompt,
    save_launch,
    skills_dir,
    spawn_detached,
    start_time,
    state_dir,
    tick_lines,
    watch_command,
)
from shipmill.gitrepo import Git
from shipmill.launchd import build
from shipmill.worktrees import Judged

from .test_app import APP_ID, BOT, REPO, FakeApi, FakeSigner, key_file
from .test_gate import ISSUES, NO_PRUNE, NOW, FakeClaude, FakeNotifier, bg
from .test_gate_app import Prepared, gh_path

NAME = f"shipmill {REPO} {NOW:%Y-%m-%d %H:%M}"
SESSION = uuid.UUID("0b8f3c52-6a3e-4d55-9a8e-6d1f3f0b2a11")
LOGIN = "amy"
STARTED = "Tue Oct  6 10:09:01 2026"
PID = 4242
Tick = tuple[Decision, str | None, tuple[Waiting, ...], tuple[Judged, ...]]


@pytest.fixture
def checkout(tmp_path: Path) -> Git:
    root = tmp_path / "demo"
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    repo = Git(root)
    repo.run("remote", "add", "origin", "https://github.com/romamo/demo.git")
    return repo


def headless(app_id: int | None = None, max_wait_minutes: int = 15) -> AgentsConfig:
    return AgentsConfig(
        prompt="/t", prs=False, retry_hours=24, app_id=app_id, mode=Mode.HEADLESS, max_wait_minutes=max_wait_minutes
    )


def interactive(app_id: int | None = None) -> AgentsConfig:
    return AgentsConfig(prompt="/t", prs=False, retry_hours=24, app_id=app_id, mode=Mode.INTERACTIVE)


def agents(text: str) -> AgentsConfig:
    return AgentsConfig.parse(Table(tomllib.loads('[agents]\nprompt = "/t"\n' + text), "shipmill.toml").table("agents"))


@dataclass
class Reads:
    """The state reads a tick made, answering each with rows"""

    rows: list[Finding] = field(default_factory=lambda: [ISSUES])
    seen: list[StateRead] = field(default_factory=list)

    def __call__(self, read: StateRead) -> list[Finding]:
        self.seen.append(read)
        return list(self.rows)


def unread(read: StateRead) -> list[Finding]:
    raise AssertionError("the state read should not run")


def same(started: str | None) -> Callable[[int], str | None]:
    """A process table where every pid started at started (None: no such process)"""

    def read(pid: int) -> str | None:
        return started

    return read


def unasked(pid: int) -> str | None:
    raise AssertionError("an interactive launch is never looked up by pid")


def login() -> str:
    return LOGIN


def no_login() -> str:
    raise ReleaseError("gh api user failed: HTTP 401")


def tick(
    git: Git,
    agents: AgentsConfig,
    claude: FakeClaude | ClaudeCli,
    reads: Callable[[StateRead], list[Finding]],
    now: dt.datetime = NOW,
    dry_run: bool = False,
    started: Callable[[int], str | None] | None = None,
    who: Callable[[], str] = login,
    app: AppCheck | None = None,
    app_env: SessionEnv | None = None,
) -> Tick:
    return gate(
        git,
        REPO,
        lambda: agents,
        claude,
        reads,
        now,
        Hold,
        FakeNotifier(),
        NO_PRUNE,
        dry_run=dry_run,
        login=who,
        started=same(STARTED) if started is None else started,
        app=app,
        app_env=app_env,
        new_session=lambda: SESSION,
    )


def record_of(git: Git) -> dict[str, object]:
    data: dict[str, object] = json.loads((state_dir(git) / RECORD).read_text(encoding="utf-8"))
    return data


def log_of(git: Git, session: uuid.UUID = SESSION) -> Path:
    return state_dir(git) / SESSIONS / f"{session}.log"


@dataclass
class Spawned:
    """ClaudeCli's spawner and runner: records each detached command and answers `claude agents`"""

    commands: list[tuple[list[str], Path, Path]] = field(default_factory=list)
    run: list[list[str]] = field(default_factory=list)

    def __call__(self, cmd: list[str], cwd: Path, log: Path) -> int:
        self.commands.append((cmd, cwd, log))
        return PID

    def runner(self, cmd: list[str], cwd: Path) -> str:
        self.run.append(cmd)
        if cmd[:2] == ["claude", "agents"]:
            return "[]"
        if cmd[:2] == ["claude", "--bg"]:
            return "started; claude attach s9\n"
        raise AssertionError(f"unexpected command {cmd}")


# S-005-1


def test_s005_1_mode_defaults_to_interactive_and_reads_what_is_given() -> None:
    assert agents("").mode is Mode.INTERACTIVE
    assert agents('mode = "interactive"\n').mode is Mode.INTERACTIVE
    assert agents('mode = "headless"\n').mode is Mode.HEADLESS


@pytest.mark.parametrize(
    ("line", "message"),
    [
        ('mode = "auto"', "mode must be one of ['interactive', 'headless'], got 'auto'"),
        ('mode = "Headless"', "mode must be one of ['interactive', 'headless'], got 'Headless'"),
        ('mode = ""', "mode must be one of ['interactive', 'headless'], got ''"),
        ("mode = 1", "mode must be a string, got 1"),
        ("mode = true", "mode must be a string, got True"),
        ('mode = ["headless"]', "mode must be a string, got ['headless']"),
    ],
)
def test_s005_1_a_bad_mode_exits_2_naming_the_key(line: str, message: str) -> None:
    with pytest.raises(ReleaseError) as caught:
        agents(line + "\n")
    assert str(caught.value) == f"shipmill.toml [agents]: {message}"


def test_s005_1_mode_loads_from_the_config_file(tmp_path: Path) -> None:
    (tmp_path / ".github").mkdir()
    (tmp_path / ".github" / "shipmill.toml").write_text('[agents]\nprompt = "/t"\nmode = "headless"\n')
    assert AgentsConfig.load(tmp_path).mode is Mode.HEADLESS


# S-005-2


@pytest.mark.parametrize("config", [interactive(), AgentsConfig(prompt="/t", prs=False, retry_hours=24)])
def test_s005_2_interactive_launches_as_it_did_before(checkout: Git, config: AgentsConfig) -> None:
    spawned = Spawned()
    reads = Reads()
    claude = ClaudeCli(["--permission-mode=auto"], run=spawned.runner, spawn=spawned)
    decision, launched, _, _ = tick(checkout, config, claude, reads, started=unasked)
    assert (decision.action, launched, spawned.commands) == (Action.LAUNCH, "s9", [])
    text = prompt("/t", REPO, [ISSUES], NOW) + "\n\n" + gate_paragraph(LOGIN)  # D-21, #206
    assert spawned.run[1] == ["claude", "--bg", "-n", NAME, "--permission-mode=auto", text]
    assert decision.asks_as is None  # the no app_id line stays headless's (S-005-14)
    assert reads.seen == [StateRead()]
    assert record_of(checkout) == {"fingerprint": fingerprint([ISSUES]), "session": "s9", "at": NOW.isoformat()}
    assert tick_lines(decision, launched, (), (), dry_run=False)[-1] == "  launched s9: claude attach s9"


def test_s005_2_interactive_reads_the_state_without_the_trust_filter(checkout: Git) -> None:
    script = skills_dir() / "github-ship-watch" / "scripts" / "watch_state.py"
    cmd = watch_command(REPO, checkout.root, StateRead())
    assert cmd == [sys.executable, str(script), REPO, "--repo-dir", str(checkout.root), "--json"]


def test_s005_2_interactive_with_an_app_reads_the_state_without_a_bot_login(checkout: Git, tmp_path: Path) -> None:
    reads = Reads(rows=[])
    api = FakeApi()
    check = app_check_for(tmp_path, api)
    decision, *_ = tick(checkout, interactive(APP_ID), FakeClaude(), reads, app=check)
    assert decision.action is Action.QUIET
    assert reads.seen == [StateRead()] and api.calls == []  # no App check on a tick that launches nothing


def test_s005_2_interactive_takes_the_flags_headless_refuses(checkout: Git) -> None:
    claude = FakeClaude(args=("--permission-mode=auto", "--bg", "--session-id"))
    decision, launched, _, _ = tick(checkout, interactive(), claude, Reads())
    assert (decision.action, launched) == (Action.LAUNCH, "s1")


# D-21, #206: an interactive gate session asks through the needs-decision protocol too


def test_d21_the_interactive_prompt_ends_with_the_gate_paragraph_naming_the_login(checkout: Git) -> None:
    claude = FakeClaude()
    tick(checkout, interactive(), claude, Reads())
    [(_, text)] = claude.launched
    paragraph = (
        "Gate session: shipmill's gate started you, and nobody may be attached. Before you ask the user"
        " anything, post the question with the needs-decision protocol (github-issue-triage's"
        " references/needs-decision.md): mention @amy and label the item needs-decision. Then ask in"
        " this session too. An answer here: post it on the item, remove the label, and go on."
    )
    assert text == prompt("/t", REPO, [ISSUES], NOW) + "\n\n" + paragraph


def test_d21_an_interactive_launch_whose_login_read_fails_starts_nothing(checkout: Git) -> None:
    claude = FakeClaude()
    with pytest.raises(ReleaseError, match="HTTP 401"):
        tick(checkout, interactive(), claude, Reads(), who=no_login)
    assert claude.launched == [] and not (state_dir(checkout) / RECORD).exists()


def test_d21_an_interactive_tick_that_launches_nothing_reads_no_login(checkout: Git) -> None:
    decision, *_ = tick(checkout, interactive(), FakeClaude(), Reads(rows=[]), who=no_login)
    assert decision.action is Action.QUIET


# S-005-3


def test_s005_3_headless_runs_claude_p_with_the_allowlist(checkout: Git) -> None:
    spawned = Spawned()
    claude = ClaudeCli(["--allowedTools", "Bash(npm *)"], run=spawned.runner, spawn=spawned)
    decision, launched, _, _ = tick(checkout, headless(), claude, Reads())
    assert (decision.action, launched) == (Action.LAUNCH, str(SESSION))
    [(cmd, cwd, log)] = spawned.commands
    text = prompt("/t", REPO, [ISSUES], NOW) + "\n\n" + headless_paragraph(LOGIN)
    assert cmd == [
        "claude",
        "-p",
        "--permission-prompts",
        "none",
        "--allowedTools",
        HEADLESS_TOOLS,
        "--disallowedTools",
        "AskUserQuestion",
        "--session-id",
        str(SESSION),
        "-n",
        NAME,
        "--allowedTools",
        "Bash(npm *)",
        "--",
        text,
    ]
    assert "--bg" not in cmd and cmd[-1] == text
    assert cmd.index("--disallowedTools") < cmd.index("--session-id")
    assert (cwd, log) == (checkout.root, log_of(checkout))
    assert spawned.run == [["claude", "agents", "--json", "--cwd", str(checkout.root)]]  # no claude --bg


def test_s005_3_a_widened_allowlist_never_reads_the_prompt_as_a_tool(checkout: Git) -> None:
    """The documented widening, `--claude-arg=--allowedTools --claude-arg "Bash(npm *)"`, ends
    with a variadic tool list; without `--` Claude Code 2.1.291 reads the prompt as one more
    tool and exits "Input must be provided" without running anything"""
    spawned = Spawned()
    claude = ClaudeCli(["--allowedTools", "Bash(npm *)"], run=spawned.runner, spawn=spawned)
    tick(checkout, headless(), claude, Reads())
    [(cmd, _, _)] = spawned.commands
    assert cmd[-2] == "--" and cmd.count("--") == 1


def test_s005_3_the_allowlist_is_exactly_the_specs() -> None:
    assert HEADLESS_TOOLS == (
        "Read Edit Write Glob Grep Skill Agent SendMessage ListAgents TodoWrite"
        " Bash(gh *) Bash(git *) Bash(uv *) Bash(uvx *)"
    )


def test_s005_3_each_launch_gets_a_new_session_id(checkout: Git) -> None:
    claude = FakeClaude()
    for at in (NOW, NOW + dt.timedelta(days=2)):  # the second is past retry_hours
        gate(
            checkout,
            REPO,
            headless,
            claude,
            Reads(),
            at,
            Hold,
            FakeNotifier(),
            NO_PRUNE,
            login=login,
            started=same(None),  # the first ended before the second tick
        )
    first, second = (session for _, _, session, _ in claude.started)
    assert first != second and first.version == second.version == 4


def test_s005_3_with_an_app_the_settings_env_goes_before_the_name(checkout: Git, tmp_path: Path) -> None:
    spawned = Spawned()
    claude = ClaudeCli([], run=spawned.runner, spawn=spawned)
    prepared = Prepared(checkout, gh_path(tmp_path))
    tick(checkout, headless(APP_ID), claude, Reads(), app=app_check_for(tmp_path), app_env=prepared)
    [(cmd, _, _)] = spawned.commands
    at = cmd.index("--settings")
    assert cmd[at - 2 : at] == ["--session-id", str(SESSION)] and cmd[at + 2 : at + 4] == ["-n", NAME]
    assert json.loads(cmd[at + 1])["env"]["GIT_AUTHOR_NAME"] == BOT


# S-005-5


REFUSED = [flag for flag in HEADLESS_REFUSED] + [f"{flag}=x" for flag in HEADLESS_REFUSED]


def test_s005_5_the_refused_flags_are_the_specs() -> None:
    assert set(HEADLESS_REFUSED) == {
        "--permission-mode",
        "--permission-prompts",
        "--dangerously-skip-permissions",
        "--allow-dangerously-skip-permissions",
        "--bg",
        "--background",
        "--session-id",
    }


@pytest.mark.parametrize("arg", REFUSED)
def test_s005_5_a_refused_claude_arg_exits_2_and_stops_and_starts_nothing(checkout: Git, arg: str) -> None:
    claude = FakeClaude([bg("old", "idle", "done")], args=("--allowedTools", "Bash(npm *)", arg))
    with pytest.raises(ReleaseError) as caught:
        tick(checkout, headless(), claude, Reads())
    flag = arg.split("=", 1)[0]
    assert str(caught.value).startswith(f'--claude-arg {flag} is refused with [agents] mode = "headless"')
    assert (claude.started, claude.launched, claude.stopped) == ([], [], [])
    assert not (state_dir(checkout) / RECORD).exists()


def test_s005_5_a_refused_claude_arg_stops_no_waiting_session_either(checkout: Git) -> None:
    """The waiting step reads the config before it would stop a blocked session left from
    interactive mode; the refusal comes first"""
    claude = FakeClaude([bg("stuck", "idle", "blocked")], args=("--bg",))
    first = tick(checkout, headless(max_wait_minutes=1), FakeClaude([bg("stuck", "idle", "blocked")]), Reads())
    assert first[0].action is Action.WAITING
    with pytest.raises(ReleaseError, match="--claude-arg --bg is refused"):
        tick(checkout, headless(max_wait_minutes=1), claude, Reads(), now=NOW + dt.timedelta(hours=1))
    assert claude.stopped == []


# S-005-6


def test_s005_6_the_headless_prompt_ends_with_the_paragraph_naming_the_login(checkout: Git) -> None:
    claude = FakeClaude()
    tick(checkout, headless(), claude, Reads())
    [(_, text, _, _)] = claude.started
    paragraph = (
        "Headless: nobody can answer AskUserQuestion or a permission prompt. A decision for the user,"
        " or a tool call that was denied, becomes the needs-decision protocol (github-issue-triage's"
        " references/needs-decision.md): mention @amy, label the item needs-decision, and leave it."
    )
    assert text == prompt("/t", REPO, [ISSUES], NOW) + "\n\n" + paragraph
    assert (skills_dir() / "github-issue-triage" / "references" / "needs-decision.md").is_file()


def test_s005_6_the_login_is_read_with_gh_api_user() -> None:
    asked: list[list[str]] = []

    def run(cmd: list[str], cwd: Path) -> str:
        asked.append(cmd)
        return "amy\n"

    assert host_login(Path("/w"), run) == "amy"
    assert asked == [["gh", "api", "user", "-q", ".login"]]


@pytest.mark.parametrize("printed", ["", "\n", "not a login\n", "-amy\n", "a" * 40 + "\n"])
def test_s005_6_a_login_that_isnt_one_exits_2(printed: str) -> None:
    with pytest.raises(ReleaseError, match="not a GitHub login"):
        host_login(Path("/w"), lambda cmd, cwd: printed)


def test_s005_6_no_gh_exits_2() -> None:
    def missing(cmd: list[str], cwd: Path) -> str:
        raise FileNotFoundError(2, "No such file or directory", "gh")

    with pytest.raises(ReleaseError, match="gh is not on PATH"):
        host_login(Path("/w"), missing)


@pytest.mark.parametrize("dry_run", [False, True])
def test_s005_6_a_failing_login_read_exits_2_and_launches_nothing(checkout: Git, dry_run: bool) -> None:
    claude = FakeClaude([bg("old", "idle", "done")])
    with pytest.raises(ReleaseError, match="gh api user failed"):
        tick(checkout, headless(), claude, Reads(), dry_run=dry_run, who=no_login)
    assert (claude.started, claude.launched, claude.stopped) == ([], [], [])
    assert not (state_dir(checkout) / RECORD).exists()


# S-005-11


def app_check_for(tmp_path: Path, api: FakeApi | None = None) -> AppCheck:
    return app_check(REPO, key_file(tmp_path), tmp_path, FakeSigner(), api or FakeApi())


def test_s005_11_headless_reads_the_state_with_the_trust_filter(checkout: Git) -> None:
    reads = Reads(rows=[])
    decision, *_ = tick(checkout, headless(), FakeClaude(), reads)
    assert decision.action is Action.QUIET
    assert reads.seen == [StateRead(trusted_only=True)]
    cmd = watch_command(REPO, checkout.root, reads.seen[0])
    assert cmd[-2:] == ["--json", "--trusted-only"] and "--bot-login" not in cmd


def test_s005_11_headless_with_an_app_passes_its_bot_login(checkout: Git, tmp_path: Path) -> None:
    reads = Reads(rows=[])
    decision, *_ = tick(checkout, headless(APP_ID), FakeClaude(), reads, app=app_check_for(tmp_path))
    assert decision.action is Action.QUIET
    assert reads.seen == [StateRead(trusted_only=True, bot_login=BOT)]
    cmd = watch_command(REPO, checkout.root, reads.seen[0])
    assert cmd[-4:] == ["--json", "--trusted-only", "--bot-login", BOT]


def test_s005_11_watch_state_takes_the_flags_the_gate_passes(checkout: Git) -> None:
    script = skills_dir() / "github-ship-watch" / "scripts" / "watch_state.py"
    spec = importlib.util.spec_from_file_location("watch_state_flags", script)
    assert spec is not None and spec.loader is not None
    ws = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = ws  # dataclasses look their module up by name
    spec.loader.exec_module(ws)
    args = ws.arguments().parse_args(watch_command(REPO, checkout.root, StateRead(True, BOT))[2:])
    assert (args.trusted_only, args.bot_login, args.json) == (True, BOT, True)


def test_s005_11_a_failing_app_check_reads_no_state_and_launches_nothing(checkout: Git, tmp_path: Path) -> None:
    claude = FakeClaude([bg("old", "idle", "done")])
    with pytest.raises(ReleaseError, match="not installed on romamo/demo"):
        tick(checkout, headless(APP_ID), claude, unread, app=app_check_for(tmp_path, FakeApi(installed=False)))
    assert (claude.started, claude.launched, claude.stopped) == ([], [], [])
    assert not (state_dir(checkout) / RECORD).exists()


def test_s005_11_headless_with_app_id_and_no_app_check_reads_no_state(checkout: Git) -> None:
    with pytest.raises(ReleaseError, match="no App check was given"):
        tick(checkout, headless(APP_ID), FakeClaude(), unread)


# S-005-19


def test_s005_19_a_headless_launch_is_detached_logged_and_recorded(checkout: Git) -> None:
    claude = FakeClaude([bg("old", "idle", "done")])
    decision, launched, _, _ = tick(checkout, headless(), claude, Reads())
    assert (launched, claude.stopped, claude.launched) == (str(SESSION), ["old"], [])
    [(name, _, session, log)] = claude.started
    assert (name, session, log) == (NAME, SESSION, log_of(checkout))
    assert record_of(checkout) == {
        "fingerprint": fingerprint([ISSUES]),
        "session": str(SESSION),
        "at": NOW.isoformat(),
        "mode": "headless",
        "pid": PID,
        "started": STARTED,
    }
    assert tick_lines(decision, launched, (), (), dry_run=False)[-1] == (
        f"  launched {SESSION}: tail -f {log_of(checkout)}"
    )


def test_s005_19_a_dry_run_starts_and_writes_nothing(checkout: Git) -> None:
    claude = FakeClaude([bg("old", "idle", "done")])
    decision, launched, _, _ = tick(checkout, headless(), claude, Reads(), dry_run=True)
    assert (decision.action, launched) == (Action.LAUNCH, None)
    assert (claude.started, claude.stopped) == ([], [])
    assert not state_dir(checkout).exists()


def test_s005_19_the_session_runs_in_a_new_session_with_no_stdin_appending_its_log(tmp_path: Path) -> None:
    log = tmp_path / "state" / SESSIONS / "s.log"
    log.parent.mkdir(parents=True)
    log.write_text("earlier\n", encoding="utf-8")
    code = (
        "import os, sys;"
        "print('own session', os.getsid(0) == os.getpid(), flush=True);"
        "print('stdin empty', sys.stdin.read() == '', flush=True);"
        "print('to stderr', file=sys.stderr)"
    )
    pid = spawn_detached([sys.executable, "-c", code], tmp_path, log)
    deadline = time.monotonic() + 20
    while "to stderr" not in log.read_text(encoding="utf-8"):
        assert time.monotonic() < deadline, log.read_text(encoding="utf-8")
        time.sleep(0.05)
    assert os.waitpid(pid, 0)[0] == pid
    assert log.read_text(encoding="utf-8") == "earlier\nown session True\nstdin empty True\nto stderr\n"


def test_s005_19_the_launch_returns_without_waiting_and_its_start_time_is_read(tmp_path: Path) -> None:
    begun = time.monotonic()
    pid = spawn_detached(["sleep", "30"], tmp_path, tmp_path / "sleep.log")
    try:
        assert time.monotonic() - begun < 10
        started = start_time(pid)
        assert started and started == start_time(pid)
    finally:
        os.kill(pid, signal.SIGKILL)
        os.waitpid(pid, 0)
    assert start_time(pid) is None


def test_s005_19_a_command_that_cant_start_exits_2(tmp_path: Path) -> None:
    with pytest.raises(ReleaseError, match="could not start"):
        spawn_detached([str(tmp_path / "no-claude"), "-p"], tmp_path, tmp_path / "x.log")


# S-005-20


def write_headless_record(git: Git, started: str | None = STARTED) -> None:
    save_launch(state_dir(git) / RECORD, Launch(fingerprint([ISSUES]), str(SESSION), NOW, Process(PID, started)))


def test_s005_20_a_running_headless_session_reads_running(checkout: Git) -> None:
    write_headless_record(checkout)
    claude = FakeClaude()
    decision, launched, waiting, _ = tick(checkout, headless(), claude, unread, now=NOW + dt.timedelta(days=3))
    assert (decision.action, launched, waiting) == (Action.RUNNING, None, ())
    assert decision.reason == f"session {SESSION} is still working: tail -f {log_of(checkout)}"
    assert (claude.started, claude.stopped) == ([], [])


def test_s005_20_it_still_reads_running_after_the_mode_goes_back_to_interactive(checkout: Git) -> None:
    write_headless_record(checkout)
    decision, *_ = tick(checkout, interactive(), FakeClaude(), unread)
    assert decision.action is Action.RUNNING


@pytest.mark.parametrize("now_started", [None, "Wed Oct  7 08:00:00 2026"])
def test_s005_20_an_ended_session_or_a_reused_pid_decides_as_if_none_ran(
    checkout: Git, now_started: str | None
) -> None:
    write_headless_record(checkout)
    claude = FakeClaude()
    rows = [Finding("ISSUES", REPO, "NEW #20", True)]
    decision, launched, _, _ = tick(checkout, headless(), claude, Reads(rows=rows), started=same(now_started))
    assert (decision.action, launched) == (Action.LAUNCH, str(SESSION))
    # unchanged findings still wait for retry_hours, as after an interactive session ended
    write_headless_record(checkout)
    decision, *_ = tick(checkout, headless(), FakeClaude(), Reads(), started=same(now_started))
    assert decision.action is Action.UNCHANGED


def test_s005_20_a_session_whose_start_time_wasnt_read_is_not_running(checkout: Git) -> None:
    write_headless_record(checkout, started=None)
    decision, *_ = tick(checkout, headless(), FakeClaude(), Reads(rows=[]), started=same(STARTED))
    assert decision.action is Action.QUIET


def test_s005_20_a_headless_session_never_waits_and_is_never_stopped(checkout: Git) -> None:
    write_headless_record(checkout)
    claude = FakeClaude()
    for hours in (1, 5, 30):  # far past max_wait_minutes = 1
        decision, _, waiting, _ = tick(
            checkout, headless(max_wait_minutes=1), claude, unread, now=NOW + dt.timedelta(hours=hours)
        )
        assert (decision.action, waiting) == (Action.RUNNING, ())
    assert claude.stopped == []


def test_s005_20_a_record_without_mode_is_an_interactive_launch(checkout: Git) -> None:
    path = state_dir(checkout) / RECORD
    path.parent.mkdir(parents=True)
    old = {"fingerprint": fingerprint([ISSUES]), "session": "s1", "at": NOW.isoformat()}
    path.write_text(json.dumps(old, indent=2) + "\n", encoding="utf-8")
    assert load_launch(path) == Launch(fingerprint([ISSUES]), "s1", NOW)
    decision, *_ = tick(checkout, headless(), FakeClaude(), Reads(), started=unasked)
    assert decision.action is Action.UNCHANGED


@pytest.mark.parametrize(
    "extra",
    [
        {"mode": "auto"},
        {"mode": "headless"},
        {"mode": "headless", "pid": PID},
        {"mode": "headless", "pid": "4242", "started": STARTED},
        {"mode": "headless", "pid": 0, "started": STARTED},
        {"mode": "headless", "pid": True, "started": STARTED},
        {"mode": "headless", "pid": PID, "started": 5},
        {"mode": "headless", "pid": PID, "started": ""},
    ],
)
def test_s005_20_a_malformed_headless_record_exits_2(checkout: Git, extra: dict[str, object]) -> None:
    path = state_dir(checkout) / RECORD
    path.parent.mkdir(parents=True)
    record = {"fingerprint": "f", "session": str(SESSION), "at": NOW.isoformat(), **extra}
    path.write_text(json.dumps(record), encoding="utf-8")
    with pytest.raises(ReleaseError, match="is malformed"):
        load_launch(path)


def test_s005_20_a_headless_record_needs_a_uuid_session(checkout: Git) -> None:
    path = state_dir(checkout) / RECORD
    path.parent.mkdir(parents=True)
    record = {"fingerprint": "f", "session": "s1", "at": NOW.isoformat(), "mode": "headless", "pid": 1, "started": "x"}
    path.write_text(json.dumps(record), encoding="utf-8")
    with pytest.raises(ReleaseError, match="is malformed"):
        load_launch(path)


@pytest.mark.parametrize("session", [5, None, ["x"]])
def test_s005_20_a_headless_record_whose_session_isnt_a_string_exits_2(checkout: Git, session: object) -> None:
    path = state_dir(checkout) / RECORD
    path.parent.mkdir(parents=True)
    record = {"fingerprint": "f", "session": session, "at": NOW.isoformat(), "mode": "headless", "pid": 1}
    path.write_text(json.dumps({**record, "started": "x"}), encoding="utf-8")
    with pytest.raises(ReleaseError, match="is malformed"):
        load_launch(path)


# S-005-21


def test_s005_21_launchd_abandons_the_process_group(tmp_path: Path) -> None:
    folder = tmp_path / "bin"
    folder.mkdir()
    for name in ("claude", "gh", "git", "uvx"):
        (folder / name).write_text("#!/bin/sh\n", encoding="utf-8")
        (folder / name).chmod(0o755)
    job = build(REPO, tmp_path / "gate", 15, "x", [], tmp_path, str(folder), lambda _: False)
    assert plistlib.loads(job.document)["AbandonProcessGroup"] is True
