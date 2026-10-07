"""Spec 004, At launch steps 4 and 5: with [agents] app_id set, the gate starts a session as
the App's bot, through `--settings` env that holds no token; unset, it launches as before"""

import datetime as dt
import json
import os
import plistlib
import subprocess
import sys
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from shipmill.app import (
    CACHE,
    CREDENTIAL_HELPER,
    HELPERS,
    Answer,
    Helpers,
    Identity,
    app_check,
    bot_id,
    prepare_session,
    session_env,
)
from shipmill.autonomy import Hold
from shipmill.errors import ReleaseError
from shipmill.gate import (
    RECORD,
    Action,
    Claude,
    ClaudeCli,
    Decision,
    Finding,
    StateRead,
    gate,
    interactive_prompt,
    state_dir,
    tick_lines,
    tick_record,
)
from shipmill.gitrepo import Git
from shipmill.launchd import build

from .test_app import APP_ID, BOT, BOT_ID, BOT_PATH, CHECKS, REPO, FakeApi, FakeSigner, app_cfg, key_file
from .test_gate import HOST, ISSUES, NO_PRUNE, NOW, FakeClaude, FakeNotifier, bg
from .test_gate import host as gh_login

EMAIL = f"{BOT_ID}+{BOT}@users.noreply.github.com"
NAME = f"shipmill {REPO} {NOW:%Y-%m-%d %H:%M}"


@pytest.fixture
def checkout(tmp_path: Path) -> Git:
    root = tmp_path / "demo"
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    git = Git(root)
    git.run("remote", "add", "origin", f"https://github.com/{REPO}.git")
    return git


def gh_path(tmp_path: Path) -> str:
    """A PATH whose only gh is a stand-in, as the gate's own PATH would have the real one"""
    folder = tmp_path / "tools"
    folder.mkdir(exist_ok=True)
    gh = folder / "gh"
    gh.write_text("#!/bin/sh\n", encoding="utf-8")
    gh.chmod(0o755)
    return os.pathsep.join([str(folder), "/usr/bin", "/bin"])


def helpers_of(git: Git) -> Path:
    return (state_dir(git) / HELPERS).resolve()


@dataclass
class Prepared:
    """The gate's session env as the CLI builds it, recording each identity it was asked for"""

    git: Git
    path: str
    asked: list[Identity] = field(default_factory=list)

    def __call__(self, identity: Identity) -> dict[str, str]:
        self.asked.append(identity)
        folder = state_dir(self.git) / HELPERS
        return prepare_session(identity, folder, Path(sys.executable), self.git.root, REPO, self.path)


def launch_as_app(
    git: Git, tmp_path: Path, claude: Claude, api: FakeApi | None = None, dry_run: bool = False
) -> tuple[Decision, str | None]:
    check = app_check(REPO, key_file(tmp_path), tmp_path, FakeSigner(), api or FakeApi())
    decision, launched, _, _ = gate(
        git,
        REPO,
        app_cfg,
        claude,
        lambda _: [ISSUES],
        NOW,
        Hold,
        FakeNotifier(),
        NO_PRUNE,
        dry_run=dry_run,
        app=check,
        app_env=Prepared(git, gh_path(tmp_path)),
        login=gh_login,
    )
    return decision, launched


def expected_env(git: Git, tmp_path: Path) -> dict[str, str]:
    folder = helpers_of(git)
    return {
        "PATH": os.pathsep.join([str(folder), gh_path(tmp_path)]),
        "GIT_AUTHOR_NAME": BOT,
        "GIT_AUTHOR_EMAIL": EMAIL,
        "GIT_COMMITTER_NAME": BOT,
        "GIT_COMMITTER_EMAIL": EMAIL,
        "GIT_CONFIG_COUNT": "4",
        "GIT_CONFIG_KEY_0": "credential.https://github.com.helper",
        "GIT_CONFIG_VALUE_0": "",
        "GIT_CONFIG_KEY_1": "credential.https://github.com.helper",
        "GIT_CONFIG_VALUE_1": str(folder / CREDENTIAL_HELPER),
        "GIT_CONFIG_KEY_2": "url.https://github.com/.insteadOf",
        "GIT_CONFIG_VALUE_2": "git@github.com:",
        "GIT_CONFIG_KEY_3": "url.https://github.com/.insteadOf",
        "GIT_CONFIG_VALUE_3": "ssh://git@github.com/",
    }


