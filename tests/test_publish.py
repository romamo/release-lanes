"""Spec 010: publish.yml publishes shipmill to PyPI on each stable and hotfix release"""

import os
import re
import shutil
import subprocess
from pathlib import Path

from shipmill.doctor import doctor
from shipmill.policy import Lane, Policy

ROOT = Path(__file__).parent.parent
PUBLISH = ROOT / ".github" / "workflows" / "publish.yml"


def _text() -> str:
    return PUBLISH.read_text(encoding="utf-8")


def _steps() -> list[str]:
    """The publish job's steps, each as its own text"""
    text = _text()
    body = text[text.index("    steps:\n") + len("    steps:\n") :]
    return [f"      - {s}" for s in re.split(r"^      - ", body, flags=re.MULTILINE) if s.strip()]


def _step(name: str) -> str:
    return next(s for s in _steps() if s.startswith(f"      - name: {name}\n"))


def _script(name: str) -> str:
    """A step's `run: |` block, dedented"""
    step = _step(name)
    block = step[step.index("        run: |\n") + len("        run: |\n") :]
    return "".join(line.removeprefix("          ") for line in block.splitlines(keepends=True))


def _bash(script: str, cwd: Path, tag: str) -> subprocess.CompletedProcess[str]:
    bash = shutil.which("bash")
    assert bash is not None
    return subprocess.run(
        [bash, "-e", "-c", script], cwd=cwd, env={**os.environ, "TAG": tag}, capture_output=True, text=True
    )


def test_s010_1_dispatch_only_in_the_pypi_environment_with_oidc() -> None:
    text = _text()
    on = text[text.index("\non:\n") : text.index("\njobs:\n")]
    assert on == (
        "\non:\n"
        "  workflow_dispatch:\n"
        "    inputs:\n"
        "      tag:\n"
        '        description: "The release tag, such as v0.30.0"\n'
        "        type: string\n"
        "        required: true\n"
    )
    jobs = re.findall(r"^  (\S+):\n", text[text.index("\njobs:\n") :], re.MULTILINE)
    assert jobs == ["publish"]
    assert "\n    environment: pypi\n" in text
    # the job's grant is the only one: no workflow-level permissions to add to it
    assert re.findall(r"^\s*permissions:.*$", text, re.MULTILINE) == ["    permissions:"]
    assert "    permissions:\n      id-token: write\n      contents: read\n    steps:\n" in text
    assert "secrets." not in text and "secrets:" not in text
    assert "--trusted-publishing always" in _step("Publish")


def test_s010_2_a_tag_that_is_not_vxyz_fails_naming_it(tmp_path: Path) -> None:
    script = _script("Check the tag")
    for bad in ("v1.2", "1.2.3", "v1.2.3rc1", "v1.2.3-x", "main"):
        run = _bash(script, tmp_path, bad)
        assert run.returncode == 1
        assert run.stdout == f"::error::{bad} is not a vX.Y.Z release tag\n"
    assert _bash(script, tmp_path, "v12.0.3").returncode == 0
    # before anything is checked out
    steps = _steps()
    assert steps.index(_step("Check the tag")) == 0
    assert "actions/checkout" in steps[1]


def test_s010_2_a_version_other_than_the_tag_fails_naming_both(tmp_path: Path) -> None:
    script = _script("Check the version")
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "x"\nversion = "0.29.0"\n', encoding="utf-8")
    assert _bash(script, tmp_path, "v0.29.0").returncode == 0
    run = _bash(script, tmp_path, "v0.30.0")
    assert run.returncode == 1
    assert run.stdout == "::error::pyproject.toml at v0.30.0 holds version 0.29.0, not 0.30.0\n"


def test_s010_3_builds_the_tag_smoke_tests_the_wheel_then_publishes() -> None:
    steps = _steps()
    checkout = steps[1]
    assert "uses: actions/checkout@v7" in checkout
    assert "          ref: refs/tags/${{ inputs.tag }}\n" in checkout
    names = [re.match(r"      - name: (.+)\n", s) for s in steps]
    order = [m[1] for m in names if m]
    assert order == ["Check the tag", "Check the version", "Build", "Smoke-test the wheel", "Publish"]
    assert _step("Build").endswith("        run: uv build\n\n")
    smoke = _script("Smoke-test the wheel")
    assert 'uv venv --no-project "$smoke"' in smoke  # a fresh environment, nothing else installed
    assert 'uv pip install --python "$smoke/bin/python" "$wheel"' in smoke
    assert "bin/shipmill --help\n" in smoke
    assert "skills/github-ship-watch/scripts/watch_state.py" in smoke
    assert "skills/github-issue-triage/scripts/triage_state.py" in smoke
    # the scripts it asks for exist in the tree the wheel force-includes as shipmill/skills
    assert (ROOT / "skills/github-ship-watch/scripts/watch_state.py").is_file()
    assert (ROOT / "skills/github-issue-triage/scripts/triage_state.py").is_file()
    assert '"skills" = "shipmill/skills"' in (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert _step("Publish") == (
        "      - name: Publish\n"
        "        run: uv publish --trusted-publishing always --check-url https://pypi.org/simple/\n"
    )


def test_s010_4_stable_and_hotfix_dispatch_publish_and_doctor_passes(tmp_path: Path) -> None:
    policy = Policy.load(ROOT / ".github" / "shipmill.toml")
    dispatched = {lane: rule.dispatch for lane, rule in policy.lanes.items()}
    assert dispatched == {Lane.STABLE: ("move-major-tag.yml", "publish.yml"), Lane.HOTFIX: ("publish.yml",)}
    # doctor on a copy of the repo's release files: no origin to ask, no tags to count from
    for name in (".github", ".claude-plugin"):
        shutil.copytree(ROOT / name, tmp_path / name)
    for name in ("CHANGELOG.md", "pyproject.toml"):
        shutil.copy(ROOT / name, tmp_path / name)
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=tmp_path, check=True)
    checks = doctor(tmp_path)
    assert {c.name for c in checks if c.status == "FAIL"} == {"remote"}
    assert [(c.status, c.detail) for c in checks if c.name == "dispatch"] == [
        ("PASS", "move-major-tag.yml (stable) runs on workflow_dispatch with a 'tag' input"),
        ("PASS", "publish.yml (stable) runs on workflow_dispatch with a 'tag' input"),
        ("PASS", "publish.yml (hotfix) runs on workflow_dispatch with a 'tag' input"),
    ]


def test_s010_5_install_says_pypi_on_stable_and_hotfix_and_python_314() -> None:
    install = " ".join((ROOT / "docs" / "install.md").read_text(encoding="utf-8").split())
    assert "published to PyPI on each stable and hotfix release" in install
    assert "needs Python 3.14" in install
