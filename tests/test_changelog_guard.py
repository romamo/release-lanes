"""The github-pr-triage skill's CHANGELOG guard, on real rebases in a scratch repo"""

import importlib.util
import re
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path
from types import ModuleType

import pytest

from shipmill.errors import ReleaseError
from shipmill.fragments import InCheckout, read
from shipmill.policy import Style

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


def test_move_refuses_when_a_released_line_was_removed(tmp_path: Path) -> None:
    # With no newline at the end of the base, git reports its last released entry as removed and
    # re-added; moving the "added" copy would take that released entry out of its section
    base = TOP + "## [0.5.2] - 2026-10-04\n\n### Fixed\n\n- A"
    root = scratch(tmp_path, base)
    edited = base + "\n- B\n"
    (root / "CHANGELOG.md").write_text(edited)

    move = guard(root, "move", "--base", "HEAD")
    assert move.returncode == 2
    assert "removed from a released section" in move.stderr
    assert (root / "CHANGELOG.md").read_text() == edited


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


REL = "## [0.5.2] - 2026-10-04\n\n"
# The PR, cut before 0.5.2, adds an Added heading and entry above Unreleased's Fixed
SYNC_BEFORE = TOP + "### Fixed\n\n- A\n\n" + OLD
SYNC_PR = TOP + "### Added\n\n- C\n\n### Fixed\n\n- A\n\n" + OLD


def synced_with_pr(tmp_path: Path, *mains: str) -> Path:
    """main moved through `mains` after the PR branched; the PR is checked out, not yet rebased"""
    root = scratch(tmp_path, SYNC_BEFORE)
    git(root, "switch", "-q", "-c", "pr")
    commit(root, SYNC_PR, "pr")
    git(root, "switch", "-q", "main")
    for n, text in enumerate(mains):
        commit(root, text, f"main {n}")
    git(root, "switch", "-q", "pr")
    return root


def released_tail(text: str) -> str:
    return text[text.index("## [0.5.2]") :]


def test_move_puts_a_bullet_whose_heading_union_absorbed_under_that_heading(tmp_path: Path) -> None:
    # The #82 shape: 0.5.2 synced into main, then an Added entry landed in Unreleased. Git moves
    # the shared "### Added" line out of the conflict block, so after union the PR's entry sits
    # right under the release heading, with no heading of its own
    released = TOP + REL + "### Fixed\n\n- A\n\n" + OLD
    main = TOP + "### Added\n\n- X\n\n" + REL + "### Fixed\n\n- A\n\n" + OLD
    root = synced_with_pr(tmp_path, released, main)
    assert rebase(root)
    assert guard(root, "union", "CHANGELOG.md").returncode == 0
    assert "## [0.5.2] - 2026-10-04\n\n- C\n\n### Fixed\n" in (root / "CHANGELOG.md").read_text()

    move = guard(root, "move", "--base", "main")
    assert move.returncode == 0, move.stderr
    assert "Added (1)" in move.stdout
    text = (root / "CHANGELOG.md").read_text()
    assert section(text, "## [Unreleased]") == "## [Unreleased]\n\n### Added\n\n- X\n- C\n\n"
    assert released_tail(text) == released_tail(main)
    assert guard(root, "check", "--base", "main").returncode == 0


def test_move_after_a_release_sync_creates_the_heading_in_an_empty_unreleased(tmp_path: Path) -> None:
    # Unreleased is empty after the sync and the release has Added and Fixed: the entry
    # goes under a new Added heading in Unreleased
    main = TOP + "### Added\n\n- W\n\n### Fixed\n\n- A\n\n" + OLD
    released = TOP + REL + "### Added\n\n- W\n\n### Fixed\n\n- A\n\n" + OLD
    root = synced_with_pr(tmp_path, main, released)
    assert rebase(root)
    assert guard(root, "union", "CHANGELOG.md").returncode == 0

    move = guard(root, "move", "--base", "main")
    assert move.returncode == 0, move.stderr
    text = (root / "CHANGELOG.md").read_text()
    assert section(text, "## [Unreleased]") == "## [Unreleased]\n\n### Added\n\n- C\n\n"
    assert released_tail(text) == released_tail(released)
    assert guard(root, "check", "--base", "main").returncode == 0