@dataclass
class Recorder:
    """ClaudeCli's runner: answers `claude agents` with no session and `claude --bg` with one"""

    commands: list[list[str]] = field(default_factory=list)

    def __call__(self, cmd: list[str], cwd: Path) -> str:
        self.commands.append(cmd)
        if cmd[:2] == ["claude", "agents"]:
            return "[]"
        if cmd[:2] == ["claude", "--bg"]:
            return "started; claude attach s9\n"
        raise AssertionError(f"unexpected command {cmd}")


# S-004-6


def test_s004_6_a_launch_as_the_app_gets_the_bots_env_and_helpers(checkout: Git, tmp_path: Path) -> None:
    claude = FakeClaude([bg("old", "idle", "done")])
    api = FakeApi()  # its POST raises: a launch mints no token
    decision, launched = launch_as_app(checkout, tmp_path, claude, api)
    assert (decision.action, launched, claude.stopped) == (Action.LAUNCH, "s1", ["old"])
    assert claude.envs == [expected_env(checkout, tmp_path)]
    assert [path for path, _ in api.calls] == CHECKS
    folder = helpers_of(checkout)
    assert sorted(p.name for p in folder.iterdir()) == ["gh", CREDENTIAL_HELPER]
    assert not (state_dir(checkout) / CACHE).exists()


def test_s004_6_the_bot_account_is_read_anonymously() -> None:
    api = FakeApi()
    assert bot_id("demo-agent", APP_ID, api) == BOT_ID
    assert api.calls == [(BOT_PATH, None)]


@pytest.mark.parametrize(
    ("answer", "message"),
    [
        (Answer(404, '{"message": "Not Found"}'), r"GitHub has no bot account demo-agent\[bot\] for app_id 123456"),
        (Answer(200, json.dumps({"login": BOT, "id": "1"})), "gave no user id"),
        (Answer(200, json.dumps({"login": "someone", "id": 5})), r"answered for 'someone', not demo-agent\[bot\]"),
        (Answer(500, "oops"), "answered 500: oops"),
    ],
)
def test_s004_6_a_bad_bot_account_exits_2_and_starts_nothing(
    checkout: Git, tmp_path: Path, answer: Answer, message: str
) -> None:
    claude = FakeClaude([bg("old", "idle", "done")])
    with pytest.raises(ReleaseError, match=message):
        launch_as_app(checkout, tmp_path, claude, FakeApi(bot=answer))
    assert (claude.launched, claude.stopped) == ([], [])
    assert not helpers_of(checkout).exists()


def test_s004_6_helpers_that_cant_be_written_stop_and_start_nothing(checkout: Git, tmp_path: Path) -> None:
    """D-14: no gh to wrap is a failure to set up the App's identity, never a host-login launch"""
    claude = FakeClaude([bg("old", "idle", "done")])
    check = app_check(REPO, key_file(tmp_path), tmp_path, FakeSigner(), FakeApi())
    with pytest.raises(ReleaseError, match="gh not found on PATH"):
        gate(
            checkout,
            REPO,
            app_cfg,
            claude,
            lambda _: [ISSUES],
            NOW,
            Hold,
            FakeNotifier(),
            NO_PRUNE,
            app=check,
            app_env=Prepared(checkout, str(tmp_path / "empty")),
            login=gh_login,
        )
    assert (claude.launched, claude.stopped) == ([], [])
    assert not (state_dir(checkout) / RECORD).exists()


def test_s004_6_with_app_id_set_and_no_session_env_nothing_starts(checkout: Git, tmp_path: Path) -> None:
    claude = FakeClaude([bg("old", "idle", "done")])
    check = app_check(REPO, key_file(tmp_path), tmp_path, FakeSigner(), FakeApi())
    with pytest.raises(ReleaseError, match="no session env was given; no session starts"):
        gate(
            checkout,
            REPO,
            app_cfg,
            claude,
            lambda _: [ISSUES],
            NOW,
            Hold,
            FakeNotifier(),
            NO_PRUNE,
            app=check,
            login=gh_login,
        )
    assert (claude.launched, claude.stopped) == ([], [])


def test_s004_6_claude_gets_settings_after_the_name_and_no_token(checkout: Git, tmp_path: Path) -> None:
    recorder = Recorder()
    claude = ClaudeCli(["--permission-mode=auto"], run=recorder)
    _, launched = launch_as_app(checkout, tmp_path, claude)
    assert launched == "s9"
    [listing, started] = recorder.commands
    assert listing[:3] == ["claude", "agents", "--json"]
    settings = json.dumps({"env": expected_env(checkout, tmp_path)})
    text = interactive_prompt("/t", REPO, [ISSUES], NOW, HOST)
    assert started == ["claude", "--bg", "-n", NAME, "--settings", settings, "--permission-mode=auto", text]
    assert not (state_dir(checkout) / CACHE).exists()  # nothing was minted, so no token can be in it
    assert "GH_TOKEN" not in settings and "x-access-token" not in settings


