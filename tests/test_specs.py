"""The specs script of the github-issue-triage skill's spec gate"""

import json
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
    issues: str | None = None,
    status: str = "approved",
    verification: str = "",
) -> str:
    lines = [f"# S-{number}: Dry run", "", f"status: {status}", "", "## Problem", "", "Runs write files."]
    lines += ["", "## Behaviour", "", behaviour]
    lines += ["", "## Acceptance criteria", ""] + [f"- S-{number}-{c}" for c in criteria]
    lines += ["", "## Out of scope", "", "- Undo", "", "## Decisions relied on", "", decisions]
    if issues is None:  # one build issue delivers every criterion
        issues = "- o/r#12: " + ", ".join(f"S-{number}-{c.split(':')[0]}" for c in criteria)
    lines += ["", "## Issues", "", issues, "", "## Verification", "", verification, ""]
    return "\n".join(lines)


def built(number: str) -> str:
    verified = f"- S-{number}-1: ran it on main, exit 2\n- S-{number}-2: ran it on main, no files"
    return spec(number, status="built", verification=verified)


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
        "002-bad.md:16: expected S-002-2",
        "002-bad.md:17: expected S-002-3",
        "unknown section 'Out Of Scope'",
        "missing the section 'Out of scope'",
        "a decision must read '- D-<n>'",
        "an issue must read",
        "names D-1, superseded by D-2",
        "names D-9, which the decisions log lacks",
        "003-wrong.md:1: the title says S-004, the file name S-003",
        "004-dup.md:26: repeats the section 'Problem'",
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


def test_no_specs_folder_passes_check_and_coverage(tmp_path: Path) -> None:
    # A repo that copies the CI step before writing its first spec stays green
    for command in ("check", "coverage"):
        out = specs(tmp_path, command)
        assert (out.returncode, out.stdout) == (0, "no specs\n"), command
    assert specs(tmp_path, "coverage", "--spec", "7").returncode == 2
    assert specs(tmp_path, "find", "x").returncode == 2


def fresh(root: Path, slug: str, status: str) -> Path:
    """A spec straight from `specs.py new`, its status set to status"""
    path = root / specs(root, "new", slug).stdout.strip()
    path.write_text(path.read_text().replace("status: draft", f"status: {status}"))
    return path


def test_check_refuses_template_placeholders_once_approved(tmp_path: Path) -> None:
    root = repo(tmp_path)
    fresh(root, "draft", "draft")
    fresh(root, "approved", "approved")
    out = specs(root, "check")
    assert out.returncode == 1
    assert "001-draft.md" not in out.stdout
    for section in ("Problem", "Behaviour", "Acceptance criteria", "Out of scope"):
        assert f"approved, but '{section}' still holds the template's placeholder" in out.stdout, section
    assert "002-approved.md:7: approved, but 'Problem'" in out.stdout
    assert "- S-002-1: <a statement" in out.stdout
    # Issues and Verification are filled in when the spec is built
    assert "'Issues'" not in out.stdout and "'Verification'" not in out.stdout
    assert "'Decisions relied on'" not in out.stdout  # "- none" is an answer, not a placeholder
    assert specs(root, "criteria", "2").returncode == 1


def test_check_refuses_every_placeholder_once_built(tmp_path: Path) -> None:
    root = repo(tmp_path)
    text = built("001").replace(
        "## Issues\n\n",
        "## Issues\n\nThe build issues, filled in once they are filed: one `- owner/repo#N: S-001-1, S-001-2` per\n"
        "line, naming the criteria that issue delivers. Each criterion belongs to exactly one.\n\n",
    )
    (root / "docs" / "specs" / "001-dry-run.md").write_text(text)
    out = specs(root, "check")
    assert out.returncode == 1
    assert "built, but 'Issues' still holds the template's placeholder" in out.stdout


def test_placeholders_come_from_the_repo_template(tmp_path: Path) -> None:
    root = repo(tmp_path)
    ours = (ROOT / "docs" / "specs" / "TEMPLATE.md").read_text()
    (root / "docs" / "specs" / "TEMPLATE.md").write_text(ours.replace("Who needs this", "Who asks"))
    (root / "docs" / "specs" / "001-dry-run.md").write_text(spec("001").replace("Runs write files.", "Who asks"))
    assert specs(root, "check").returncode == 0  # one line of a paragraph is not the placeholder
    fresh(root, "next", "approved")
    assert "002-next.md:7: approved, but 'Problem' still holds" in specs(root, "check").stdout


