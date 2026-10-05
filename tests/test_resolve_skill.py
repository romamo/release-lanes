"""github-issue-resolve branches in a worktree, never in the checkout it starts in (#110)"""

from __future__ import annotations

import re
from pathlib import Path

SKILL = Path(__file__).resolve().parents[1] / "skills" / "github-issue-resolve" / "SKILL.md"


def phase(text: str, number: int) -> str:
    return text.split(f"\n## Phase {number}:", 1)[1].split("\n## ", 1)[0]


def test_phase_3_creates_the_branch_in_a_worktree() -> None:
    section = phase(SKILL.read_text(encoding="utf-8"), 3)
    assert "git worktree add -b fix/<short-slug> tmp/wt-<short-slug> origin/<default>" in section
    assert "Never switch, reset, or pull the user's checkout" in section


def test_phase_3_branches_in_place_only_inside_a_linked_worktree() -> None:
    section = phase(SKILL.read_text(encoding="utf-8"), 3)
    assert "git checkout -b" not in section
    in_place = [line for line in section.splitlines() if re.search(r"git (checkout -b|switch -c)", line)]
    assert in_place, "the linked-worktree case names its in-place command"
    for line in in_place:
        assert "--git-common-dir" in line, f"in-place branching outside the linked-worktree case: {line}"


def test_the_skill_never_branches_in_place_in_a_code_block() -> None:
    blocks = re.findall(r"```bash\n(.*?)```", SKILL.read_text(encoding="utf-8"), re.S)
    for block in blocks:
        assert not re.search(r"git (checkout -b|switch -c|checkout |switch |reset )", block), block


def test_the_worktree_is_removed_only_after_landed_confirms_the_merge() -> None:
    section = phase(SKILL.read_text(encoding="utf-8"), 7)
    landed = section.index("landed.py --onto origin/<default>")
    assert landed < section.index("git worktree remove tmp/wt-<short-slug>")
    assert landed < section.index("git branch -D fix/<short-slug>")