def test_s004_6_git_reads_the_identity_and_config_from_the_env(checkout: Git, tmp_path: Path) -> None:
    claude = FakeClaude()
    launch_as_app(checkout, tmp_path, claude)
    [env] = claude.envs
    assert env is not None
    host = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}

    def git(*args: str) -> list[str]:
        proc = subprocess.run(
            ["git", *args], cwd=checkout.root, env={**host, **env}, capture_output=True, text=True, check=True
        )
        return proc.stdout.splitlines()

    # GIT_CONFIG_* is read after the host's files: its empty value resets the helpers they set
    helper = str(helpers_of(checkout) / CREDENTIAL_HELPER)
    assert git("config", "--get-all", "credential.https://github.com.helper")[-2:] == ["", helper]
    rewrites = git("config", "--get-all", "url.https://github.com/.insteadof")
    assert rewrites[-2:] == ["git@github.com:", "ssh://git@github.com/"]
    assert git("var", "GIT_AUTHOR_IDENT")[0].startswith(f"{BOT} <{EMAIL}>")
    assert git("var", "GIT_COMMITTER_IDENT")[0].startswith(f"{BOT} <{EMAIL}>")


def test_s004_6_a_helper_path_that_needs_quoting_goes_in_as_a_shell_command(tmp_path: Path) -> None:
    folder = tmp_path / "a b"
    helpers = Helpers(folder, folder / "gh", folder / CREDENTIAL_HELPER, Path("/usr/bin/gh"))
    identity = Identity(APP_ID, "demo-agent", BOT_ID, tmp_path / "k.pem")
    env = session_env(identity, helpers, "/usr/bin")
    assert env["GIT_CONFIG_VALUE_1"] == f"!'{folder / CREDENTIAL_HELPER}'"
    assert env["PATH"] == f"{folder}{os.pathsep}/usr/bin"


# S-004-7


def test_s004_7_with_app_id_unset_the_launch_is_todays(checkout: Git, tmp_path: Path) -> None:
    recorder = Recorder()

    def unchecked(app_id: int, now: dt.datetime) -> Identity:
        raise AssertionError("no app_id, no App check")

    def unprepared(identity: Identity) -> Mapping[str, str]:
        raise AssertionError("no app_id, no helpers")

    decision, launched, _, _ = gate(
        checkout,
        REPO,
        lambda: app_cfg(None),
        ClaudeCli(["--permission-mode=auto"], run=recorder),
        lambda _: [ISSUES],
        NOW,
        Hold,
        FakeNotifier(),
        NO_PRUNE,
        app=unchecked,
        app_env=unprepared,
        login=gh_login,
    )
    assert (decision.action, decision.identity, launched) == (Action.LAUNCH, None, "s9")
    text = interactive_prompt("/t", REPO, [ISSUES], NOW, HOST)
    assert recorder.commands[1] == ["claude", "--bg", "-n", NAME, "--permission-mode=auto", text]
    assert not helpers_of(checkout).exists()
    assert not (state_dir(checkout) / CACHE).exists()


# S-004-11


def test_s004_11_a_launch_as_the_app_names_the_bot(checkout: Git, tmp_path: Path) -> None:
    decision, launched = launch_as_app(checkout, tmp_path, FakeClaude())
    lines = tick_lines(decision, launched, (), (), dry_run=False)
    assert lines[0] == f"LAUNCH: 1 finding(s) need an agent as {BOT}"
    assert not any("would launch" in line for line in lines)
    assert tick_record(decision, launched, (), (), dry_run=False)["identity"] == BOT


def test_s004_11_with_app_id_unset_identity_is_null(checkout: Git) -> None:
    decision, launched, _, _ = gate(
        checkout,
        REPO,
        lambda: app_cfg(None),
        FakeClaude(),
        lambda _: [ISSUES],
        NOW,
        Hold,
        FakeNotifier(),
        NO_PRUNE,
        login=gh_login,
    )
    assert tick_lines(decision, launched, (), (), dry_run=False)[0] == "LAUNCH: 1 finding(s) need an agent"
    record = json.loads(json.dumps(tick_record(decision, launched, (), (), dry_run=False)))
    assert "identity" in record and record["identity"] is None


