import json
from pathlib import Path

import pytest

from shipmill import UVX, cli_command
from shipmill.cli import main
from shipmill.config import config_path
from shipmill.doctor import doctor
from shipmill.errors import ReleaseError
from shipmill.init import init
from shipmill.policy import Lane, Policy
from shipmill.roadmap import RoadmapConfig

from .conftest import Repo

CI = """\
name: CI
on:
  push:
  workflow_call: # shipmill runs it on its release commit
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
    (repo.root / ".github" / "shipmill.toml").unlink()
    repo.write(".github/workflows/ci.yml", CI)
    initialized = init(repo.root, "ci.yml", force=False)
    written = initialized.written
    assert [p.relative_to(repo.root).as_posix() for p in written] == [
        ".github/shipmill.toml",
        ".github/workflows/release.yml",
    ]
    policy = Policy.load(written[0])
    assert (policy.name, policy.branch, policy.style.value, policy.version_files.value) == (
        "demo",
        "main",
        "keep-a-changelog",
        "pyproject",
    )
    caller = written[1].read_text(encoding="utf-8")
    assert "uses: ./.github/workflows/ci.yml" in caller
    assert "shipmill/shipmill/.github/workflows/prepare.yml@v0" in caller
    # the prepare job lists the release workflow's runs to replace an orphaned work branch (#175)
    prepare_job = caller[caller.index("  prepare:") : caller.index("  ci:")]
    assert "      actions: read #" in prepare_job
    checks = {c.name: c.status for c in doctor(repo.root)}
    assert checks["policy"] == checks["workflow"] == checks["ci"] == checks["changelog"] == "PASS"
    with pytest.raises(ReleaseError, match="shipmill.toml, .github/workflows/release.yml exist; pass --force"):
        init(repo.root, "ci.yml", force=False)


def test_the_policy_loads_from_its_file(repo: Repo) -> None:
    assert repo.policy_file == ".github/shipmill.toml"
    assert repo.policy.name == "demo"
    (repo.root / ".github" / "shipmill.toml").unlink()
    with pytest.raises(ReleaseError, match="^no release policy at .*shipmill.toml$"):
        config_path(repo.root)


def test_doctor_names_the_policy_file(repo: Repo) -> None:
    checks = {c.name: c for c in doctor(repo.root)}
    assert checks["policy"].status == "PASS" and checks["policy"].detail.startswith(".github/shipmill.toml, mode")


def test_doctor_reports_what_is_missing(repo: Repo) -> None:
    repo.write(
        repo.policy_file,
        repo.read(repo.policy_file).replace("[lanes.hotfix]", '[lanes.hotfix]\ndispatch = ["publish.yml"]'),
    )
    failed = {c.name for c in doctor(repo.root) if c.status == "FAIL"}
    assert failed == {"workflow", "dispatch"}
    repo.write(".github/workflows/publish.yml", PUBLISH)
    assert "dispatch" not in {c.name for c in doctor(repo.root) if c.status == "FAIL"}


DEPLOY = """\
name: Deploy
on:
  workflow_dispatch:
    inputs:
      tag:
        type: string
        required: true
      environment:
        type: string
        required: true
jobs:
  ref:
    runs-on: ubuntu-latest
    steps:
      - env:
          TAG: ${{ inputs.tag }}
        run: '[ "$GITHUB_REF" = "refs/tags/$TAG" ]'
  deploy:
    needs: ref
    runs-on: ubuntu-latest
    environment: ${{ inputs.environment }}
    steps: []
  operate:
    needs: deploy
    runs-on: ubuntu-latest
    permissions:
      actions: write
    steps:
      - env:
          GH_TOKEN: ${{ github.token }}
          GH_REPO: ${{ github.repository }}
        run: gh workflow run operate.yml -f dry-run=false
