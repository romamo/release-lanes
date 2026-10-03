#!/usr/bin/env python3
"""CHANGELOG safety for rebases (Keep a Changelog layout, `## [Unreleased]` then `## [X]`).

check --base REF   exit 1 if lines added (or removed) since REF fall outside Unreleased
move --base REF    move lines added since REF that sit in a released section under Unreleased
union FILE         resolve conflict blocks by keeping both sides, ours first

Exit codes: 0 ok; 1 a problem was found (check) or nothing to do (move, union);
2 bad input, such as no Unreleased heading or malformed conflict markers.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

UNRELEASED = "## [Unreleased]"
RELEASE = re.compile(r"^## \[")
HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def fail(message: str) -> None:
    print(f"changelog_guard: {message}", file=sys.stderr)
    sys.exit(2)


def unreleased_range(lines: list[str]) -> tuple[int, int]:
    """1-based, inclusive start and exclusive end of the Unreleased section"""
    starts = [i for i, line in enumerate(lines, 1) if line.rstrip() == UNRELEASED]
    if len(starts) != 1:
        fail(f"expected one '{UNRELEASED}' heading, found {len(starts)}")
    start = starts[0]
    end = next((i for i, line in enumerate(lines, 1) if i > start and RELEASE.match(line)), len(lines) + 1)
    return start, end


def diff_changes(base: str, path: Path) -> tuple[list[int], list[int]]:
    """New-file line numbers of added lines, and old-file line numbers of removed ones"""
    proc = subprocess.run(
        ["git", "diff", "-U0", base, "--", str(path)], capture_output=True, text=True, cwd=path.parent
    )
    if proc.returncode != 0:
        fail(f"git diff failed: {proc.stderr.strip()}")
    added: list[int] = []
    removed: list[int] = []
    old = new = 0
    for line in proc.stdout.splitlines():
        if m := HUNK.match(line):
            old, new = int(m.group(1)), int(m.group(3))
        elif line.startswith("+") and not line.startswith("+++"):
            added.append(new)
            new += 1
        elif line.startswith("-") and not line.startswith("---"):
            removed.append(old)
            old += 1
    return added, removed


def base_lines(base: str, path: Path) -> list[str]:
    top = subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True, cwd=path.parent)
    if top.returncode != 0:
        fail(f"not in a git checkout: {top.stderr.strip()}")
    rel = path.resolve().relative_to(Path(top.stdout.strip()).resolve()).as_posix()
    proc = subprocess.run(["git", "show", f"{base}:{rel}"], capture_output=True, text=True, cwd=path.parent)
    if proc.returncode != 0:
        fail(f"git show {base}:{rel} failed: {proc.stderr.strip()}")
    return proc.stdout.splitlines(keepends=True)


def cmd_check(path: Path, base: str, allow_released_edits: bool) -> int:
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    start, end = unreleased_range(lines)
    added, removed = diff_changes(base, path)
    old = base_lines(base, path)
    # Right after a release the base has no Unreleased heading: every base line is released,
    # and the heading itself is one of the added lines
    has_unreleased = any(line.rstrip() == UNRELEASED for line in old)
    first = start + 1 if has_unreleased else start
    outside = [n for n in added if not first <= n < end]
    old_start, old_end = unreleased_range(old) if has_unreleased else (0, 0)
    edited = [] if allow_released_edits else [n for n in removed if not old_start < n < old_end]
    for n in outside:
        print(f"added outside Unreleased, line {n}: {lines[n - 1].rstrip()}")
    for n in edited:
        print(f"removed from a released section, {base} line {n}")
    if outside or edited:
        return 1
    print(f"ok: {len(added)} added line(s), all under Unreleased (lines {start}-{end - 1})")
    return 0


def cmd_move(path: Path, base: str) -> int:
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    start, end = unreleased_range(lines)
    added, _ = diff_changes(base, path)
    outside = [n for n in added if not start < n < end]
    if not outside:
        print("nothing to move: every added line is under Unreleased")
        return 1
    if outside != list(range(outside[0], outside[-1] + 1)):
        fail(f"added lines outside Unreleased are not one block ({outside}); move them by hand")
    block = lines[outside[0] - 1 : outside[-1]]
    rest = lines[: outside[0] - 1] + lines[outside[-1] :]
    while block and not block[0].strip():
        block.pop(0)
    while block and not block[-1].strip():
        block.pop()
    heading = start - 1  # 0-based; the moved lines were below it, so it has not shifted
    if heading + 1 < len(rest) and not rest[heading + 1].strip():
        insert_at, moved = heading + 2, block + ["\n"]  # after the heading's blank line
    else:
        insert_at, moved = heading + 1, ["\n"] + block + ["\n"]
    path.write_text("".join(rest[:insert_at] + moved + rest[insert_at:]), encoding="utf-8")
    print(f"moved {len(block)} line(s) under Unreleased")
    return 0


def cmd_union(path: Path) -> int:
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    out: list[str] = []
    blocks = 0
    i = 0
    while i < len(lines):
        if not lines[i].startswith("<<<<<<< "):
            # a bare ======= outside a block is a Markdown heading underline, not a marker
            if lines[i].startswith((">>>>>>> ", "||||||| ")):
                fail(f"stray conflict marker at line {i + 1}")
            out.append(lines[i])
            i += 1
            continue
        ours: list[str] = []
        theirs: list[str] = []
        section = ours
        j = i + 1
        while j < len(lines) and not lines[j].startswith(">>>>>>> "):
            if lines[j].startswith("<<<<<<< "):
                fail(f"nested conflict marker at line {j + 1}")
            if lines[j].startswith("||||||| "):
                section = []  # diff3 base: dropped
            elif lines[j].rstrip() == "=======":
                section = theirs
            else:
                section.append(lines[j])
            j += 1
        if j == len(lines):
            fail(f"unterminated conflict block starting at line {i + 1}")
        out.extend(ours + theirs)
        blocks += 1
        i = j + 1
    if not blocks:
        print("no conflict blocks")
        return 1
    path.write_text("".join(out), encoding="utf-8")
    print(f"resolved {blocks} block(s) by keeping both sides")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("check", "move"):
        p = sub.add_parser(name)
        p.add_argument("--base", required=True, help="ref to compare with, e.g. origin/main")
        p.add_argument("--file", default="CHANGELOG.md", type=Path, help="default CHANGELOG.md")
        if name == "check":
            p.add_argument(
                "--allow-released-edits", action="store_true", help="allow removed lines in released sections"
            )
    u = sub.add_parser("union")
    u.add_argument("file", type=Path)
    args = parser.parse_args()
    path = Path(args.file).resolve()
    if not path.is_file():
        fail(f"no such file: {path}")
    if args.command == "check":
        return cmd_check(path, args.base, args.allow_released_edits)
    if args.command == "move":
        return cmd_move(path, args.base)
    return cmd_union(path)


if __name__ == "__main__":
    sys.exit(main())
