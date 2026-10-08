#!/usr/bin/env python3
"""CHANGELOG safety for rebases (Keep a Changelog layout, `## [Unreleased]` then `## [X]`).

check --base REF   exit 1 if lines added (or removed) since REF fall outside Unreleased, or if
                   Unreleased has a bullet with no ### heading or the same ### heading twice.
                   When .github/shipmill.toml sets [changelog] fragments (spec 013), also exit 1
                   on a line added under Unreleased since REF (the entry goes in a fragment
                   instead) and on a fragment added or changed since REF that fails to read
move --base REF    move entries added since REF that sit in a released section under Unreleased,
                   each under its own ### heading (created in Keep a Changelog order if missing);
                   an entry right under a ## heading goes under the nearest ### heading above it
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
from pathlib import Path, PurePosixPath

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10: the [changelog] table is read line by line instead
    tomllib = None

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


CONFIG = PurePosixPath(".github/shipmill.toml")
STYLES = ("keep-a-changelog", "dash")
FRAGMENT_README = "README.md"  # keeps the folder in git; never a fragment
FRAGMENT_SUFFIX = ".md"
# the plain form read on Python 3.10: a table header, or key = "string", each with an optional comment
PLAIN_HEADER = re.compile(r"^\[\s*([A-Za-z0-9_.-]+)\s*\]\s*(?:#.*)?$")
PLAIN_KEY = re.compile(r"""^([A-Za-z0-9_-]+)\s*=\s*(?:"([^"\\]*)"|'([^']*)')\s*(?:#.*)?$""")
PLAIN_DOTTED = re.compile(r"^changelog\s*[.=]")  # [changelog] keys written outside its table
BRANCH_FRAGMENT = re.compile(r"^\d+-[A-Za-z0-9._-]+$")  # a branch's last part that names a fragment


def git_root(cwd: Path) -> Path:
    top = subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True, cwd=cwd)
    if top.returncode != 0:
        fail(f"not in a git checkout: {top.stderr.strip()}")
    return Path(top.stdout.strip())