def test_move_creates_an_absorbed_heading_missing_from_unreleased_in_order(tmp_path: Path) -> None:
    # The absorbed shape one release down: the entry sits right under 0.5.1, and the nearest
    # heading above it is 0.5.2's Added, which Unreleased does not have yet
    base = TOP + "### Fixed\n\n- E\n\n" + REL + "### Added\n\n- W\n\n" + OLD
    root = scratch(tmp_path, base)
    (root / "CHANGELOG.md").write_text(base.replace(OLD, OLD.replace("01\n\n", "01\n\n- C\n\n", 1)))

    move = guard(root, "move", "--base", "HEAD")
    assert move.returncode == 0, move.stderr
    assert (root / "CHANGELOG.md").read_text() == base.replace(TOP, TOP + "### Added\n\n- C\n\n", 1)
    assert guard(root, "check", "--base", "HEAD").returncode == 0


def test_move_refuses_an_entry_with_no_heading_anywhere_above(tmp_path: Path) -> None:
    base = TOP + REL + "### Fixed\n\n- A\n\n" + OLD
    root = scratch(tmp_path, base)
    misplaced = base.replace(REL, REL + "- C\n\n", 1)
    (root / "CHANGELOG.md").write_text(misplaced)

    move = guard(root, "move", "--base", "HEAD")
    assert move.returncode == 2
    assert "no ### heading above them" in move.stderr
    assert (root / "CHANGELOG.md").read_text() == misplaced


# Changelog fragments (spec 013): with [changelog] fragments set, a PR's entry goes in a new
# fragment, so check fails a line added under Unreleased and a fragment that fails to read

FOLDER = "changelog.d"
FRAGMENTS_README = "# Changelog fragments\n\nOne file per pull request.\n"
# arrays, an array of tables, and comments: the plain form Python 3.10 reads skips all of them
CONFIG = """\
name = "demo"  # the repo
mode = "release"

[changelog]
style = "keep-a-changelog"  # as the CHANGELOG
fragments = 'changelog.d'

[bump]
minor = ["Added", "Changed"]

[[version_lines]]
file = "README.md"
pattern = '[changelog]'
"""
GOOD = "### Added\n\n- Feature (#12)\n  more of it\n\n### Fixed\n\n- A fix (#12)\n"
# the fragments shipmill's reader refuses (tests/test_fragments.py, S-013-3), with its words
BAD = [
    ("1-empty.md", "\n\n", "changelog.d/1-empty.md: an empty fragment"),
    ("2-prose.md", "### Added\n\n- Feature A (#2)\n\nSome prose\n", "changelog.d/2-prose.md:5: text outside"),
    ("3-bare.md", "- Feature A (#3)\n", "changelog.d/3-bare.md:1: an entry has no '### ' category heading"),
    ("4-version.md", "## [Unreleased]\n\n### Added\n\n- A (#4)\n", "changelog.d/4-version.md:1: a '## ' heading"),
    ("sub/5-nested.md", "### Added\n\n- A (#5)\n", "changelog.d/sub/5-nested.md: a fragment sits right in"),
    ("6-text.txt", "### Added\n\n- A (#6)\n", "changelog.d/6-text.txt: not a fragment"),
    ("7-heading-only.md", "### Added\n", "changelog.d/7-heading-only.md: the fragment holds no entry"),
    ("8-indented.md", "### Added\n\n  stray\n", "changelog.d/8-indented.md:3: text outside a '- ' entry"),
]


def with_fragments(tmp_path: Path, config: str = CONFIG) -> Path:
    """A repo whose config sets [changelog] fragments, on a PR branch named after its issue"""
    root = scratch(tmp_path, BEFORE)
    (root / ".github").mkdir()
    (root / ".github" / "shipmill.toml").write_text(config)
    (root / FOLDER).mkdir()
    (root / FOLDER / "README.md").write_text(FRAGMENTS_README)
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "Use changelog fragments")
    git(root, "switch", "-q", "-c", "fix/12-a-fix")
    return root


def add(root: Path, name: str, text: str) -> None:
    path = root / FOLDER / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", f"Add {name}")