def test_s004_11_a_dry_run_checks_the_app_and_writes_nothing(checkout: Git, tmp_path: Path) -> None:
    claude = FakeClaude([bg("old", "idle", "done")])
    api = FakeApi()
    decision, launched = launch_as_app(checkout, tmp_path, claude, api, dry_run=True)
    assert [path for path, _ in api.calls] == CHECKS  # the key, installation, permissions, and bot
    assert (decision.identity, launched, claude.launched, claude.stopped) == (BOT, None, [], [])
    assert not state_dir(checkout).exists()  # no helpers, no token cache, no launch record
    lines = tick_lines(decision, launched, (), (), dry_run=True)
    assert f"  would launch as {BOT}" in lines
    assert tick_record(decision, launched, (), (), dry_run=True)["identity"] == BOT


def test_s004_11_a_dry_run_still_exits_2_on_a_failed_check(checkout: Git, tmp_path: Path) -> None:
    with pytest.raises(ReleaseError, match=f"^app demo-agent is not installed on {REPO}$"):
        launch_as_app(checkout, tmp_path, FakeClaude(), FakeApi(installed=False), dry_run=True)
    assert not state_dir(checkout).exists()


# S-004-12


def test_s004_12_the_gates_own_reads_keep_the_hosts_environment(checkout: Git, tmp_path: Path) -> None:
    """The findings and the hold read run as subprocesses of the gate's own environment (watch
    and GhCli pass no env), so they see the App's helpers or a token only if the gate put them
    there; it never does, before or after a launch as the App"""
    host = dict(os.environ)
    seen: list[dict[str, str]] = []

    def findings(read: StateRead) -> list[Finding]:
        seen.append(dict(os.environ))
        return [ISSUES]

    def hold() -> Hold:
        seen.append(dict(os.environ))
        return Hold()

    check = app_check(REPO, key_file(tmp_path), tmp_path, FakeSigner(), FakeApi())
    claude = FakeClaude()
    for at in (NOW, NOW + dt.timedelta(days=2)):  # the second tick reads after the first launched as the App
        gate(
            checkout,
            REPO,
            app_cfg,
            claude,
            findings,
            at,
            hold,
            FakeNotifier(),
            NO_PRUNE,
            app=check,
            app_env=Prepared(checkout, gh_path(tmp_path)),
            login=gh_login,
        )
    assert len(claude.launched) == 2 and len(seen) == 4
    assert all(env == host for env in seen)
    assert dict(os.environ) == host
    assert str(helpers_of(checkout)) not in os.environ.get("PATH", "")


def test_s004_12_with_app_id_unset_the_reads_keep_the_hosts_environment(checkout: Git) -> None:
    host = dict(os.environ)
    seen: list[dict[str, str]] = []

    def findings(read: StateRead) -> list[Finding]:
        seen.append(dict(os.environ))
        return [ISSUES]

    gate(
        checkout,
        REPO,
        lambda: app_cfg(None),
        FakeClaude(),
        findings,
        NOW,
        Hold,
        FakeNotifier(),
        NO_PRUNE,
        login=gh_login,
    )
    assert seen == [host] and dict(os.environ) == host


# S-004-13


def tools(folder: Path) -> str:
    folder.mkdir(parents=True)
    for name in ("claude", "gh", "git", "uvx"):
        (folder / name).write_text("#!/bin/sh\n", encoding="utf-8")
        (folder / name).chmod(0o755)
    return str(folder)


def test_s004_13_launchd_puts_app_key_in_the_gates_arguments(tmp_path: Path) -> None:
    path = tools(tmp_path / "bin")
    key = tmp_path / "keys & <more>" / "app.pem"
    job = build(REPO, tmp_path / "gate", 15, "x", ["--permission-mode=auto"], tmp_path, path, lambda _: False, key)
    args = plistlib.loads(job.document)["ProgramArguments"]
    assert args[args.index("gate") :] == [
        "gate",
        REPO,
        "--refresh",
        "--app-key",
        str(key),
        "--claude-arg=--permission-mode=auto",
    ]


def test_s004_13_without_app_key_the_job_names_no_key(tmp_path: Path) -> None:
    job = build(REPO, tmp_path / "gate", 15, "x", [], tmp_path, tools(tmp_path / "bin"), lambda _: False)
    assert "--app-key" not in plistlib.loads(job.document)["ProgramArguments"]