def plain_changelog(text: str, where: str) -> dict[str, str]:
    """The [changelog] table of the config's plain form, for Python 3.10: its lines must be
    key = "string"; other tables are skipped"""
    table: dict[str, str] = {}
    current: str | None = ""  # "" before any header; None in an array of tables
    for number, line in enumerate(text.splitlines(), 1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("[["):
            current = None
            continue
        header = PLAIN_HEADER.match(stripped)
        if header is not None:
            current = header.group(1)
            continue
        if current == "" and PLAIN_DOTTED.match(stripped):
            fail(f"{where}: line {number}: can't read {stripped!r} on Python 3.10: write a [changelog] table")
        if current != "changelog":
            continue
        found = PLAIN_KEY.match(stripped)
        if found is None:
            fail(f'{where}: line {number}: can\'t read {stripped!r} on Python 3.10: write key = "string" lines')
        key = found.group(1)
        if key in table:
            fail(f"{where}: line {number}: {key} is set twice")
        table[key] = found.group(2) if found.group(2) is not None else found.group(3)
    return table


def fragments_config(root: Path) -> tuple[str, str] | None:
    """The [changelog] fragments folder and style of the checkout's config; None when there is
    no config or it sets no fragments"""
    path = root / CONFIG
    if not path.is_file():
        return None
    text = path.read_text(encoding="utf-8")
    where = str(CONFIG)
    if tomllib is None:
        table: object = plain_changelog(text, where)
    else:
        try:
            table = tomllib.loads(text).get("changelog", {})
        except tomllib.TOMLDecodeError as exc:
            fail(f"{where}: not TOML: {exc}")
    if not isinstance(table, dict):
        fail(f"{where}: changelog must be a table")
    if "fragments" not in table:
        return None
    folder = table["fragments"]
    posix = PurePosixPath(folder) if isinstance(folder, str) else None
    if (
        posix is None
        or not folder
        or folder == "."
        or posix.is_absolute()
        or ".." in posix.parts
        or posix.as_posix() != folder
    ):
        example = "such as 'changelog.d'"
        fail(f"{where}: [changelog] fragments is a folder relative to the repo root, {example}; got {folder!r}")
    style = table.get("style")
    if style not in STYLES:
        fail(f"{where}: [changelog] style must be one of {', '.join(STYLES)}; got {style!r}")
    return folder, style


def fragment_problem(text: str, style: str, path: str) -> str | None:
    """Why a fragment fails to read, as shipmill's reader (src/shipmill/changelog.py,
    fragment_entries) says it, or None when it reads"""
    lines = text.splitlines()
    if not any(line.strip() for line in lines):
        return f"{path}: an empty fragment; it holds the entries a PR adds, as under Unreleased"
    for n, line in enumerate(lines, 1):
        if line.startswith("## "):
            return f"{path}:{n}: a '## ' heading; a fragment holds only what goes under Unreleased: {line!r}"
    entries = 0
    heading = False
    in_entry = False
    for n, line in enumerate(lines, 1):
        if style == "dash":
            if line.startswith(CATEGORY):
                entries, in_entry = entries + 1, True
            elif not in_entry and line.strip():
                return f"{path}:{n}: text outside a '### ' entry: {line!r}"
            continue
        if line.startswith(CATEGORY):
            heading, in_entry = True, False
        elif line.startswith(BULLET):
            if not heading:
                return f"{path}:{n}: an entry has no '### ' category heading: {line!r}"
            entries, in_entry = entries + 1, True
        elif not line.strip():
            continue
        elif not (in_entry and line[0].isspace()):
            return f"{path}:{n}: text outside a '- ' entry: {line!r}"
    if not entries:
        return f"{path}: the fragment holds no entry"
    return None


def changed_fragments(root: Path, base: str, folder: str) -> list[str]:
    """The files under folder added or changed since base, untracked ones included, as paths
    relative to the repo root"""
    diff = subprocess.run(
        ["git", "diff", "--name-only", "-z", "--no-renames", "--diff-filter=AM", base, "--", f"{folder}/"],
        capture_output=True,
        text=True,
        cwd=root,
    )
    if diff.returncode != 0:
        fail(f"git diff failed: {diff.stderr.strip()}")
    untracked = subprocess.run(
        ["git", "ls-files", "-z", "--others", "--exclude-standard", "--", f"{folder}/"],
        capture_output=True,
        text=True,
        cwd=root,
    )
    if untracked.returncode != 0:
        fail(f"git ls-files failed: {untracked.stderr.strip()}")
    return sorted({p for p in (diff.stdout + untracked.stdout).split("\0") if p})


def fragment_problems(root: Path, base: str, folder: str, style: str) -> tuple[int, list[str]]:
    """How many fragments added or changed since base read, and why the others don't, as
    shipmill's reader (src/shipmill/fragments.py, read) says it"""
    if not (root / folder).is_dir():
        fail(f"[changelog] fragments names {folder}, which is not a folder in {root}")
    good = 0
    problems: list[str] = []
    for path in changed_fragments(root, base, folder):
        name = path[len(folder) + 1 :]
        if "/" in name:
            problems.append(f"{path}: a fragment sits right in {folder}, not in a subfolder")
        elif name == FRAGMENT_README:
            continue
        elif not name.endswith(FRAGMENT_SUFFIX) or name == FRAGMENT_SUFFIX:
            problems.append(
                f"{path}: not a fragment; a file in {folder} is a <name>{FRAGMENT_SUFFIX} or {FRAGMENT_README}"
            )
        elif (problem := fragment_problem((root / path).read_text(encoding="utf-8"), style, path)) is not None:
            problems.append(problem)
        else:
            good += 1
    return good, problems


def fragment_to_write(root: Path, folder: str) -> str:
    """The fragment an entry added under Unreleased belongs in: named after the branch when
    it reads as <issue>-<slug>, else the convention"""
    branch = subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"], capture_output=True, text=True, cwd=root)
    last = branch.stdout.strip().rsplit("/", 1)[-1] if branch.returncode == 0 else ""
    name = last if BRANCH_FRAGMENT.match(last) else "<issue>-<slug>"
    return f"{folder}/{name}{FRAGMENT_SUFFIX}"


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
    root = git_root(path.parent)
    config = fragments_config(root)
    under: list[int] = []
    unread: list[str] = []
    good = 0
    if config is not None:
        folder, style = config
        under = [n for n in added if start < n < end and lines[n - 1].strip()]
        for n in under:
            print(f"added under Unreleased, line {n}: {lines[n - 1].rstrip()}")
        if under:
            print(
                f"[changelog] fragments is set: move the entry into a new fragment, {fragment_to_write(root, folder)},"
                " and leave the CHANGELOG alone"
            )
        good, unread = fragment_problems(root, base, folder, style)
        for problem in unread:
            print(problem)
    if outside or edited or shape or under or unread:
        return 1
    if config is None:
        print(f"ok: {len(added)} added line(s), all under Unreleased (lines {start}-{end - 1})")
    else:
        print(f"ok: no entry added under Unreleased; {good} fragment(s) added or changed in {config[0]}, each reads")
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
    uses_headings = any(line.startswith(CATEGORY) for line in lines)
    if not involved and not (entries and uses_headings):  # a moved block would leave a bullet headless
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
    that belong to no added entry (text, a continuation of a released entry, a ## heading).

    An added entry right under a ## heading takes the nearest ### heading above it. That is the
    shape a union leaves when git moved the PR's heading line, identical to the other side's, out of
    the conflict block: the heading stays above the other side's lines, which end in a ## heading,
    and the PR's entry follows with no heading of its own (#82)"""
    added = set(outside)
    entries: list[tuple[int, str | None, list[str]]] = []
    stray: list[int] = []
    involved = False
    heading: str | None = None
    above: str | None = None  # the nearest ### heading, across ## headings
    current: list[str] | None = None
    blanks: list[str] = []
    for n, line in enumerate(lines, 1):
        new = n in added
        if line.startswith(CATEGORY):
            heading, current, blanks = line[len(CATEGORY) :].strip(), None, []
            above = heading
        elif line.startswith("#"):
            heading, current, blanks = None, None, []
            if new:
                stray.append(n)
        elif line.startswith(BULLET):
            current, blanks = ([line], []) if new else (None, [])
            if current is not None:
                entries.append((n, heading if heading is not None else above, current))
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
        involved = involved or (new and (heading is not None or above is not None))
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
