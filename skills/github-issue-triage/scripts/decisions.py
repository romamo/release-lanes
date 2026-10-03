#!/usr/bin/env python3
"""Keep a repo's decisions log: numbered rules the user settled, which triage and review
check every change against.

Usage:
  decisions.py check [--file F]
  decisions.py find [--file F] [--all] TERM...
  decisions.py add [--file F] --title T --rule R --why W --applies A --enforced E
                   --source owner/repo#N [--supersedes D-N] [--date YYYY-MM-DD]

The log is DECISIONS.md or docs/decisions.md, whichever exists (docs/decisions.md is
created by the first add). Each entry is:

  ## D-7: <title>

  - Decided: 2026-10-03, in owner/repo#123
  - Rule: <one testable sentence>
  - Why: <the reason, so a later change can tell whether it still holds>
  - Applies to: <paths or globs, comma-separated, or areas such as "CLI flags">
  - Enforced by: <a test, lint, or audit rule; or "review">
  - Supersedes: D-3            (optional)
  - Superseded by: D-9         (written by add, on the old entry)

check   every entry is well formed, ids run 1..n, and supersede links agree both ways
find    active entries whose Applies to matches a TERM (a path matches a glob or a
        prefix; a word matches as a substring of Applies to, Rule, or title). --all
        includes superseded entries. `find $(git diff --name-only origin/main...)`
        lists what a diff must respect
add     append the next entry and mark the one it supersedes; prints the new id

Exit 0 on success (find: at least one match), 1 when check finds a problem or find
matches nothing, 2 on bad input. Python 3.10+, standard library only.
"""

from __future__ import annotations

import argparse
import datetime as dt
import fnmatch
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

CANDIDATES = (Path("DECISIONS.md"), Path("docs/decisions.md"))
HEADING = re.compile(r"^## D-(\d+): (\S.*)$")
FIELD = re.compile(r"^- ([A-Z][a-z]+(?: [a-z]+)?): (\S.*)$")
REQUIRED = ("Decided", "Rule", "Why", "Applies to", "Enforced by")
OPTIONAL = ("Supersedes", "Superseded by")
DECIDED = re.compile(r"^\d{4}-\d\d-\d\d, in [\w.-]+/[\w.-]+#\d+$")
REF = re.compile(r"^D-(\d+)$")
PREAMBLE = """# Decisions

Rules the maintainers settled, numbered in order. Triage designs and code reviews check
every change against the entries whose "Applies to" it touches. Change a rule by adding an
entry that supersedes it, never by editing an old one.
"""


@dataclass
class Entry:
    number: int
    title: str
    line: int
    fields: dict[str, str] = field(default_factory=dict)

    @property
    def active(self) -> bool:
        return "Superseded by" not in self.fields

    def applies(self) -> list[str]:
        return [a.strip() for a in self.fields.get("Applies to", "").split(",") if a.strip()]


class LogError(Exception):
    pass


def locate(given: str | None, create: bool = False) -> Path:
    if given:
        return Path(given)
    for path in CANDIDATES:
        if path.is_file():
            return path
    if create:
        return CANDIDATES[1]
    raise LogError("no DECISIONS.md or docs/decisions.md: nothing has been decided yet")


def parse(text: str) -> tuple[list[Entry], list[str]]:
    """The entries, and every format problem found"""
    entries: list[Entry] = []
    problems: list[str] = []
    for n, line in enumerate(text.splitlines(), 1):
        heading = HEADING.match(line)
        if heading:
            entries.append(Entry(int(heading.group(1)), heading.group(2).strip(), n))
            continue
        if line.startswith("## "):
            problems.append(f"line {n}: a heading that isn't '## D-<n>: <title>'")
            continue
        item = FIELD.match(line)
        if item and entries:
            name, value = item.group(1), item.group(2).strip()
            if name not in REQUIRED + OPTIONAL:
                problems.append(f"line {n}: D-{entries[-1].number} has an unknown field '{name}'")
            elif name in entries[-1].fields:
                problems.append(f"line {n}: D-{entries[-1].number} repeats '{name}'")
            else:
                entries[-1].fields[name] = value
        elif line.startswith("- ") and entries:
            problems.append(f"line {n}: D-{entries[-1].number} has a malformed field line")
    return entries, problems


