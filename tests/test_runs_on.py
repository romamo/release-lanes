"""Spec 015: the release workflows' `runs-on` input and `shipmill init --runs-on`"""

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

from shipmill.cli import main
from shipmill.doctor import CALLER, OPERATE_CALLER
from shipmill.errors import ReleaseError
from shipmill.init import RunsOn

from .conftest import Repo

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"
GOLDEN = Path(__file__).parent / "golden" / "release.yml"
EXPRESSION = "${{ startsWith(inputs.runs-on, '[') && fromJSON(inputs.runs-on) || inputs.runs-on }}"
SETTLE_GROUP = "${{ github.event_name == 'push' && 'shipmill-settle' || format('shipmill-settle-{0}', github.run_id) }}"


def _jobs(text: str) -> dict[str, str]:
    """Each job's block, by name, from a workflow's `jobs:` mapping"""
    body = text.split("\njobs:\n", 1)[1]
    names = list(re.finditer(r"^  ([A-Za-z0-9_-]+):\n", body, re.MULTILINE))
    return {m[1]: body[m.end() : n.start() if n else len(body)] for m, n in zip(names, [*names[1:], None], strict=True)}


@pytest.mark.parametrize(
    ("workflow", "jobs"),
    [("prepare.yml", ["settle", "prepare", "propose"]), ("land.yml", ["land", "close-proposal", "cleanup", "upgrade"])],
)
def test_s015_1_every_release_job_runs_on_the_input(workflow: str, jobs: list[str]) -> None:
    text = (WORKFLOWS / workflow).read_text(encoding="utf-8")
    inputs = text.split("    inputs:\n", 1)[1].split("    outputs:\n", 1)[0].split("\njobs:\n", 1)[0]
    assert (
        "      runs-on:\n"
        '        description: "The runner for every job: a label, or a JSON list of labels"\n'
        "        type: string\n"
        '        default: "ubuntu-latest"\n'
    ) in inputs
    found = _jobs(text)
    assert list(found) == jobs
    for name, block in found.items():
        assert re.findall(r"^    runs-on: (.+)$", block, re.MULTILINE) == [EXPRESSION], name
    if workflow == "prepare.yml":  # D-1: the settle job's group is the one it had
        assert f"      group: {SETTLE_GROUP}\n      cancel-in-progress: true\n" in found["settle"]


def test_s015_1_the_repos_own_release_workflow_keeps_the_default() -> None:
    assert "runs-on" not in (WORKFLOWS / "release.yml").read_text(encoding="utf-8")


def test_s015_2_init_without_runs_on_writes_the_release_workflow_it_wrote_before(repo: Repo) -> None:
    assert main(["--repo", str(repo.root), "init", "--force"]) == 0
    written = (repo.root / CALLER).read_bytes()
    assert written == GOLDEN.read_bytes()
    assert b"runs-on" not in written and b'    - cron: "7 * * * *"' in written


@pytest.mark.parametrize(
    ("given", "line"),
    [("self-hosted", "runs-on: self-hosted"), ('["self-hosted", "linux"]', """runs-on: '["self-hosted","linux"]'""")],
)
def test_s015_3_init_runs_on_passes_the_runner_to_prepare_and_land(repo: Repo, given: str, line: str) -> None:
    assert main(["--repo", str(repo.root), "init", "--force", "--runs-on", given]) == 0
    jobs = _jobs((repo.root / CALLER).read_text(encoding="utf-8"))
    for name in ("prepare", "land"):
        with_ = jobs[name].split("    with:\n", 1)[1].split("    permissions:\n", 1)[0]
        assert f"      {line}\n" in with_, name
    assert "runs-on" not in jobs["ci"]
    # apart from the two lines, the file is the one written without the flag
    text = (repo.root / CALLER).read_text(encoding="utf-8")
    assert text.replace(f"      {line}\n", "") == GOLDEN.read_text(encoding="utf-8")


