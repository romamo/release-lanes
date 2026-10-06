"""The launchd job that runs the gate on a Mac"""

import os
import plistlib
from pathlib import Path

import pytest

from shipmill.cli import _parser
from shipmill.errors import ReleaseError
from shipmill.launchd import BASE_PATH, build, find_tool, is_temporary, label


def tools(folder: Path, *names: str) -> Path:
    folder.mkdir(parents=True)
    for name in names:
        tool = folder / name
        tool.write_text("#!/bin/sh\n", encoding="utf-8")
        tool.chmod(0o755)
    return folder


def nothing(folder: str) -> bool:
    return False


def test_the_label_is_safe_and_per_repo() -> None:
    assert label("romamo/Treaty_X") == "dev.shipmill.gate.romamo.treaty-x"
    with pytest.raises(ReleaseError, match="owner/name"):
        label("treaty")


def test_temporary_folders_are_skipped() -> None:
    assert is_temporary("/var/folders/xs/abc/T/cmux-cli-shims/6AA9")
    assert is_temporary("/tmp/shims")
    assert not is_temporary("/opt/homebrew/bin")


def test_a_tool_is_found_past_a_skipped_shim(tmp_path: Path) -> None:
    shim = tools(tmp_path / "shims", "claude")
    real = tools(tmp_path / "bin", "claude")
    path = os.pathsep.join([str(shim), str(real)])
    assert find_tool("claude", path, skip=lambda f: f == str(shim)) == real / "claude"
    with pytest.raises(ReleaseError, match="gh is not on PATH"):
        find_tool("gh", path, skip=nothing)


def test_the_job_runs_the_gate_with_a_stable_path(tmp_path: Path) -> None:
    local = tools(tmp_path / "local", "claude", "uvx")
    brew = tools(tmp_path / "brew", "gh", "git")
    path = os.pathsep.join([str(local), str(brew)])
    checkout = tmp_path / "treaty-gate"
    job = build("romamo/treaty", checkout, 15, "git+x@v0", ["--permission-mode=auto"], tmp_path, path, nothing)
    data = plistlib.loads(job.document)
    assert data["Label"] == job.label == "dev.shipmill.gate.romamo.treaty"
    assert data["ProgramArguments"] == [
        str(local / "uvx"),
        "--from",
        "git+x@v0",
        "shipmill",
        "--repo",
        str(checkout),
        "gate",
        "romamo/treaty",
        "--refresh",
        "--claude-arg=--permission-mode=auto",
    ]
    assert data["StartInterval"] == 900 and data["WorkingDirectory"] == str(checkout)
    assert data["EnvironmentVariables"]["PATH"].split(os.pathsep) == [str(local), str(brew), *BASE_PATH]
    assert job.plist == tmp_path / "Library" / "LaunchAgents" / "dev.shipmill.gate.romamo.treaty.plist"
    assert data["StandardOutPath"] == data["StandardErrorPath"] == str(job.log)


def test_s005_18_the_jobs_claude_args_parse_back_as_the_gates(tmp_path: Path) -> None:
    """The documented widening, `--claude-arg=--allowedTools --claude-arg "Bash(npm *)"`, reaches
    the job's gate as the same flags: a `--claude-arg` followed by a separate dash-led value is
    an argparse error, which would fail every tick"""
    path = os.pathsep.join([str(tools(tmp_path / "bin", "claude", "uvx", "gh", "git"))])
    widen = ["--allowedTools", "Bash(npm *)"]
    job = build("romamo/treaty", tmp_path / "gate", 15, "x", widen, tmp_path, path, nothing)
    args = plistlib.loads(job.document)["ProgramArguments"]
    parsed = _parser().parse_args(args[args.index("--repo") :])
    assert parsed.command == "gate" and parsed.claude_arg == widen
    documented = ["gate", "romamo/treaty", "--claude-arg=--allowedTools", "--claude-arg", "Bash(npm *)"]
    assert _parser().parse_args(documented).claude_arg == widen


def test_the_interval_is_bounded(tmp_path: Path) -> None:
    with pytest.raises(ReleaseError, match="5..1440"):
        build("romamo/treaty", tmp_path, 1, "x", [], tmp_path, "", nothing)