def test_the_repo_specs_pass_check() -> None:
    out = specs(ROOT, "check")
    assert (out.returncode, out.stdout) == (0, "")


def test_status_is_required_and_a_built_spec_is_verified(tmp_path: Path) -> None:
    root = repo(
        tmp_path,
        **{
            "001-built.md": built("001"),
            "002-odd.md": spec("002", status="done"),
            "003-none.md": spec("003").replace("status: approved\n", ""),
            "004-unverified.md": spec("004", status="built", issues="", verification="- S-004-1: ran it"),
        },
    )
    out = specs(root, "check").stdout
    assert "001-built.md" not in out
    assert "002-odd.md:3: status must be one of draft, approved, built, got 'done'" in out
    assert "003-none.md:1: no status line" in out
    assert "004-unverified.md:1: built, but lists no build issues" in out
    assert "004-unverified.md:1: built, but Verification doesn't name S-004-2" in out
    assert "S-004-1" not in out
    specs(root, "new", "next")
    assert "\nstatus: draft\n" in (root / "docs" / "specs" / "005-next.md").read_text()


def write(root: Path, path: str, *lines: str) -> None:
    file = root / path
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_text("\n".join(lines) + "\n")


def test_coverage_finds_both_forms_in_each_language(tmp_path: Path) -> None:
    root = repo(tmp_path, **{"007-dry-run.md": built("007"), "008-later.md": spec("008")})
    write(root, "tests/test_run.py", "def test_s007_1_exits_2_on_a_missing_config():", "    pass")
    write(root, "web/run.test.ts", "// proves: S-007-1, S-007-2", "it('runs', () => {})")
    write(root, "web/__tests__/dry.js", "test('s007_2 writes nothing', () => {})")
    write(root, "go/run_test.go", "func TestS007_2DryRun(t *testing.T) {}")
    write(root, "src/lib.rs", "    #[test]", "    fn test_s007_1_missing() {}")
    write(root, "tests/conftest.py", "def test_s007_2_not_a_test_file(): pass")
    write(root, "node_modules/x/a.test.js", "// proves: S-007-2")
    out = specs(root, "coverage")
    assert out.returncode == 0, out.stdout
    assert out.stdout == (
        "S-007: Dry run (built)\n"
        "  S-007-1: tool run exits 2 on a missing config\n"
        "    src/lib.rs:2 test_s007_1_missing\n"
        "    tests/test_run.py:1 test_s007_1_exits_2_on_a_missing_config\n"
        "    web/run.test.ts:1\n"
        "  S-007-2: tool run --dry-run writes nothing\n"
        "    go/run_test.go:1 TestS007_2DryRun\n"
        "    web/__tests__/dry.js:1 s007_2 writes nothing\n"
        "    web/run.test.ts:1\n"
    )


def test_coverage_reports_a_criterion_with_no_test(tmp_path: Path) -> None:
    root = repo(tmp_path, **{"007-dry-run.md": built("007")})
    write(root, "tests/test_run.py", "# proves: S-007-1", "def test_exits():", "    pass")
    write(root, "tests/test_old.py", "def test_s007_9_renamed(): pass", "def test_s0071_1_other_spec(): pass")
    out = specs(root, "coverage")
    assert out.returncode == 1
    assert "  S-007-2: tool run --dry-run writes nothing\n    no test proves it\n" in out.stdout
    assert "S-007-9: no such criterion, yet named by tests/test_old.py:1 test_s007_9_renamed" in out.stdout


def test_coverage_skips_a_python_fixture_inside_a_string(tmp_path: Path) -> None:
    root = repo(tmp_path, **{"007-dry-run.md": built("007")})
    fixture = ('FIXTURE = """', "# proves: S-007-2", "def test_s007_2_in_a_string():", '"""')
    write(root, "tests/test_run.py", *fixture, "", "# proves: S-007-1", "def test_exits():", "    pass")
    out = specs(root, "coverage")
    assert out.returncode == 1
    assert "    tests/test_run.py:6\n" in out.stdout
    assert "  S-007-2: tool run --dry-run writes nothing\n    no test proves it\n" in out.stdout