def test_s015_3_a_list_reaches_the_input_as_the_json_it_names() -> None:
    written = RunsOn.parse('["self-hosted", "linux", "it\'s"]').yaml()
    assert written == """'["self-hosted","linux","it''s"]'"""
    assert json.loads(written[1:-1].replace("''", "'")) == ["self-hosted", "linux", "it's"]


@pytest.mark.parametrize(
    ("label", "written"),
    [
        ("self-hosted", "self-hosted"),
        ("ubuntu-22.04", "ubuntu-22.04"),
        ("Linux_X64", "Linux_X64"),
        # bare, YAML reads these as a bool, a null, a number, or a mapping that doesn't parse
        ("true", "'true'"),
        ("False", "'False'"),
        ("null", "'null'"),
        ("~", "'~'"),
        ("123", "'123'"),
        ("0x1F", "'0x1F'"),
        (".inf", "'.inf'"),
        ("1e3", "'1e3'"),
        ("yes", "'yes'"),
        ("foo:", "'foo:'"),
    ],
)
def test_s015_3_a_label_reaches_the_input_as_the_string_it_names(repo: Repo, label: str, written: str) -> None:
    assert RunsOn.parse(label).yaml() == written
    assert main(["--repo", str(repo.root), "init", "--force", "--runs-on", label]) == 0
    jobs = _jobs((repo.root / CALLER).read_text(encoding="utf-8"))
    for name in ("prepare", "land"):
        assert f"      runs-on: {written}\n" in jobs[name], name


INVALID = [
    "",
    "self hosted",
    "self-hosted\t",
    "'self-hosted'",
    '"self-hosted"',
    "self-hosted,linux",
    "[self-hosted]",
    '["self-hosted",',
    "[]",
    '[""]',
    '["self-hosted", 1]',
    '[["self-hosted"]]',
    '[{"group": "x"}]',
    "*self-hosted",
]


@pytest.mark.parametrize("value", INVALID)
@pytest.mark.parametrize("flags", [[], ["--force"], ["--operate"], ["--operate", "--force"], ["--no-fragments"]])
def test_s015_4_init_refuses_a_malformed_runner_and_writes_nothing(repo: Repo, value: str, flags: list[str]) -> None:
    (repo.root / ".github" / "shipmill.toml").unlink()
    before = sorted(p.relative_to(repo.root) for p in repo.root.rglob("*") if ".git" not in p.parts)
    with pytest.raises(ReleaseError, match=re.escape(f"--runs-on {value!r}")):
        main(["--repo", str(repo.root), "init", *flags, "--runs-on", value])
    after = sorted(p.relative_to(repo.root) for p in repo.root.rglob("*") if ".git" not in p.parts)
    assert after == before


def test_s015_4_the_cli_exits_2_naming_the_value(repo: Repo) -> None:
    (repo.root / ".github" / "shipmill.toml").unlink()
    argv = [sys.executable, "-m", "shipmill", "--repo", str(repo.root), "init", "--runs-on", "self hosted"]
    out = subprocess.run(argv, capture_output=True, text=True, check=False)
    assert out.returncode == 2
    assert out.stderr.startswith("shipmill: --runs-on 'self hosted' is not a runner label")
    assert not (repo.root / CALLER).exists() and not (repo.root / ".github" / "shipmill.toml").exists()


def test_init_operate_refuses_a_runner_it_would_not_write(repo: Repo) -> None:
    """operate.yml takes no runner input (out of scope for spec 015), so a valid --runs-on with
    --operate is refused rather than ignored"""
    with pytest.raises(ReleaseError, match="--operate writes only"):
        main(["--repo", str(repo.root), "init", "--operate", "--runs-on", "self-hosted"])
    assert not (repo.root / OPERATE_CALLER).exists()


def test_a_label_and_a_list_parse_to_their_labels() -> None:
    assert RunsOn.parse("self-hosted") == RunsOn(("self-hosted",), listed=False)
    assert RunsOn.parse('["self-hosted","linux"]') == RunsOn(("self-hosted", "linux"), listed=True)
    assert RunsOn.parse('["one"]').yaml() == """'["one"]'"""
