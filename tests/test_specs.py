"""The specs script of the github-issue-triage skill's spec gate"""

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "skills" / "github-issue-triage" / "scripts" / "specs.py"
DECISIONS = (
    "# Decisions\n\n"
    "## D-1: Old\n\n- Decided: 2026-10-03, in o/r#1\n- Rule: r\n- Why: w\n- Applies to: x\n"
    "- Enforced by: review\n- Superseded by: D-2\n\n"
    "## D-2: New\n\n- Decided: 2026-10-03, in o/r#2\n- Rule: r\n- Why: w\n- Applies to: x\n"
    "- Enforced by: review\n- Supersedes: D-1\n"
)


def specs(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, str(SCRIPT), *args], cwd=cwd, capture_output=True, text=True, check=False)


def spec(
    number: str,
    *,
    behaviour: str = "Adds `src/tool/run.py` and the `--dry-run` flag.",
    criteria: tuple[str, ...] = ("1: tool run exits 2 on a missing config", "2: tool run --dry-run writes nothing"),
    decisions: str = "- D-2: New",
    issues: str = "- o/r#12",
) -> str:
    lines = [f"# S-{number}: Dry run", "", "## Problem", "", "Runs write files.", "", "## Behaviour", "", behaviour]
    lines += ["", "## Acceptance criteria", ""] + [f"- S-{number}-{c}" for c in criteria]
    lines += ["", "## Out of scope", "", "- Undo", "", "## Decisions relied on", "", decisions]
    lines += ["", "## Issues", "", issues, ""]
    return "\n".join(lines)


def repo(tmp_path: Path, **files: str) -> Path:
    (tmp_path / "docs" / "specs").mkdir(parents=True)
    (tmp_path / "docs" / "decisions.md").write_text(DECISIONS)
    for name, text in files.items():
        (tmp_path / "docs" / "specs" / name).write_text(text)
    return tmp_path


def test_new_numbers_specs_in_order_from_the_template(tmp_path: Path) -> None:
    root = repo(tmp_path)
    assert specs(root, "new", "dry-run").stdout == "docs/specs/001-dry-run.md\n"
    assert specs(root, "new", "--title", "Undo a run", "undo").stdout == "docs/specs/002-undo.md\n"
    text = (root / "docs" / "specs" / "002-undo.md").read_text()
    assert text.startswith("# S-002: Undo a run\n")
    assert "- S-002-1: <a statement" in text
    assert (root / "docs" / "specs" / "001-dry-run.md").read_text().startswith("# S-001: Dry run\n")
    assert specs(root, "check").returncode == 0
    assert specs(root, "new", "undo").returncode == 2
    assert specs(root, "new", "Bad_Slug").returncode == 2


def test_new_uses_the_repo_template_and_the_builtin_one_matches_ours(tmp_path: Path) -> None:
    root = repo(tmp_path)
    assert specs(root, "new", "first").returncode == 0
    builtin = (root / "docs" / "specs" / "001-first.md").read_text()
    ours = (ROOT / "docs" / "specs" / "TEMPLATE.md").read_text()
    assert builtin == ours.replace("S-NNN", "S-001").replace("<title>", "First")
    (root / "docs" / "specs" / "TEMPLATE.md").write_text(ours.replace("Who needs this", "Who asks"))
    assert specs(root, "new", "second").returncode == 0
    assert "Who asks" in (root / "docs" / "specs" / "002-second.md").read_text()


def test_a_good_spec_passes_check(tmp_path: Path) -> None:
    root = repo(tmp_path, **{"001-dry-run.md": spec("001"), "TEMPLATE.md": "anything"})
    out = specs(root, "check")
    assert (out.returncode, out.stdout) == (0, "")