def check(entries: list[Entry]) -> list[str]:
    problems = []
    by_number = {e.number: e for e in entries}
    for i, entry in enumerate(entries, 1):
        tag = f"D-{entry.number} (line {entry.line})"
        if entry.number != i:
            problems.append(f"{tag}: expected D-{i}; ids run 1..n in order")
        for name in REQUIRED:
            if name not in entry.fields:
                problems.append(f"{tag}: missing '{name}'")
        decided = entry.fields.get("Decided")
        if decided is not None and not DECIDED.match(decided):
            problems.append(f"{tag}: Decided must read 'YYYY-MM-DD, in owner/repo#N'")
        for name, back in (("Supersedes", "Superseded by"), ("Superseded by", "Supersedes")):
            ref = entry.fields.get(name)
            if ref is None:
                continue
            match = REF.match(ref)
            other = by_number.get(int(match.group(1))) if match else None
            if other is None:
                problems.append(f"{tag}: {name} {ref} names no entry")
            elif other.fields.get(back) != f"D-{entry.number}":
                problems.append(f"{tag}: {name} {ref}, but {ref} lacks '{back}: D-{entry.number}'")
    return problems


def matches(entry: Entry, term: str) -> bool:
    for scope in entry.applies():
        if fnmatch.fnmatch(term, scope) or term.startswith(scope.rstrip("*").rstrip("/") + "/") or term == scope:
            return True
    if "/" in term or "." in term:
        return False
    text = f"{entry.title} {entry.fields.get('Rule', '')} {entry.fields.get('Applies to', '')}".lower()
    return term.lower() in text


def render(entry: Entry) -> str:
    lines = [f"## D-{entry.number}: {entry.title}", ""]
    lines += [f"- {name}: {entry.fields[name]}" for name in REQUIRED + OPTIONAL if name in entry.fields]
    return "\n".join(lines) + "\n"


def add(path: Path, args: argparse.Namespace) -> int:
    text = path.read_text(encoding="utf-8") if path.is_file() else PREAMBLE
    entries, problems = parse(text)
    problems += check(entries)
    if problems:
        raise LogError(f"{path} must pass check before an add: {problems[0]}")
    number = len(entries) + 1
    fields = {
        "Decided": f"{args.date}, in {args.source}",
        "Rule": args.rule,
        "Why": args.why,
        "Applies to": args.applies,
        "Enforced by": args.enforced,
    }
    if args.supersedes:
        match = REF.match(args.supersedes)
        old = next((e for e in entries if match and e.number == int(match.group(1))), None)
        if old is None:
            raise LogError(f"--supersedes {args.supersedes} names no entry")
        if not old.active:
            raise LogError(f"{args.supersedes} is already superseded by {old.fields['Superseded by']}")
        fields["Supersedes"] = args.supersedes
        lines = text.splitlines()
        end = next((e.line - 1 for e in entries if e.number == old.number + 1), len(lines))
        while end > old.line and not lines[end - 1].strip():
            end -= 1
        lines.insert(end, f"- Superseded by: D-{number}")
        text = "\n".join(lines) + "\n"
    new = Entry(number, args.title, 0, fields)
    if not DECIDED.match(fields["Decided"]):
        raise LogError("--source must be owner/repo#N and --date YYYY-MM-DD")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text.rstrip("\n") + "\n\n" + render(new), encoding="utf-8")
    print(f"D-{number}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("check", "find", "add"):
        p = sub.add_parser(name)
        p.add_argument("--file", help="the log (default: DECISIONS.md or docs/decisions.md)")
        if name == "find":
            p.add_argument("--all", action="store_true", help="include superseded entries")
            p.add_argument("terms", nargs="+", metavar="TERM")
        if name == "add":
            for flag in ("title", "rule", "why", "applies", "enforced", "source"):
                p.add_argument(f"--{flag}", required=True)
            p.add_argument("--supersedes")
            p.add_argument("--date", default=dt.date.today().isoformat())
    args = parser.parse_args()
    try:
        if args.command == "add":
            return add(locate(args.file, create=True), args)
        path = locate(args.file)
        entries, problems = parse(path.read_text(encoding="utf-8"))
        if args.command == "check":
            problems += check(entries)
            for problem in problems:
                print(f"{path}: {problem}")
            return 1 if problems else 0
        found = [e for e in entries if (args.all or e.active) and any(matches(e, t) for t in args.terms)]
        for entry in found:
            print(f"D-{entry.number}: {entry.title}\n  Rule: {entry.fields.get('Rule', '')}")
        return 0 if found else 1
    except (LogError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