"""
GUARD_JOB = DEPLOY[DEPLOY.index("  ref:") : DEPLOY.index("  deploy:")]


def test_init_writes_an_environments_example_that_parses(repo: Repo) -> None:
    written = init(repo.root, "ci.yml", force=True).written
    text = written[0].read_text(encoding="utf-8")
    assert Policy.load(written[0]).environments == {}  # commented out
    start = text.index("# [environments.staging]")
    end = text.index("\n\n", start)
    example = "\n".join(line.removeprefix("#").removeprefix(" ") for line in text[start:end].splitlines())
    assert "${{ inputs.environment }}" in text
    repo.write(".github/shipmill.toml", text[:start] + example + text[end:])
    environments = Policy.load(written[0]).environments
    assert [(e.name, e.lane, e.source, e.bake_minutes) for e in environments.values()] == [
        ("staging", Lane.RC, None, 0),
        ("production", None, "staging", 60),
    ]


def test_doctor_checks_each_environments_workflow(repo: Repo) -> None:
    repo.write(
        repo.policy_file,
        repo.read(repo.policy_file)
        + '\n[environments.staging]\nlane = "rc"\nworkflow = "deploy.yml"\n'
        + '\n[environments.production]\nfrom = "staging"\nworkflow = "deploy.yml"\n',
    )

    def checks() -> list[tuple[str, str]]:
        return [(c.status, c.detail) for c in doctor(repo.root) if c.name == "environment"]

    assert checks() == [
        ("FAIL", "deploy.yml (staging): no .github/workflows/deploy.yml"),
        ("FAIL", "deploy.yml (production): no .github/workflows/deploy.yml"),
    ]
    repo.write(".github/workflows/deploy.yml", DEPLOY)
    passed = (
        "runs on workflow_dispatch with 'tag' and 'environment' inputs; fails a run that isn't on the tag;"
        " starts operate.yml after the deploy"
    )
    assert checks() == [("PASS", f"deploy.yml (staging) {passed}"), ("PASS", f"deploy.yml (production) {passed}")]

    no_input = DEPLOY.replace("      environment:\n        type: string\n        required: true\n", "")
    repo.write(".github/workflows/deploy.yml", no_input)
    assert checks()[0] == ("FAIL", "deploy.yml (staging) lacks the 'environment' input")
    # a blank line between inputs doesn't end the inputs (#53)
    spaced = DEPLOY.replace("      environment:\n", "\n      environment:\n")
    repo.write(".github/workflows/deploy.yml", spaced)
    assert checks()[0] == ("PASS", f"deploy.yml (staging) {passed}")
    # the input alone is not the job's environment
    repo.write(".github/workflows/deploy.yml", DEPLOY.replace("    environment: ${{ inputs.environment }}\n", ""))
    assert checks()[0] == (
        "FAIL",
        "deploy.yml (staging) lacks a job with environment: (GitHub records a deployment only then)",
    )


def test_doctor_wants_environment_as_a_jobs_own_key(repo: Repo) -> None:
    repo.write(
        repo.policy_file,
        repo.read(repo.policy_file) + '\n[environments.staging]\nlane = "rc"\nworkflow = "deploy.yml"\n',
    )
    head = DEPLOY[: DEPLOY.index("jobs:")]

    def status(jobs: str) -> str:
        repo.write(".github/workflows/deploy.yml", head + jobs + GUARD_JOB)
        return next(c.status for c in doctor(repo.root) if c.name == "environment")

    steps = "jobs:\n  deploy:\n    runs-on: ubuntu-latest\n    steps:\n      - run: echo\n"
    # an action's input or an env entry named environment records no deployment
    with_input = steps.replace("- run: echo\n", "- uses: o/deploy@v1\n        with:\n          environment: x\n")
    assert status(with_input) == "FAIL"
    assert status(steps.replace("    steps:", "    env:\n      environment: x\n    steps:")) == "FAIL"
    mapping = "    environment:\n      name: ${{ inputs.environment }}\n      url: https://example.com\n"
    assert status(steps.replace("    steps:", mapping + "    steps:")) == "PASS"
    # a job that calls a local reusable workflow deploys if that workflow's job sets it
    caller = "jobs:\n  deploy:\n    uses: ./.github/workflows/_deploy.yml\n    with:\n      environment: x\n"
    repo.write(".github/workflows/_deploy.yml", "on: workflow_call\n" + steps)
    assert status(caller) == "FAIL"
    repo.write(
        ".github/workflows/_deploy.yml",
        "on: workflow_call\n" + steps.replace("    steps:", "    environment: x\n    steps:"),
    )
    assert status(caller) == "PASS"
    # a blank line between steps doesn't end the jobs, so a later job's environment counts (#53)
    two_jobs = steps + "\n      - run: echo\n\n  deploy-site:\n    needs: deploy\n" + mapping + "    steps: []\n"
    assert status(two_jobs) == "PASS"


def test_doctor_warns_when_a_deploy_workflow_runs_off_its_tag(repo: Repo) -> None:
    """A deploy started without --ref <tag> records a deployment whose ref names no release
    tag (#80): doctor wants a step that fails such a run, comparing the ref, not ref_name"""
    repo.write(
        repo.policy_file,
        repo.read(repo.policy_file) + '\n[environments.staging]\nlane = "rc"\nworkflow = "deploy.yml"\n',
    )
    guard = '\'[ "$GITHUB_REF" = "refs/tags/$TAG" ]\''

    def check(text: str) -> tuple[str, str]:
        repo.write(".github/workflows/deploy.yml", text)
        return next((c.status, c.detail) for c in doctor(repo.root) if c.name == "environment")

    assert check(DEPLOY)[0] == "PASS"
    status, detail = check(DEPLOY.replace(guard, "echo $TAG"))
    assert status == "WARN"
    assert "no step fails a run whose github.ref isn't refs/tags/<tag>" in detail
    # the checkout of the tag isn't a guard; ref_name doesn't tell a branch named v1.2.0 from the tag
    assert check(DEPLOY.replace(guard, "echo refs/tags/$TAG"))[0] == "WARN"
    assert check(DEPLOY.replace(guard, '\'[ "$GITHUB_REF_NAME" = "refs/tags/$TAG" ]\''))[0] == "WARN"
    expression = "        if: github.ref != format('refs/tags/{0}', inputs.tag)\n        run: exit 1"
    assert check(DEPLOY.replace(f"        run: {guard}", expression))[0] == "PASS"
    either_order = '\'[ "refs/tags/$TAG" = "${GITHUB_REF}" ]\''
    assert check(DEPLOY.replace(guard, either_order))[0] == "PASS"
    # a workflow that fails the other checks reports those, not the guard
    assert check(DEPLOY.replace("    environment: ${{ inputs.environment }}\n", "").replace(guard, "x"))[0] == "FAIL"


def test_doctor_notes_whether_a_deploy_workflow_starts_operate(repo: Repo) -> None:
    """The deploy workflow starts the operate caller for a first health check right after
    the deploy (#84); a WARN only for an environment operate watches, with health or from (D-9)"""
    config = repo.read(repo.policy_file)
    starts = "        run: gh workflow run operate.yml -f dry-run=false\n"

    def check(text: str, environment: str = 'lane = "rc"\n') -> tuple[str, str]:
        policy = config + f'\n[environments.staging]\n{environment}workflow = "deploy.yml"\n'
        repo.write(repo.policy_file, policy)
        repo.write(".github/workflows/deploy.yml", text)
        return next((c.status, c.detail) for c in doctor(repo.root) if c.name == "environment")

    watched = 'lane = "rc"\nhealth = "https://staging.example.com/health"\n'
    status, detail = check(DEPLOY, watched)
    assert status == "PASS" and detail.endswith("; starts operate.yml after the deploy")
    # a continued line is one command
    assert (
        check(
            DEPLOY.replace(starts, "        run: gh workflow run operate.yml \\\n          -f dry-run=false\n"), watched
        )[0]
        == "PASS"
    )

    no_start = DEPLOY.replace(starts, "        run: echo deployed\n")
    status, detail = check(no_start, watched)
    assert status == "WARN"
    assert "but it doesn't start operate.yml after the deploy, so the first health check waits" in detail
    assert "`gh workflow run operate.yml -f dry-run=false` with actions: write" in detail
    # an environment operate doesn't watch sees no warning, nor a note
    status, detail = check(no_start)
    assert status == "PASS" and "operate.yml" not in detail

    dry = DEPLOY.replace(starts, "        run: gh workflow run operate.yml\n")
    assert check(dry, watched) == (
        "WARN",
        "deploy.yml (staging) runs on workflow_dispatch with 'tag' and 'environment' inputs; fails a run that"
        " isn't on the tag, but it starts operate.yml as a dry run: add -f dry-run=false",
    )
    # another workflow whose name ends the same isn't the caller
    assert check(DEPLOY.replace("run operate.yml", "run my-operate.yml"), watched)[0] == "WARN"
    # both warnings at once
    guard = '\'[ "$GITHUB_REF" = "refs/tags/$TAG" ]\''
    status, detail = check(no_start.replace(guard, "true"), watched)
    assert status == "WARN" and "isn't refs/tags/<tag>" in detail and "; and it doesn't start operate.yml" in detail


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
        ],
        repo.github,
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
    written = init(repo.root, "ci.yml", force=True).written
    assert Policy.load(written[0]).version_files.value == "none"