def test_coverage_ignores_specs_not_yet_built_unless_named(tmp_path: Path) -> None:
    root = repo(tmp_path, **{"007-dry-run.md": spec("007"), "008-draft.md": spec("008", status="draft")})
    out = specs(root, "coverage")
    assert (out.returncode, out.stdout) == (0, "no built specs\n")
    write(root, "tests/test_run.py", "def test_s007_1_x(): pass", "def test_s007_2_x(): pass")
    assert specs(root, "coverage", "--spec", "7").returncode == 0
    assert specs(root, "coverage", "--spec", "S-008").returncode == 1
    assert specs(root, "coverage", "--spec", "9").returncode == 2
    assert specs(root, "coverage", "--root", "missing").returncode == 2


def test_the_repo_built_specs_are_covered() -> None:
    out = specs(ROOT, "coverage")
    assert out.returncode == 0, out.stdout


# Task graph (#71): an approved spec splits into build issues, each criterion in exactly one

FOUR = ("1: a", "2: b", "3: c", "4: d")


def test_check_assigns_each_criterion_to_exactly_one_build_issue(tmp_path: Path) -> None:
    lines = "\n".join(
        (
            "- o/r#12: S-007-1, S-007-2",
            "- o/r#13: S-007-2, S-007-9",
            "- o/r#14",
            "- o/r#12: S-006-3",
            "- #15: S-007-x",
        )
    )
    root = repo(tmp_path, **{"007-dry-run.md": spec("007", criteria=FOUR, issues=lines)})
    out = specs(root, "check").stdout
    assert "S-007-2 is in both o/r#12 and o/r#13" in out
    assert "o/r#13 names S-007-9, which the spec lacks" in out
    assert "o/r#14 names no criteria" in out
    assert "lists o/r#12 twice" in out
    assert "names S-006-3, a criterion of another spec" in out
    assert "'S-007-x' is not a criterion id" in out
    assert "S-007-3 is in no build issue" in out
    assert "S-007-4 is in no build issue" in out


def test_check_waits_for_the_split_and_ignores_drafts(tmp_path: Path) -> None:
    approved = spec("007", issues="")
    draft = spec("008", status="draft", issues="- o/r#3: S-008-1")
    root = repo(tmp_path, **{"007-dry-run.md": approved, "008-draft.md": draft})
    out = specs(root, "check")
    assert (out.returncode, out.stdout) == (0, "")


def test_split_puts_every_criterion_in_one_issue_by_default(tmp_path: Path) -> None:
    root = repo(tmp_path, **{"007-dry-run.md": spec("007", issues="")})
    out = specs(root, "split", "7")
    assert out.returncode == 0, out.stderr
    assert "=== B1 (depends on nothing)\ntitle: Dry run (S-007-1, S-007-2)\n" in out.stdout
    assert "- S-007-1: tool run exits 2 on a missing config\n- S-007-2: tool run --dry-run" in out.stdout
    assert "Depends on" not in out.stdout
    assert out.stdout.endswith("filed:\n\n- #{B1}: S-007-1, S-007-2\n")


def test_split_groups_criteria_into_a_graph_that_passes_check(tmp_path: Path) -> None:
    root = repo(tmp_path, **{"007-dry-run.md": spec("007", criteria=FOUR, issues="")})
    groups = ("--group", "1,S-007-2", "--group", "3", "--group", "4")
    after = ("--after", "2:1", "--after", "3:1", "--after", "3:2")
    out = specs(root, "split", "--json", "--repo", "o/r", *groups, *after, "007")
    assert out.returncode == 0, out.stderr
    graph = json.loads(out.stdout)
    assert [i["criteria"] for i in graph["issues"]] == [["S-007-1", "S-007-2"], ["S-007-3"], ["S-007-4"]]
    assert [i["depends_on"] for i in graph["issues"]] == [[], ["B1"], ["B1", "B2"]]
    assert graph["issues"][2]["body"].endswith("\n\nDepends on o/r#{B1}\nDepends on o/r#{B2}\n")
    # The triage agent files them in order, replacing each key with the number it got
    lines = [i["issues_line"].replace("{" + i["key"] + "}", str(20 + k)) for k, i in enumerate(graph["issues"], 1)]
    assert lines == ["- o/r#21: S-007-1, S-007-2", "- o/r#22: S-007-3", "- o/r#23: S-007-4"]
    (root / "docs" / "specs" / "007-dry-run.md").write_text(spec("007", criteria=FOUR, issues="\n".join(lines)))
    assert specs(root, "check").returncode == 0
    assert specs(root, "split", "7").stdout == "S-007: every criterion is in a build issue already\n"
    assert "nothing to group" in specs(root, "split", "7", "--group", "1").stderr


