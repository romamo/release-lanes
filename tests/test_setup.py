import json
from pathlib import Path

import pytest

from shipyard.cli import main
from shipyard.doctor import doctor
from shipyard.errors import ReleaseError
from shipyard.init import init
from shipyard.policy import Policy

from .conftest import Repo

CI = """\
name: CI
on:
  push:
  workflow_call: # the release bot runs it on its release commit
    inputs:
      ref:
        type: string
        required: true
jobs: {}
"""

PUBLISH = """\
name: Publish
on:
  workflow_dispatch:
    inputs:
      tag:
        type: string
jobs: {}
"""


def test_init_writes_a_policy_doctor_accepts(repo: Repo) -> None:
    (repo.root / ".github" / "release-policy.toml").unlink()
    repo.write(".github/workflows/ci.yml", CI)
    written = init(repo.root, "ci.yml", force=False)
    assert [p.name for p in written] == ["release-policy.toml", "release.yml"]
    policy = Policy.load(written[0])
    assert (policy.name, policy.branch, policy.style.value, policy.version_files.value) == (
        "demo",
        "main",
        "keep-a-changelog",
        "pyproject",
    )
    caller = written[1].read_text(encoding="utf-8")
    assert "uses: ./.github/workflows/ci.yml" in caller
    assert "romamo/shipyard/.github/workflows/prepare.yml@v0" in caller
    checks = {c.name: c.status for c in doctor(repo.root)}
    assert checks["policy"] == checks["workflow"] == checks["ci"] == checks["changelog"] == "PASS"
    with pytest.raises(ReleaseError, match="exists"):
        init(repo.root, "ci.yml", force=False)


def test_doctor_reports_what_is_missing(repo: Repo) -> None:
    repo.write(
        ".github/release-policy.toml",
        repo.read(".github/release-policy.toml").replace(
            "[lanes.hotfix]", '[lanes.hotfix]\ndispatch = ["publish.yml"]'
        ),
    )
    failed = {c.name for c in doctor(repo.root) if c.status == "FAIL"}
    assert failed == {"workflow", "dispatch"}
    repo.write(".github/workflows/publish.yml", PUBLISH)
    assert "dispatch" not in {c.name for c in doctor(repo.root) if c.status == "FAIL"}


def test_doctor_catches_a_version_line_that_no_longer_matches(repo: Repo) -> None:
    repo.write("README.md", "# demo\n")
    checks = [c for c in doctor(repo.root) if c.name == "version_lines"]
    assert checks[0].status == "FAIL" and "0 time(s)" in checks[0].detail


def test_doctor_fails_a_local_uv_source(repo: Repo) -> None:
    checks = {c.name: c.status for c in doctor(repo.root)}
    assert checks["sources"] == "PASS"
    repo.write(
        "pyproject.toml",
        repo.read("pyproject.toml")
        + '\n[tool.uv.sources]\nlib = { path = "../lib", editable = true }\n'
        + 'pkg = [{ index = "pytorch", marker = "sys_platform == \'linux\'" }, { path = "../pkg" }]\n'
        + 'remote = { git = "https://github.com/o/remote" }\n',
    )
    [check] = [c for c in doctor(repo.root) if c.name == "sources"]
    assert check.status == "FAIL" and check.detail.endswith(": lib, pkg")


