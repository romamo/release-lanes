"""#233, D-22: with [agents] plugin_update = true the gate updates its checkout's own
project-scope plugin install, at most once a day, before it starts a session"""

import datetime as dt
import json
import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from shipmill.agents import AgentsConfig
from shipmill.autonomy import Hold
from shipmill.errors import ReleaseError
from shipmill.gate import Action, Decision, Waiting, gate, state_dir, tick_lines, tick_record
from shipmill.gitrepo import Git
from shipmill.plugin import CHECKED, PluginUpdate, daily_update, load_checked, project_install
from shipmill.worktrees import Judged

from .test_gate import ISSUES, NO_PRUNE, NOW, FakeClaude, FakeNotifier, bg, host, write_config

Tick = tuple[Decision, str | None, tuple[Waiting, ...], tuple[Judged, ...]]
UPDATE = ["claude", "plugin", "update", "shipmill@shipmill", "--scope", "project"]


@dataclass
class FakeRunner:
    """claude plugin list, gh release view, and claude plugin update, without either tool"""

    checkout: Path
    version: str | None = "0.25.0"  # the checkout's project install; None: none
    latest: str = "v0.32.2"
    fail_update: bool = False
    calls: list[tuple[list[str], Path]] = field(default_factory=list)

    def __call__(self, cmd: list[str], cwd: Path) -> str:
        self.calls.append((cmd, cwd))
        if cmd[:3] == ["claude", "plugin", "list"]:
            installs = [{"id": "shipmill@shipmill", "scope": "user", "version": "0.32.2"}]
            if self.version is not None:
                own = {"scope": "project", "version": self.version, "projectPath": str(self.checkout)}
                installs.append({"id": "shipmill@shipmill", **own})
            return json.dumps(installs)
        if cmd[:3] == ["gh", "release", "view"]:
            return self.latest + "\n"
        if cmd == UPDATE:
            if self.fail_update:
                raise ReleaseError("claude plugin update failed: network down")
            return "updated\n"
        raise AssertionError(f"unexpected command {cmd}")

    @property
    def updates(self) -> list[Path]:
        return [cwd for cmd, cwd in self.calls if cmd == UPDATE]


@pytest.fixture
def checkout(tmp_path: Path) -> Git:
    root = tmp_path / "demo"
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    git = Git(root)
    git.run("remote", "add", "origin", "https://github.com/romamo/demo.git")
    return git


def agents(plugin_update: bool) -> AgentsConfig:
    return AgentsConfig(prompt="/t", prs=False, retry_hours=24, plugin_update=plugin_update)


def tick(git: Git, runner: FakeRunner, plugin_update: bool = True, now: dt.datetime = NOW, dry: bool = False) -> Tick:
    return gate(
        git,
        "romamo/demo",
        lambda: agents(plugin_update),
        FakeClaude([bg("old", "idle", "done")]),
        lambda _: [ISSUES],
        now,
        Hold,
        FakeNotifier(),
        NO_PRUNE,
        dry_run=dry,
        login=host,
        plugins=runner,
    )


def test_a_behind_install_is_updated_in_the_checkout_before_the_launch(checkout: Git) -> None:
    runner = FakeRunner(checkout.root)
    decision, launched, _, _ = tick(checkout, runner)
    assert (decision.action, launched) == (Action.LAUNCH, "s1")
    assert runner.updates == [checkout.root]
    assert decision.plugin == PluginUpdate("0.25.0", "0.32.2", True)
    lines = tick_lines(decision, launched, (), (), dry_run=False)
    assert "  plugin updated: shipmill@shipmill 0.25.0 -> 0.32.2 in this checkout (project scope)" in lines
    assert tick_record(decision, launched, (), (), dry_run=False)["plugin"] == {
        "installed": "0.25.0",
        "latest": "0.32.2",
        "updated": True,
        "error": None,
    }
    assert load_checked(state_dir(checkout) / CHECKED) == NOW


def test_the_check_runs_at_most_once_a_day(checkout: Git) -> None:
    runner = FakeRunner(checkout.root)
    tick(checkout, runner)
    runner.calls.clear()
    (state_dir(checkout) / "gate.json").unlink()  # a new launch, not UNCHANGED
    decision, _, _, _ = tick(checkout, runner, now=NOW + dt.timedelta(hours=23))
    assert decision.action is Action.LAUNCH and decision.plugin is None and runner.calls == []
    (state_dir(checkout) / "gate.json").unlink()
    decision, _, _, _ = tick(checkout, runner, now=NOW + dt.timedelta(hours=24))
    assert decision.plugin is not None and len(runner.updates) == 1


def test_a_failed_update_is_reported_and_the_session_still_starts(checkout: Git) -> None:
    runner = FakeRunner(checkout.root, fail_update=True)
    decision, launched, _, _ = tick(checkout, runner)
    assert (decision.action, launched) == (Action.LAUNCH, "s1")
    assert decision.plugin == PluginUpdate("0.25.0", "0.32.2", False, "claude plugin update failed: network down")
    lines = tick_lines(decision, launched, (), (), dry_run=False)
    assert "  plugin update failed, the session starts anyway: claude plugin update failed: network down" in lines
    assert load_checked(state_dir(checkout) / CHECKED) == NOW  # retried the next day, not every tick