def test_doctor_fails_a_branch_on_origin_that_blocks_the_work_branch(repo: Repo) -> None:
    def check() -> tuple[str, str]:
        [found] = [c for c in doctor(repo.root) if c.name == "work branch"]
        return found.status, found.detail

    repo.git.run("push", "-q", "origin", "main:refs/heads/shipmill-x", "main:refs/heads/shipmill/v1.0.1")
    repo.git.run("fetch", "-q", "origin")
    assert check()[0] == "PASS"

    repo.git.run("branch", "shipmill", "main")  # local only: pushes to origin don't see it
    assert check() == (
        "WARN",
        "a local branch 'shipmill' would block the work branch shipmill/<tag> once pushed to origin",
    )

    repo.git.run("push", "-q", "origin", ":refs/heads/shipmill/v1.0.1", "shipmill:refs/heads/shipmill")
    repo.git.run("fetch", "-q", "--prune", "origin")
    assert check() == (
        "FAIL",
        "origin has a branch 'shipmill', which blocks the work branch shipmill/<tag>; delete or rename it",
    )


def test_doctor_asks_origin_not_the_clones_refs(repo: Repo) -> None:
    # A shallow clone fetches one branch, so its refs/remotes never showed origin's
    # 'shipmill' and doctor passed; a stale origin/shipmill failed it after the branch went
    def check(root: Path) -> str:
        [found] = [c for c in doctor(root) if c.name == "work branch"]
        return found.status

    origin = repo.root.parent / "origin.git"
    repo.git.run("push", "-q", "origin", "main:refs/heads/shipmill")
    shallow = repo.root.parent / "shallow"
    repo.git.run("clone", "-q", "--depth", "1", origin.as_uri(), str(shallow))
    assert not (shallow / ".git" / "refs" / "remotes" / "origin" / "shipmill").exists()
    assert check(shallow) == "FAIL"

    repo.git.run("fetch", "-q", "origin")
    repo.git.run("push", "-q", str(origin), ":refs/heads/shipmill")  # leaves origin/shipmill behind
    assert repo.git.ok("show-ref", "--verify", "-q", "refs/remotes/origin/shipmill")
    assert check(repo.root) == "PASS"