def test_check_reports_each_problem(tmp_path: Path) -> None:
    bad = spec("002", criteria=("1: one", "1: again", "4: gap"), decisions="- D-1\n- D-9\n- maybe")
    bad = bad.replace("## Out of scope", "## Out Of Scope").replace("- o/r#12", "- see #12")
    root = repo(
        tmp_path,
        **{
            "001-dry-run.md": spec("001"),
            "002-bad.md": bad,
            "003-wrong.md": spec("004"),
            "004-dup.md": spec("004").replace("## Issues", "## Problem"),
            "5-short.md": spec("005"),
        },
    )
    out = specs(root, "check")
    assert out.returncode == 1
    for problem in (
        "5-short.md: not named NNN-<slug>.md",
        "002-bad.md:14: expected S-002-2",
        "002-bad.md:15: expected S-002-3",
        "unknown section 'Out Of Scope'",
        "missing the section 'Out of scope'",
        "a decision must read '- D-<n>'",
        "an issue must read",
        "names D-1, superseded by D-2",
        "names D-9, which the decisions log lacks",
        "003-wrong.md:1: the title says S-004, the file name S-003",
        "004-dup.md:24: repeats the section 'Problem'",
    ):
        assert problem in out.stdout, problem


def test_check_flags_order_criteria_and_decisions_without_a_log(tmp_path: Path) -> None:
    shuffled = spec("001").replace("## Problem", "## Tmp").replace("## Out of scope", "## Problem")
    shuffled = shuffled.replace("## Tmp", "## Out of scope")
    root = repo(
        tmp_path,
        **{
            "001-shuffled.md": shuffled,
            "002-empty.md": spec("002", criteria=()),
            "003-foreign.md": spec("003", criteria=("1: ok",)).replace("S-003-1", "S-007-1"),
        },
    )
    (root / "docs" / "decisions.md").unlink()
    out = specs(root, "check").stdout
    assert "001-shuffled.md:1: sections out of order" in out
    assert "002-empty.md:1: no acceptance criteria" in out
    assert "criterion S-007-1 in spec S-003" in out
    assert "names D-2, but there is no decisions log" in out


def test_find_matches_paths_globs_directories_and_words(tmp_path: Path) -> None:
    root = repo(
        tmp_path,
        **{
            "001-dry-run.md": spec("001"),
            "002-deploy.md": spec("002", behaviour="Everything under `src/deploy/` and `workflows/*.yml`."),
        },
    )
    assert specs(root, "find", "src/tool/run.py").stdout == "S-001: Dry run  (docs/specs/001-dry-run.md)\n"
    assert specs(root, "find", "src/deploy/env.py").stdout.startswith("S-002")
    assert specs(root, "find", "workflows/land.yml").stdout.startswith("S-002")
    assert specs(root, "find", "dry-run").stdout.startswith("S-001")
    assert specs(root, "find", "src/other.py").returncode == 1


def test_criteria_prints_one_spec(tmp_path: Path) -> None:
    root = repo(tmp_path, **{"007-dry-run.md": spec("007")})
    want = "S-007-1: tool run exits 2 on a missing config\nS-007-2: tool run --dry-run writes nothing\n"
    for name in ("007", "7", "S-007"):
        out = specs(root, "criteria", name)
        assert (out.returncode, out.stdout) == (0, want)
    assert specs(root, "criteria", "8").returncode == 2
    assert specs(root, "criteria", "x7").returncode == 2


def test_a_criterion_wrapped_onto_indented_lines_is_read_whole(tmp_path: Path) -> None:
    wrapped = ("1: tool run exits 2 on a missing config, and prints the path\n  it looked for", "2: one line")
    root = repo(tmp_path, **{"007-dry-run.md": spec("007", criteria=wrapped)})
    out = specs(root, "criteria", "7")
    want = "S-007-1: tool run exits 2 on a missing config, and prints the path it looked for\nS-007-2: one line\n"
    assert (out.returncode, out.stdout) == (0, want)
    assert specs(root, "check").returncode == 0


def test_no_specs_folder_is_an_input_error(tmp_path: Path) -> None:
    assert specs(tmp_path, "check").returncode == 2
    assert specs(tmp_path, "find", "x").returncode == 2


def test_the_repo_specs_pass_check() -> None:
    out = specs(ROOT, "check")
    assert (out.returncode, out.stdout) == (0, "")