def guard_310(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """The guard on Python 3.10, as a skill may run it, with no tomllib"""
    uv = shutil.which("uv")
    assert uv is not None, "uv runs the skill scripts"
    command = [uv, "run", "--no-project", "--python", "3.10", "python", str(SCRIPT), *args]
    return subprocess.run(command, cwd=cwd, capture_output=True, text=True, check=False)


def guard_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("changelog_guard", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_s013_14_a_line_added_under_unreleased_fails_naming_the_fragment(tmp_path: Path) -> None:
    root = with_fragments(tmp_path)
    commit(root, BEFORE.replace("- A\n", "- A\n- B (#12)\n"), "entry under Unreleased")
    done = guard(root, "check", "--base", "main")
    assert done.returncode == 1, done.stdout + done.stderr
    assert "added under Unreleased, line 8: - B (#12)" in done.stdout
    assert "move the entry into a new fragment, changelog.d/12-a-fix.md," in done.stdout


def test_s013_14_rewording_an_unreleased_entry_in_place_fails(tmp_path: Path) -> None:
    # #304: an edit is a removed line plus an added one, and the added one fails
    root = with_fragments(tmp_path)
    commit(root, BEFORE.replace("- A\n", "- A, reworded\n"), "reword an Unreleased entry")
    done = guard(root, "check", "--base", "main")
    assert done.returncode == 1, done.stdout + done.stderr
    assert "added under Unreleased, line 7: - A, reworded" in done.stdout


def test_s013_14_moving_an_unreleased_entry_into_a_fragment_passes(tmp_path: Path) -> None:
    # #304: the skills tell a reviewer to fix stale Unreleased wording this way
    root = with_fragments(tmp_path)
    commit(root, BEFORE.replace("### Fixed\n\n- A\n\n", ""), "move A out of Unreleased")
    add(root, "12-a-fix.md", "### Fixed\n\n- A, reworded\n")
    done = guard(root, "check", "--base", "main")
    assert done.returncode == 0, done.stdout + done.stderr
    assert "ok: no entry added under Unreleased; 1 fragment(s) added or changed in changelog.d" in done.stdout


def test_s013_14_a_well_formed_fragment_passes(tmp_path: Path) -> None:
    root = with_fragments(tmp_path)
    add(root, "12-a-fix.md", GOOD)
    (root / FOLDER / "13-untracked.md").write_text("### Fixed\n\n- Another (#13)\n")
    done = guard(root, "check", "--base", "main")
    assert done.returncode == 0, done.stdout + done.stderr
    assert "2 fragment(s) added or changed in changelog.d, each reads" in done.stdout


@pytest.mark.parametrize(("name", "text", "error"), BAD)
def test_s013_14_a_fragment_that_fails_to_read_fails_as_shipmill_reads_it(
    tmp_path: Path, name: str, text: str, error: str
) -> None:
    root = with_fragments(tmp_path)
    add(root, name, text)
    done = guard(root, "check", "--base", "main")
    assert done.returncode == 1, done.stdout + done.stderr
    assert error in done.stdout
    # shipmill's reader refuses the same file in the same words
    with pytest.raises(ReleaseError, match=f"^{re.escape(error)}"):
        read(InCheckout(root), FOLDER, Style.KEEP_A_CHANGELOG)


def test_s013_14_dash_style_fragments_read_as_shipmill_reads_them(tmp_path: Path) -> None:
    root = with_fragments(tmp_path, CONFIG.replace('"keep-a-changelog"', '"dash"'))
    add(root, "12-good.md", "### A titled entry (#12)\n\nIts body.\n")
    assert guard(root, "check", "--base", "main").returncode == 0
    add(root, "13-bad.md", "Prose first\n\n### A titled entry (#13)\n")
    done = guard(root, "check", "--base", "main")
    error = "changelog.d/13-bad.md:1: text outside a '### ' entry: 'Prose first'"
    assert done.returncode == 1 and error in done.stdout
    with pytest.raises(ReleaseError, match=f"^{re.escape(error)}"):
        read(InCheckout(root), FOLDER, Style.DASH)


def test_s013_14_without_fragments_a_line_under_unreleased_still_passes(tmp_path: Path) -> None:
    root = with_fragments(tmp_path, CONFIG.replace("fragments = 'changelog.d'\n", ""))
    commit(root, BEFORE.replace("- A\n", "- A\n- B (#12)\n"), "entry under Unreleased")
    (root / FOLDER / "1-empty.md").write_text("\n")  # not read: the config sets no fragments
    done = guard(root, "check", "--base", "main")
    assert done.returncode == 0, done.stdout + done.stderr
    assert done.stdout.startswith("ok: 1 added line(s), all under Unreleased")


def test_s013_14_a_missing_folder_or_a_bad_setting_is_bad_input(tmp_path: Path) -> None:
    root = with_fragments(tmp_path)
    git(root, "rm", "-q", "-r", FOLDER)
    done = guard(root, "check", "--base", "main")
    assert done.returncode == 2 and "[changelog] fragments names changelog.d, which is not a folder" in done.stderr
    (root / ".github" / "shipmill.toml").write_text(CONFIG.replace("'changelog.d'", "'../out'"))
    done = guard(root, "check", "--base", "main")
    assert done.returncode == 2 and "fragments is a folder relative to the repo root" in done.stderr


def test_s013_14_on_python_3_10(tmp_path: Path) -> None:
    root = with_fragments(tmp_path)
    add(root, "12-a-fix.md", GOOD)
    done = guard_310(root, "check", "--base", "main")
    assert done.returncode == 0, done.stdout + done.stderr
    add(root, "3-bare.md", "- Feature A (#3)\n")
    commit(root, BEFORE.replace("- A\n", "- A\n- B (#12)\n"), "entry under Unreleased")
    done = guard_310(root, "check", "--base", "main")
    assert done.returncode == 1, done.stdout + done.stderr
    assert "added under Unreleased, line 8: - B (#12)" in done.stdout
    assert "changelog.d/3-bare.md:1: an entry has no '### ' category heading" in done.stdout


def test_the_plain_form_reads_the_changelog_table_as_tomllib_does() -> None:
    """Python 3.10's reader, on this example and on shipmill's own config"""
    own = (SCRIPT.parents[3] / ".github" / "shipmill.toml").read_text()
    for text in (CONFIG, own):
        assert guard_module().plain_changelog(text, "config") == tomllib.loads(text)["changelog"]


@pytest.mark.parametrize("header", ['["changelog"]', "['changelog']", '[changelog."x"]'])
def test_the_plain_form_refuses_a_header_it_cannot_read(header: str, capsys: pytest.CaptureFixture[str]) -> None:
    """A quoted header was skipped, so ["changelog"]'s fragments read as unset on Python 3.10"""
    text = f'[bump]\nfrom = "headings"\n{header}\nstyle = "dash"\nfragments = "changelog.d"\n'
    with pytest.raises(SystemExit) as exited:
        guard_module().plain_changelog(text, "config")
    assert exited.value.code == 2
    assert f"config: line 3: can't read the header {header!r} on Python 3.10" in capsys.readouterr().err


def test_s013_15_two_branches_that_each_add_a_fragment_merge_with_no_conflict(tmp_path: Path) -> None:
    root = with_fragments(tmp_path)
    git(root, "switch", "-q", "-c", "a", "main")
    add(root, "20-one.md", "### Added\n\n- One (#20)\n")
    git(root, "switch", "-q", "-c", "b", "main")
    add(root, "21-two.md", "### Added\n\n- Two (#21)\n")
    git(root, "merge", "-q", "--no-edit", "a")  # b takes a
    git(root, "switch", "-q", "a")
    git(root, "merge", "-q", "--no-edit", "b")  # and a takes b back
    assert sorted(p.name for p in (root / FOLDER).iterdir()) == ["20-one.md", "21-two.md", "README.md"]
    # the same two entries written under Unreleased conflict: what fragments avoid
    git(root, "switch", "-q", "-c", "c", "main")
    commit(root, BEFORE.replace("- A\n", "- A\n- One (#20)\n"), "one")
    git(root, "switch", "-q", "-c", "d", "main")
    commit(root, BEFORE.replace("- A\n", "- A\n- Two (#21)\n"), "two")
    merged = subprocess.run(["git", "merge", "--no-edit", "c"], cwd=root, capture_output=True, text=True, check=False)
    assert merged.returncode != 0 and "CONFLICT" in merged.stdout
