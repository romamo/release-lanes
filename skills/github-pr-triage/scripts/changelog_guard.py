#!/usr/bin/env python3
"""CHANGELOG safety for rebases (Keep a Changelog layout, `## [Unreleased]` then `## [X]`).

check --base REF   exit 1 if lines added (or removed) since REF fall outside Unreleased, or if
                   Unreleased has a bullet with no ### heading or the same ### heading twice
move --base REF    move entries added since REF that sit in a released section under Unreleased,
                   each under its own ### heading (created in Keep a Changelog order if missing)
union FILE         resolve conflict blocks by keeping both sides, ours first, with a blank line
                   between a heading and the other side

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
CATEGORY = "### "
BULLET = ("- ", "* ")
ORDER = ("Added", "Changed", "Deprecated", "Removed", "Fixed", "Security")


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


def heading_problems(lines: list[str], start: int, end: int) -> list[str]:
    """A bullet with no ### heading above it, or a ### heading seen twice, under Unreleased.
    A CHANGELOG with no ### heading anywhere has no categories to check."""
    uses_headings = any(line.startswith(CATEGORY) for line in lines)
    problems: list[str] = []
    seen: dict[str, int] = {}
    for n in range(start + 1, end):
        line = lines[n - 1]
        if line.startswith(CATEGORY):
            name = line[len(CATEGORY) :].strip()
            if name in seen:
                problems.append(
                    f"duplicate heading under Unreleased, line {n}: {line.rstrip()} (first at line {seen[name]})"
                )
            else:
                seen[name] = n
        elif line.startswith(BULLET) and not seen and uses_headings:
            problems.append(f"bullet with no ### heading under Unreleased, line {n}: {line.rstrip()}")
    return problems


def released_removals(base: str, path: Path, removed: list[int]) -> tuple[bool, list[int]]:
    """Whether the base has an Unreleased heading, and the removed base lines outside it.
    Right after a release the base has no Unreleased heading: every base line is released,
    and the heading itself is one of the added lines"""
    old = base_lines(base, path)
    has_unreleased = any(line.rstrip() == UNRELEASED for line in old)
    old_start, old_end = unreleased_range(old) if has_unreleased else (0, 0)
    return has_unreleased, [n for n in removed if not old_start < n < old_end]


def cmd_check(path: Path, base: str, allow_released_edits: bool) -> int:
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    start, end = unreleased_range(lines)
    added, removed = diff_changes(base, path)
    has_unreleased, edited = released_removals(base, path, removed)
    first = start + 1 if has_unreleased else start
    outside = [n for n in added if not first <= n < end]
    if allow_released_edits:
        edited = []
    for n in outside:
        print(f"added outside Unreleased, line {n}: {lines[n - 1].rstrip()}")
    for n in edited:
        print(f"removed from a released section, {base} line {n}")
    shape = heading_problems(lines, start, end)
    for problem in shape:
        print(problem)
    if outside or edited or shape:
        return 1
    print(f"ok: {len(added)} added line(s), all under Unreleased (lines {start}-{end - 1})")
    return 0


def cmd_move(path: Path, base: str) -> int:
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    start, end = unreleased_range(lines)
    added, removed = diff_changes(base, path)
    outside = [n for n in added if not start < n < end]
    if not outside:
        print("nothing to move: every added line is under Unreleased")
        return 1
    _, edited = released_removals(base, path, removed)
    if edited:
        fail(f"lines removed from a released section since {base} (base lines {edited}); fix them by hand")
    entries, involved, stray = misplaced_entries(lines, outside)
    if not involved:
        return move_block(path, lines, start, outside)
    if start in outside:
        fail(f"the '{UNRELEASED}' heading itself is new since {base}; move the entries by hand")
    if stray:
        fail(f"added lines outside Unreleased that are not whole entries ({stray}); move them by hand")
    headless = [n for n, heading, _ in entries if heading is None]
    if headless:
        fail(f"added entries outside Unreleased with no ### heading above them ({headless}); move them by hand")
    categories: dict[str, list[str]] = {}
    for _, heading, body in entries:
        assert heading is not None
        categories.setdefault(heading, []).extend(body)
    removed = set(outside)
    rest = [line for n, line in enumerate(lines, 1) if n not in removed]
    for name, body in categories.items():
        insert_entries(rest, name, [line if line.endswith("\n") else line + "\n" for line in body])
    path.write_text("".join(rest), encoding="utf-8")
    counts = ", ".join(f"{name} ({sum(1 for _, h, _ in entries if h == name)})" for name in categories)
    print(f"moved {len(entries)} entr{'y' if len(entries) == 1 else 'ies'} under Unreleased: {counts}")
    return 0