def test_split_proposes_only_the_criteria_no_issue_has(tmp_path: Path) -> None:
    root = repo(tmp_path, **{"007-dry-run.md": spec("007", criteria=FOUR, issues="- o/r#21: S-007-1, S-007-2")})
    assert specs(root, "check").returncode == 1  # until the rest is filed
    out = specs(root, "split", "7")
    assert out.returncode == 0, out.stderr
    assert "title: Dry run (S-007-3, S-007-4)\n" in out.stdout
    assert "already in o/r#21" in specs(root, "split", "7", "--group", "1", "--group", "3,4").stderr


def test_split_refuses_a_bad_graph(tmp_path: Path) -> None:
    approved = spec("007", criteria=FOUR, issues="")
    root = repo(tmp_path, **{"007-dry-run.md": approved, "008-draft.md": spec("008", status="draft")})
    cases = {
        ("8",): "S-008 is draft; only an approved spec is split",
        ("7", "--group", "1,2"): "S-007-3, S-007-4 in no --group",
        ("7", "--group", "1,2,3", "--group", "3,4"): "S-007-3 is in both --group 1 and --group 2",
        ("7", "--group", "1,2,3,4,5"): "S-007 has no criterion S-007-5",
        ("7", "--group", "1,2", "--group", "3,4", "--after", "1:2"): "depends only on an earlier group",
        ("7", "--group", "1,2", "--group", "3,4", "--after", "2:3"): "numbered 1 to 2",
        ("7", "--after", "2-1"): "--after is B:A",
        ("7", "--repo", "r"): "--repo must be owner/repo",
        ("7", "--group", "S-008-1"): "named k or S-007-k",
    }
    for args, message in cases.items():
        out = specs(root, "split", *args)
        assert (out.returncode, message in out.stderr) == (2, True), (args, out.stderr)
    broken = spec("007", criteria=("1: a", "3: c"), issues="")
    (root / "docs" / "specs" / "007-dry-run.md").write_text(broken)
    out = specs(root, "split", "7")
    assert out.returncode == 1
    assert "must pass check before it is split" in out.stderr


# A dropped criterion (#219) keeps its id, needs no test, and no build issue delivers it

DROPPED = ("1: dropped in #81, see S-007-3", "2: b", "3: c", "4: the flag is dropped in #9 of the output")


def test_a_dropped_criterion_needs_no_test_but_a_mention_of_dropped_does(tmp_path: Path) -> None:
    verified = "\n".join(("- S-007-1: dropped in #81", "- S-007-2: ran it", "- S-007-3: ran it", "- S-007-4: ran it"))
    issues = "- o/r#21: S-007-2, S-007-3, S-007-4"
    built_spec = spec("007", criteria=DROPPED, issues=issues, status="built", verification=verified)
    root = repo(tmp_path, **{"007-dry-run.md": built_spec})
    assert specs(root, "check").stdout == ""
    write(root, "tests/test_run.py", "# proves: S-007-2, S-007-3", "def test_x(): pass")
    out = specs(root, "coverage")
    assert out.returncode == 1
    assert "  S-007-1: dropped in #81, see S-007-3\n    dropped, no test needed\n" in out.stdout
    assert "  S-007-4: the flag is dropped in #9 of the output\n    no test proves it\n" in out.stdout
    assert specs(root, "criteria", "7").stdout.startswith("S-007-1: dropped in #81, see S-007-3\n")


def test_split_leaves_out_a_dropped_criterion(tmp_path: Path) -> None:
    root = repo(tmp_path, **{"007-dry-run.md": spec("007", criteria=DROPPED, issues="")})
    out = specs(root, "split", "--json", "7")
    assert out.returncode == 0, out.stderr
    assert [i["criteria"] for i in json.loads(out.stdout)["issues"]] == [["S-007-2", "S-007-3", "S-007-4"]]
    assert "S-007-1 is dropped" in specs(root, "split", "7", "--group", "1,2", "--group", "3,4").stderr
    # Filed without it, the spec passes check, and split has nothing left to propose
    filed = spec("007", criteria=DROPPED, issues="- o/r#21: S-007-2, S-007-3, S-007-4")
    (root / "docs" / "specs" / "007-dry-run.md").write_text(filed)
    assert specs(root, "check").stdout == ""
    assert specs(root, "split", "7").stdout == "S-007: every criterion is in a build issue already\n"
    # A criterion dropped after the split may stay in its build issue
    (root / "docs" / "specs" / "007-dry-run.md").write_text(spec("007", criteria=DROPPED))
    assert specs(root, "check").stdout == ""
