"""The decisions log script shared by github-issue-triage and github-pr-triage"""

import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "skills" / "github-issue-triage" / "scripts" / "decisions.py"


def decisions(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, str(SCRIPT), *args], cwd=cwd, capture_output=True, text=True, check=False)


def add(cwd: Path, title: str, applies: str, *extra: str) -> subprocess.CompletedProcess[str]:
    return decisions(
        cwd,
        "add",
        "--title", title,
        "--rule", f"{title}, always",
        "--why", "the user said so",
        "--applies", applies,
        "--enforced", "review",
        "--source", "romamo/demo#12",
        "--date", "2026-10-03",
        *extra,
    )  # fmt: skip


def test_add_creates_the_log_and_numbers_entries(tmp_path: Path) -> None:
    assert add(tmp_path, "Representation is --format", "src/cli/*, CLI flags").stdout == "D-1\n"
    assert add(tmp_path, "One handler catch", "src/handler.py").stdout == "D-2\n"
    log = (tmp_path / "docs" / "decisions.md").read_text()
    assert "## D-2: One handler catch\n\n- Decided: 2026-10-03, in romamo/demo#12\n" in log
    assert decisions(tmp_path, "check").returncode == 0


def test_supersede_links_both_ways_and_hides_the_old_rule(tmp_path: Path) -> None:
    add(tmp_path, "Old rule", "src/a.py")
    add(tmp_path, "Unrelated", "src/b.py")
    assert add(tmp_path, "New rule", "src/a.py", "--supersedes", "D-1").stdout == "D-3\n"
    log = (tmp_path / "docs" / "decisions.md").read_text()
    assert "- Enforced by: review\n- Superseded by: D-3\n\n## D-2" in log
    assert decisions(tmp_path, "check").returncode == 0
    assert decisions(tmp_path, "find", "src/a.py").stdout.startswith("D-3: New rule")
    assert "D-1" in decisions(tmp_path, "find", "--all", "src/a.py").stdout
    assert add(tmp_path, "Again", "src/a.py", "--supersedes", "D-1").returncode == 2


def test_find_matches_globs_prefixes_and_words(tmp_path: Path) -> None:
    add(tmp_path, "Flags", "src/cli/*, CLI flags")
    add(tmp_path, "Docs", "docs/")
    assert decisions(tmp_path, "find", "src/cli/main.py").stdout.startswith("D-1")
    assert decisions(tmp_path, "find", "docs/guide/intro.md").stdout.startswith("D-2")
    assert decisions(tmp_path, "find", "flags").stdout.startswith("D-1")
    assert decisions(tmp_path, "find", "src/other.py").returncode == 1


def test_check_reports_each_problem(tmp_path: Path) -> None:
    (tmp_path / "DECISIONS.md").write_text(
        "# Decisions\n\n## D-1: First\n\n- Decided: yesterday\n- Rule: r\n- Rule: again\n"
        "- Why: w\n- Applies to: x\n\n## D-3: Gap\n\n- Decided: 2026-10-03, in o/r#1\n- Rule: r\n"
        "- Why: w\n- Applies to: x\n- Enforced by: review\n- Supersedes: D-1\n## Notes\n"
    )
    out = decisions(tmp_path, "check")
    assert out.returncode == 1
    for problem in (
        "repeats 'Rule'",
        "D-1 (line 3): missing 'Enforced by'",
        "Decided must read",
        "expected D-2",
        "lacks 'Superseded by: D-3'",
        "a heading that isn't",
    ):
        assert problem in out.stdout, problem
    assert add(tmp_path, "Blocked", "x").returncode == 2


def test_no_log_is_an_input_error(tmp_path: Path) -> None:
    assert decisions(tmp_path, "check").returncode == 2
