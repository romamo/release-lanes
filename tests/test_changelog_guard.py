"""The github-pr-triage skill's CHANGELOG guard, on real rebases in a scratch repo"""

import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "skills" / "github-pr-triage" / "scripts" / "changelog_guard.py"

TOP = "# Changelog\n\n## [Unreleased]\n\n"
OLD = "## [0.5.1] - 2026-10-01\n\n### Fixed\n\n- Z\n"
BEFORE = TOP + "### Fixed\n\n- A\n\n" + OLD
# The release moved Unreleased into 0.5.2
RELEASED = TOP + "## [0.5.2] - 2026-10-04\n\n### Fixed\n\n- A\n\n" + OLD
# The PR, cut before the release, adds a Fixed and an Added entry
PR = TOP + "### Added\n\n- C\n  more of C\n\n### Fixed\n\n- A\n- B\n\n" + OLD


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout


def guard(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, str(SCRIPT), *args], cwd=cwd, capture_output=True, text=True, check=False)


def scratch(tmp_path: Path, base: str) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    git(root, "config", "user.email", "t@example.com")
    git(root, "config", "user.name", "t")
    (root / "CHANGELOG.md").write_text(base)
    git(root, "add", "CHANGELOG.md")
    git(root, "commit", "-q", "-m", "base")
    return root


def commit(root: Path, text: str, message: str) -> None:
    (root / "CHANGELOG.md").write_text(text)
    git(root, "commit", "-q", "-am", message)


def released_with_pr(tmp_path: Path, pr: str) -> Path:
    """main released 0.5.2 after the PR branched; the PR is checked out, not yet rebased"""
    root = scratch(tmp_path, BEFORE)
    git(root, "switch", "-q", "-c", "pr")
    commit(root, pr, "pr")
    git(root, "switch", "-q", "main")
    commit(root, RELEASED, "release 0.5.2")
    git(root, "switch", "-q", "pr")
    return root


def rebase(root: Path) -> bool:
    """True when the rebase stopped on a conflict"""
    return subprocess.run(["git", "rebase", "main"], cwd=root, capture_output=True, check=False).returncode != 0


def section(text: str, heading: str) -> str:
    start = text.index(heading)
    end = text.find("\n## ", start + 1)
    return text[start : end + 1 if end != -1 else len(text)]


def test_union_keeps_a_blank_line_under_a_release_heading_and_move_fixes_the_rest(tmp_path: Path) -> None:
    root = released_with_pr(tmp_path, PR)
    assert rebase(root)  # the #21 shape: the release heading against the new Added block
    assert guard(root, "union", "CHANGELOG.md").returncode == 0
    text = (root / "CHANGELOG.md").read_text()
    assert "## [0.5.2] - 2026-10-04\n\n### Added\n" in text

    check = guard(root, "check", "--base", "main")
    assert check.returncode == 1
    assert "added outside Unreleased" in check.stdout

    move = guard(root, "move", "--base", "main")
    assert move.returncode == 0, move.stderr
    assert "Added (1), Fixed (1)" in move.stdout
    text = (root / "CHANGELOG.md").read_text()
    assert section(text, "## [Unreleased]") == (
        "## [Unreleased]\n\n### Added\n\n- C\n  more of C\n\n### Fixed\n\n- B\n\n"
    )
    assert section(text, "## [0.5.2]") == section(RELEASED, "## [0.5.2]")
    assert text[text.index("## [0.5.2]") :] == RELEASED[RELEASED.index("## [0.5.2]") :]
    assert guard(root, "check", "--base", "main").returncode == 0


def test_move_carries_entries_into_a_new_and_an_existing_heading_in_order(tmp_path: Path) -> None:
    # Unreleased already has Fixed and Security; the release section gets an Added entry
    # under a new heading and a Fixed one under its own heading, with no conflict
    base = TOP + "### Fixed\n\n- E\n\n### Security\n\n- S\n\n## [0.5.2] - 2026-10-04\n\n### Fixed\n\n- A\n\n" + OLD
    root = scratch(tmp_path, base)
    misplaced = base.replace("### Fixed\n\n- A\n", "### Added\n\n- C\n\n### Fixed\n\n- A\n- B\n  of B\n")
    (root / "CHANGELOG.md").write_text(misplaced)

    assert guard(root, "move", "--base", "HEAD").returncode == 0
    text = (root / "CHANGELOG.md").read_text()
    assert section(text, "## [Unreleased]") == (
        "## [Unreleased]\n\n### Added\n\n- C\n\n### Fixed\n\n- E\n- B\n  of B\n\n### Security\n\n- S\n\n"
    )
    assert text == base.replace("## [Unreleased]\n\n", "## [Unreleased]\n\n### Added\n\n- C\n\n", 1).replace(
        "- E\n", "- E\n- B\n  of B\n", 1
    )
    assert guard(root, "check", "--base", "HEAD").returncode == 0