def test_a_failed_read_is_reported_and_retried_on_the_next_launch(checkout: Git) -> None:
    runner = FakeRunner(checkout.root, latest="latest")
    decision, launched, _, _ = tick(checkout, runner)
    assert launched == "s1" and decision.plugin is not None and decision.plugin.error is not None
    assert not (state_dir(checkout) / CHECKED).exists()


def test_a_current_install_is_left_alone_and_says_nothing(checkout: Git) -> None:
    runner = FakeRunner(checkout.root, version="0.32.2")
    decision, launched, _, _ = tick(checkout, runner)
    assert runner.updates == [] and decision.plugin == PluginUpdate("0.32.2", "0.32.2", False)
    assert not any("plugin" in line for line in tick_lines(decision, launched, (), (), dry_run=False))


def test_without_a_project_install_in_the_checkout_nothing_is_updated(checkout: Git) -> None:
    runner = FakeRunner(checkout.root, version=None)
    decision, _, _, _ = tick(checkout, runner)
    assert runner.updates == [] and decision.plugin == PluginUpdate(None, None, False)


def test_by_default_the_gate_changes_no_install(checkout: Git) -> None:
    runner = FakeRunner(checkout.root)
    decision, launched, _, _ = tick(checkout, runner, plugin_update=False)
    assert launched == "s1" and runner.calls == [] and decision.plugin is None
    assert tick_record(decision, launched, (), (), dry_run=False)["plugin"] is None


def test_a_dry_run_checks_nothing(checkout: Git) -> None:
    runner = FakeRunner(checkout.root)
    decision, _, _, _ = tick(checkout, runner, dry=True)
    assert decision.action is Action.LAUNCH and runner.calls == []


def test_a_tick_that_launches_nothing_checks_nothing(checkout: Git) -> None:
    runner = FakeRunner(checkout.root)
    decision, _, _, _ = gate(
        checkout,
        "romamo/demo",
        lambda: agents(True),
        FakeClaude(),
        lambda _: [],
        NOW,
        Hold,
        FakeNotifier(),
        NO_PRUNE,
        login=host,
        plugins=runner,
    )
    assert decision.action is Action.QUIET and runner.calls == []


def test_only_the_checkouts_own_project_install_counts() -> None:
    root = Path("/work/r/tmp/shipmill-gate")
    installs = [
        {"id": "shipmill@shipmill", "scope": "project", "version": "0.31.1", "projectPath": "/work/r"},
        {"id": "shipmill@shipmill", "scope": "local", "version": "0.20.0", "projectPath": str(root)},
        {"id": "other@x", "scope": "project", "version": "1.0.0", "projectPath": str(root)},
    ]
    assert project_install(json.dumps(installs), root) is None
    installs.append({"id": "shipmill@shipmill", "scope": "project", "version": "0.25.0", "projectPath": str(root)})
    assert project_install(json.dumps(installs), root) == "0.25.0"


def test_a_relative_checkout_matches_its_absolute_install(tmp_path: Path) -> None:
    # launchd runs `shipmill --repo tmp/shipmill-gate gate ...`; Claude Code records the full path
    gate = tmp_path / "r" / "tmp" / "shipmill-gate"
    gate.mkdir(parents=True)
    install = {"id": "shipmill@shipmill", "scope": "project", "version": "0.25.0", "projectPath": str(gate.resolve())}
    relative = Path(os.path.relpath(gate))
    assert not relative.is_absolute()
    assert project_install(json.dumps([install]), relative) == "0.25.0"


@pytest.mark.parametrize(
    "text",
    ["not json", "{}", '["x"]', '[{"id": "shipmill@shipmill", "scope": "project"}]'],
)
def test_a_malformed_plugin_list_is_refused(text: str) -> None:
    with pytest.raises(ReleaseError):
        project_install(text, Path("/w"))


@pytest.mark.parametrize("text", ["{", '{"checked": 1}', '{"checked": "2026-10-04T12:00:00"}', '{"checked": "x"}'])
def test_a_malformed_check_record_fails(tmp_path: Path, text: str) -> None:
    path = tmp_path / CHECKED
    path.write_text(text, encoding="utf-8")
    with pytest.raises(ReleaseError, match="malformed"):
        daily_update(path, tmp_path, NOW, FakeRunner(tmp_path))


@pytest.mark.parametrize(("value", "expected"), [("true", True), ("false", False)])
def test_the_key_is_read_from_the_config(tmp_path: Path, value: str, expected: bool) -> None:
    write_config(tmp_path, f'[agents]\nprompt = "/t"\nplugin_update = {value}\n')
    assert AgentsConfig.load(tmp_path).plugin_update is expected


def test_the_key_defaults_to_false_and_must_be_a_boolean(tmp_path: Path) -> None:
    write_config(tmp_path, '[agents]\nprompt = "/t"\n')
    assert AgentsConfig.load(tmp_path).plugin_update is False
    write_config(tmp_path, '[agents]\nprompt = "/t"\nplugin_update = "yes"\n')
    with pytest.raises(ReleaseError, match="plugin_update"):
        AgentsConfig.load(tmp_path)


def test_shipmills_own_gate_updates_its_plugin() -> None:
    assert AgentsConfig.load(Path(__file__).resolve().parents[1]).plugin_update is True