def misplaced_entries(
    lines: list[str], outside: list[int]
) -> tuple[list[tuple[int, str | None, list[str]]], bool, list[int]]:
    """The entries made of added lines outside Unreleased, as (line, the ### heading they sat
    under or None, their lines), whether any added line sits under a ### heading, and the added lines
    that belong to no added entry (text, a continuation of a released entry, a ## heading)"""
    added = set(outside)
    entries: list[tuple[int, str | None, list[str]]] = []
    stray: list[int] = []
    involved = False
    heading: str | None = None
    current: list[str] | None = None
    blanks: list[str] = []
    for n, line in enumerate(lines, 1):
        new = n in added
        if line.startswith(CATEGORY):
            heading, current, blanks = line[len(CATEGORY) :].strip(), None, []
        elif line.startswith("#"):
            heading, current, blanks = None, None, []
            if new:
                stray.append(n)
        elif line.startswith(BULLET):
            current, blanks = ([line], []) if new else (None, [])
            if current is not None:
                entries.append((n, heading, current))
        elif not line.strip():
            if new and current is not None:
                blanks.append(line)
        elif line[0].isspace() and current is not None:
            if new:
                current.extend(blanks)
                current.append(line)
                blanks = []
            else:
                stray.append(n)  # a released line continues an added entry
                current = None
        else:
            if new:
                stray.append(n)
            current, blanks = None, []
        involved = involved or (new and heading is not None)
    return entries, involved, stray


def insert_entries(lines: list[str], name: str, body: list[str]) -> None:
    """Append body under '### name' in Unreleased, creating the heading in Keep a Changelog order"""
    start, end = unreleased_range(lines)
    top, bottom = start - 1, end - 1  # 0-based: the Unreleased heading, the next ## heading (or EOF)
    found = [(i, lines[i][len(CATEGORY) :].strip()) for i in range(top + 1, bottom) if lines[i].startswith(CATEGORY)]

    def last_text(lo: int, hi: int) -> int:
        return next(i for i in range(hi - 1, lo - 1, -1) if lines[i].strip())

    def place(at: int, block: list[str]) -> None:
        if at < len(lines) and lines[at].strip():
            block = block + ["\n"]
        lines[at:at] = block

    existing = [k for k, (_, heading) in enumerate(found) if heading == name]
    if existing:
        k = existing[0]
        stop = found[k + 1][0] if k + 1 < len(found) else bottom
        last = last_text(found[k][0], stop)
        place(last + 1, (["\n"] if last == found[k][0] else []) + body)
        return
    rank = ORDER.index(name) if name in ORDER else len(ORDER)
    later = [i for i, heading in found if (ORDER.index(heading) if heading in ORDER else len(ORDER)) > rank]
    if later:
        place(later[0], [f"{CATEGORY}{name}\n", "\n", *body])
    else:
        place(last_text(top, bottom) + 1, ["\n", f"{CATEGORY}{name}\n", "\n", *body])


def move_block(path: Path, lines: list[str], start: int, outside: list[int]) -> int:
    """No ### heading involved: move the added lines as one block to the top of Unreleased"""
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


def is_heading(line: str) -> bool:
    return line.startswith("#")


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
        out.extend(ours)
        if (
            ours
            and theirs
            and ours[-1].strip()
            and theirs[0].strip()
            and (is_heading(ours[-1]) or is_heading(theirs[0]))
        ):
            out.append("\n")  # "## [0.5.2]" then "### Added" would read as one section
        out.extend(theirs)
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