def test_move_puts_an_unknown_heading_after_the_known_ones(tmp_path: Path) -> None:
    base = TOP + "## [0.5.2] - 2026-10-04\n\n### Fixed\n\n- A\n\n" + OLD
    root = scratch(tmp_path, base)
    (root / "CHANGELOG.md").write_text(base.replace("### Fixed\n\n- A\n", "### Docs\n\n- D\n\n### Fixed\n\n- A\n- B\n"))

    assert guard(root, "move", "--base", "HEAD").returncode == 0
    text = (root / "CHANGELOG.md").read_text()
    assert section(text, "## [Unreleased]") == "## [Unreleased]\n\n### Fixed\n\n- B\n\n### Docs\n\n- D\n\n"
    assert guard(root, "check", "--base", "HEAD").returncode == 0


def test_move_without_headings_keeps_moving_one_block(tmp_path: Path) -> None:
    base = "# Changelog\n\n## [Unreleased]\n\n## [1.0.0] - 2026-10-01\n\n- A\n"
    root = scratch(tmp_path, base)
    (root / "CHANGELOG.md").write_text(base + "- B\n- C\n")

    move = guard(root, "move", "--base", "HEAD")
    assert move.returncode == 0
    assert move.stdout == "moved 2 line(s) under Unreleased\n"
    assert (root / "CHANGELOG.md").read_text() == (
        "# Changelog\n\n## [Unreleased]\n\n- B\n- C\n\n## [1.0.0] - 2026-10-01\n\n- A\n"
    )
    assert guard(root, "check", "--base", "HEAD").returncode == 0


def test_move_refuses_a_line_that_continues_a_released_entry(tmp_path: Path) -> None:
    base = TOP + "## [0.5.2] - 2026-10-04\n\n### Fixed\n\n- A\n\n" + OLD
    root = scratch(tmp_path, base)
    (root / "CHANGELOG.md").write_text(base.replace("- A\n", "- A\n  now longer\n", 1))

    move = guard(root, "move", "--base", "HEAD")
    assert move.returncode == 2
    assert "not whole entries" in move.stderr


def test_check_fails_on_a_headless_bullet_under_unreleased(tmp_path: Path) -> None:
    base = TOP + "## [0.5.2] - 2026-10-04\n\n### Fixed\n\n- A\n"
    root = scratch(tmp_path, base)
    (root / "CHANGELOG.md").write_text(base.replace(TOP, TOP + "- B\n\n"))

    check = guard(root, "check", "--base", "HEAD")
    assert check.returncode == 1
    assert "bullet with no ### heading under Unreleased, line 5: - B" in check.stdout


def test_check_fails_on_a_duplicate_heading_under_unreleased(tmp_path: Path) -> None:
    base = TOP + "### Added\n\n- A\n\n## [0.5.2] - 2026-10-04\n\n### Fixed\n\n- Z\n"
    root = scratch(tmp_path, base)
    (root / "CHANGELOG.md").write_text(base.replace("- A\n", "- A\n\n### Added\n\n- B\n"))

    check = guard(root, "check", "--base", "HEAD")
    assert check.returncode == 1
    assert "duplicate heading under Unreleased, line 9: ### Added (first at line 5)" in check.stdout


def test_check_without_any_headings_accepts_bullets(tmp_path: Path) -> None:
    base = "# Changelog\n\n## [Unreleased]\n\n## [1.0.0] - 2026-10-01\n\n- A\n"
    root = scratch(tmp_path, base)
    (root / "CHANGELOG.md").write_text(base.replace("## [Unreleased]\n\n", "## [Unreleased]\n\n- B\n\n"))

    assert guard(root, "check", "--base", "HEAD").returncode == 0


def test_union_of_plain_bullets_adds_no_blank_line(tmp_path: Path) -> None:
    path = tmp_path / "CHANGELOG.md"
    path.write_text("### Fixed\n\n<<<<<<< ours\n- A\n=======\n- B\n>>>>>>> theirs\n")

    assert guard(tmp_path, "union", "CHANGELOG.md").returncode == 0
    assert path.read_text() == "### Fixed\n\n- A\n- B\n"