def test_doctor_warns_when_it_cannot_ask_origin(repo: Repo) -> None:
    def check() -> tuple[str, str]:
        [found] = [c for c in doctor(repo.root) if c.name == "work branch"]
        return found.status, found.detail

    repo.git.run("remote", "set-url", "origin", str(repo.root.parent / "gone.git"))
    status, detail = check()
    assert status == "WARN" and detail.startswith("can't ask origin for a branch 'shipmill': ")

    repo.git.run("remote", "remove", "origin")
    assert check() == ("WARN", "no 'origin' remote to ask for a branch 'shipmill'")


def test_doctor_reports_the_roadmap_only_when_configured(repo: Repo) -> None:
    assert "roadmap" not in {c.name for c in doctor(repo.root)}
    repo.write(repo.policy_file, repo.read(repo.policy_file) + "\n[roadmap]\nwip = 3\n")
    checks = {c.name: c for c in doctor(repo.root)}
    assert checks["roadmap"].status == "PASS"
    assert checks["roadmap"].detail == "wip 3 open issues, a milestone every 2 weeks; read by the product-intake skill"
    assert Policy.load(config_path(repo.root)).roadmap == RoadmapConfig(wip=3, cadence=2)
    repo.write(repo.policy_file, repo.read(repo.policy_file).replace("wip = 3", "wip = 3\ncadence = 1\nteam = 2"))
    checks = {c.name: c for c in doctor(repo.root)}
    assert checks["policy"].status == "FAIL" and "[roadmap]: unknown keys ['team']" in checks["policy"].detail


def test_init_hints_name_the_installed_form_and_the_file_the_uvx_one(
    repo: Repo, capsys: pytest.CaptureFixture[str]
) -> None:
    """#223, #236: output names how this CLI runs; the committed file the form that runs anywhere"""
    assert main(["--repo", str(repo.root), "init", "--force"]) == 0
    assert capsys.readouterr().out.splitlines()[-1] == f"next: review the policy, then run `{cli_command()} doctor`"
    assert main(["--repo", str(repo.root), "init", "--operate", "--force"]) == 0
    assert capsys.readouterr().out.splitlines()[-1] == f"next: run `{cli_command()} doctor`"
    assert f"run\n# `{UVX} init --operate`:" in config_path(repo.root).read_text(encoding="utf-8")
