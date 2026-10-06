"""Spec 008: `shipmill status`, github-ship-watch's report from the CLI"""

import subprocess
import sys
from pathlib import Path

import pytest

import shipmill.cli
from shipmill.cli import main
from shipmill.errors import ReleaseError
from shipmill.gate import StateRead, skills_dir, watch, watch_command

REPO = "acme/web"
SCRIPT = skills_dir() / "github-ship-watch" / "scripts" / "watch_state.py"
TABLE = "BOT_FAILED     acme/web  release.yml failed\nPUBLISHED      v1.2.0    on PyPI\n"
ROW = '{"agent": true, "detail": "release.yml failed", "state": "BOT_FAILED", "subject": "acme/web"}\n'


class Script:
    """watch_state.py as the fake answers it; seen holds each command it was given"""

    def __init__(self, code: int = 0, stdout: str = TABLE, stderr: str = "") -> None:
        self.code, self.stdout, self.stderr = code, stdout, stderr
        self.seen: list[list[str]] = []

    def __call__(self, cmd: list[str]) -> subprocess.CompletedProcess[str]:
        self.seen.append(cmd)
        return subprocess.CompletedProcess(cmd, self.code, self.stdout, self.stderr)


def checkout(root: Path, origin: str = f"git@github.com:{REPO}.git") -> Path:
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "remote", "add", "origin", origin], check=True)
    return root.resolve()


def status(root: Path, script: Script, *extra: str) -> int:
    return main(["--repo", str(root), "status", *extra], state=script)


def test_s008_1_the_default_repo_is_origins_with_the_checkouts_top_level(tmp_path: Path) -> None:
    root = checkout(tmp_path / "repo")
    (root / "src").mkdir()
    script = Script()
    assert status(root / "src", script) == 0
    assert script.seen == [[sys.executable, str(SCRIPT), REPO, "--repo-dir", str(root)]]


def test_s008_2_an_origin_off_github_asks_for_the_repo(tmp_path: Path) -> None:
    root = checkout(tmp_path / "repo", "https://gitlab.com/acme/web.git")
    script = Script()
    with pytest.raises(ReleaseError, match=r"origin isn't a GitHub repo; name the repo"):
        status(root, script)
    assert script.seen == []


def test_s008_3_a_named_repo_must_be_the_checkouts_origin(tmp_path: Path) -> None:
    root = checkout(tmp_path / "repo")
    script = Script()
    with pytest.raises(ReleaseError, match=r"origin is git@github\.com:acme/web\.git, not acme/api"):
        status(root, script, "acme/api")
    assert script.seen == []
    assert status(root, script, REPO) == 0
    assert script.seen[0][2] == REPO


def test_s008_4_the_table_and_the_json_lines_are_printed_unchanged(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = checkout(tmp_path / "repo")
    assert status(root, Script()) == 0
    assert capsys.readouterr().out == TABLE
    script = Script(stdout=ROW)
    assert status(root, script, "--json") == 0
    assert capsys.readouterr().out == ROW
    assert script.seen[0][-1] == "--json"


def test_s008_5_the_scripts_answer_is_the_exit_code(tmp_path: Path) -> None:
    root = checkout(tmp_path / "repo")
    assert status(root, Script(code=1)) == 1
    assert status(root, Script(code=0)) == 0


@pytest.mark.parametrize(("code", "stdout"), [(2, ""), (2, TABLE), (3, ""), (1, ""), (1, "\n")])
def test_s008_6_a_failed_script_exits_2_with_its_stderr(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], code: int, stdout: str
) -> None:
    root = checkout(tmp_path / "repo")
    assert status(root, Script(code=code, stdout=stdout, stderr="error: gh repo view...: auth\n")) == 2
    out, err = capsys.readouterr()
    assert out == ""
    assert err == f"error: gh repo view...: auth\nshipmill: watch_state.py failed (exit {code})\n"


def test_s008_7_the_gate_and_status_share_watch_command(tmp_path: Path) -> None:
    root = checkout(tmp_path / "repo")
    base = [sys.executable, str(SCRIPT), REPO, "--repo-dir", str(root)]
    assert watch_command(REPO, root, StateRead()) == [*base, "--json"]
    assert watch_command(REPO, root, StateRead(), json=False) == base
    assert watch_command(REPO, root, StateRead(True, "x[bot]"), json=False) == [
        *base,
        "--trusted-only",
        "--bot-login",
        "x[bot]",
    ]
    script = Script(stdout=ROW)
    watch(REPO, root, StateRead(), script)
    assert script.seen == [[*base, "--json"]]


def test_s008_8_the_docstring_names_status_and_its_exit_1() -> None:
    doc = shipmill.cli.__doc__ or ""
    assert "\n  status " in doc
    assert "status found a row that needs\naction" in doc


@pytest.mark.parametrize("extra", [(), (REPO,)])
def test_s008_9_outside_a_checkout_status_says_to_run_it_in_one(tmp_path: Path, extra: tuple[str, ...]) -> None:
    script = Script()
    with pytest.raises(
        ReleaseError, match=r"is not a git checkout; run status in a checkout of the repo, or pass --repo PATH"
    ):
        status(tmp_path, script, *extra)
    assert script.seen == []


def test_s008_10_stderr_passes_through_on_0_and_1_and_whole_on_a_failure(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = checkout(tmp_path / "repo")
    for code in (0, 1):
        assert status(root, Script(code=code, stderr="warning: slow PyPI\n")) == code
        assert capsys.readouterr() == (TABLE, "warning: slow PyPI\n")
    trace = (
        "Traceback (most recent call last):\n"
        + "  frame\n" * 200
        + "urllib.error.URLError: <urlopen error timed out>\n"
    )
    assert status(root, Script(code=1, stdout="", stderr=trace)) == 2
    assert capsys.readouterr().err.startswith(trace)


def test_s008_11_the_gate_fails_on_a_crashed_script_with_the_end_of_stderr(tmp_path: Path) -> None:
    root = checkout(tmp_path / "repo")
    trace = (
        "Traceback (most recent call last):\n" + "  frame\n" * 200 + "urllib.error.URLError: <urlopen error timed out>"
    )
    with pytest.raises(
        ReleaseError, match=r"(?s)watch_state\.py failed \(exit 1\): .*URLError: <urlopen error timed out>$"
    ):
        watch(REPO, root, StateRead(), Script(code=1, stdout="", stderr=trace))
    with pytest.raises(ReleaseError, match=r"failed \(exit 2\)"):
        watch(REPO, root, StateRead(), Script(code=2, stdout="", stderr="error"))
    assert [f.state for f in watch(REPO, root, StateRead(), Script(code=1, stdout=ROW))] == ["BOT_FAILED"]


def test_s008_12_the_docs_name_shipmill_status() -> None:
    root = Path(__file__).resolve().parents[1]
    for doc in ("README.md", "skills/github-ship-watch/SKILL.md"):
        assert "shipmill status" in (root / doc).read_text(encoding="utf-8"), doc