def test_cli_plan_and_prepare(repo: Repo, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    repo.merge(1, "Added", "Feature A")
    # no blocker label: the CLI talks to GitHub through gh, which this repo has no remote for
    policy = repo.read(".github/release-policy.toml").replace(
        "[lanes.dev]", '[gates]\nblocker_label = ""\n\n[lanes.dev]'
    )
    repo.write(".github/release-policy.toml", policy)
    repo.git.run("commit", "-qam", "No blocker label")
    out = tmp_path / "out"
    code = main(
        [
            "--repo",
            str(repo.root),
            "plan",
            "--event",
            "workflow_dispatch",
            "--lane",
            "rc",
            "--now",
            "2026-10-06T08:00:00+00:00",
            "--github-output",
            str(out),
        ]
    )
    assert code == 0
    decision = json.loads(capsys.readouterr().out)
    assert (decision["action"], decision["version"]) == ("release", "1.1.0rc1")
    assert "version=1.1.0rc1\n" in out.read_text(encoding="utf-8")
    code = main(
        [
            "--repo",
            str(repo.root),
            "prepare",
            "--lane",
            "rc",
            "--version",
            "1.1.0rc1",
            "--base",
            decision["base"],
            "--date",
            "2026-10-06",
        ]
    )
    assert code == 0
    assert "stamped pyproject.toml" in capsys.readouterr().out
    assert 'version = "1.1.0rc1"' in repo.read("pyproject.toml")


def test_cli_notes(repo: Repo, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--repo", str(repo.root), "notes", "--version", "1.0.0"]) == 0
    assert capsys.readouterr().out == "### Added\n\n- The first release\n"


def test_init_ignores_a_placeholder_version(repo: Repo) -> None:
    repo.write("pyproject.toml", '[project]\nname = "demo"\nversion = "0.0.0"\n')
    written = init(repo.root, "ci.yml", force=True)
    assert Policy.load(written[0]).version_files.value == "none"


def test_doctor_fails_a_branch_on_origin_that_blocks_the_work_branch(repo: Repo) -> None:
    def check() -> tuple[str, str]:
        [found] = [c for c in doctor(repo.root) if c.name == "work branch"]
        return found.status, found.detail

    repo.git.run("push", "-q", "origin", "main:refs/heads/shipyard-x", "main:refs/heads/shipyard/v1.0.1")
    repo.git.run("fetch", "-q", "origin")
    assert check()[0] == "PASS"

    repo.git.run("branch", "shipyard", "main")  # local only: pushes to origin don't see it
    assert check() == (
        "WARN",
        "a local branch 'shipyard' would block the work branch shipyard/<tag> once pushed to origin",
    )

    repo.git.run("push", "-q", "origin", ":refs/heads/shipyard/v1.0.1", "shipyard:refs/heads/shipyard")
    repo.git.run("fetch", "-q", "--prune", "origin")
    assert check() == (
        "FAIL",
        "origin has a branch 'shipyard', which blocks the work branch shipyard/<tag>; delete or rename it",
    )


def test_doctor_asks_origin_not_the_clones_refs(repo: Repo) -> None:
    # A shallow clone fetches one branch, so its refs/remotes never showed origin's
    # 'shipyard' and doctor passed; a stale origin/shipyard failed it after the branch went
    def check(root: Path) -> str:
        [found] = [c for c in doctor(root) if c.name == "work branch"]
        return found.status

    origin = repo.root.parent / "origin.git"
    repo.git.run("push", "-q", "origin", "main:refs/heads/shipyard")
    shallow = repo.root.parent / "shallow"
    repo.git.run("clone", "-q", "--depth", "1", origin.as_uri(), str(shallow))
    assert not (shallow / ".git" / "refs" / "remotes" / "origin" / "shipyard").exists()
    assert check(shallow) == "FAIL"

    repo.git.run("fetch", "-q", "origin")
    repo.git.run("push", "-q", str(origin), ":refs/heads/shipyard")  # leaves origin/shipyard behind
    assert repo.git.ok("show-ref", "--verify", "-q", "refs/remotes/origin/shipyard")
    assert check(repo.root) == "PASS"


def test_doctor_warns_when_it_cannot_ask_origin(repo: Repo) -> None:
    def check() -> tuple[str, str]:
        [found] = [c for c in doctor(repo.root) if c.name == "work branch"]
        return found.status, found.detail

    repo.git.run("remote", "set-url", "origin", str(repo.root.parent / "gone.git"))
    status, detail = check()
    assert status == "WARN" and detail.startswith("can't ask origin for a branch 'shipyard': ")

    repo.git.run("remote", "remove", "origin")
    assert check() == ("WARN", "no 'origin' remote to ask for a branch 'shipyard'")
