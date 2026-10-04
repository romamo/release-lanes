#!/usr/bin/env python3
"""Keep a repo's feature specs: files docs/specs/NNN-<slug>.md, each merged through a pull
request before its build starts, with numbered acceptance criteria S-NNN-k.

Usage:
  specs.py new [--dir D] [--title T] SLUG
  specs.py check [--dir D] [--decisions F]
  specs.py find [--dir D] TERM...
  specs.py criteria [--dir D] NNN

A spec has a title line and fixed sections, in this order:

  # S-007: <title>

  ## Problem
  ## Behaviour              (name touched paths and areas in backticks)
  ## Acceptance criteria    (- S-007-1: <checkable statement>, numbered from 1)
  ## Out of scope
  ## Decisions relied on    (- D-5 ..., or - none)
  ## Issues                 (- owner/repo#N, filled in once the build issues exist)

new       write the next spec from D/TEMPLATE.md (or the built-in template), replacing
          S-NNN and <title>; prints the new path
check     every spec is well formed: its file name, title, and sections; criterion ids
          unique and sequential; the decisions it names exist in the log and are active
find      specs whose Behaviour touches a TERM (a path matches a backticked path, glob,
          or directory there; any term matches as a substring of the section).
          `find $(git diff --name-only origin/main...)` lists the specs a diff touches
criteria  print one spec's acceptance criteria (NNN, 7, or S-007)

Exit 0 on success (find: at least one match), 1 when check (or criteria, for its spec)
finds a problem or find matches nothing, 2 on bad input. Python 3.10+, standard library
only.
"""

from __future__ import annotations

import argparse
import fnmatch
import re
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from decisions import CANDIDATES as DECISION_LOGS
from decisions import parse as parse_log

DEFAULT_DIR = Path("docs/specs")
NOT_SPECS = ("TEMPLATE.md", "README.md")
NAME = re.compile(r"^(\d{3,})-([a-z0-9]+(?:-[a-z0-9]+)*)\.md$")
SLUG = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
TITLE = re.compile(r"^# S-(\d{3,}): (\S.*)$")
SECTIONS = ("Problem", "Behaviour", "Acceptance criteria", "Out of scope", "Decisions relied on", "Issues")
CRITERION = re.compile(r"^- S-(\d{3,})-(\d+): (\S.*)$")
DECISION_ITEM = re.compile(r"^- D-(\d+)\b")
ISSUE_ITEM = re.compile(r"^- (?:[\w.-]+/[\w.-]+)?#\d+$")
ID = re.compile(r"^(?:S-)?(\d+)$")
# Reports one problem at a line of the spec being parsed
ProblemSink = Callable[[int, str], None]
TEMPLATE = """\
# S-NNN: <title>

## Problem

Who needs this and what goes wrong without it, in a few sentences. Link the issue it came
from.

## Behaviour

What the feature does, as users and other tools see it: commands, flags, outputs, files,
defaults, and errors. Name every path or area it touches in backticks, such as
`src/pkg/cli.py` or `CLI flags`, so `specs.py find` lists this spec for a change there.

## Acceptance criteria

Each criterion is one checkable statement with its own id, numbered from 1. A test that
proves one names the id.

- S-NNN-1: <a statement a test can check, e.g. "tool run exits 2 when the config is missing">

## Out of scope

- <what this spec deliberately leaves out, and where it went>

## Decisions relied on

- none

## Issues

The build issues, filled in once they are filed, one `owner/repo#N` per line.
"""


@dataclass
class Criterion:
    number: int
    text: str
    line: int
    end: int = 0  # its last line, past the first when it wraps

    def __post_init__(self) -> None:
        self.end = self.end or self.line


@dataclass
class Spec:
    path: Path
    number: int
    slug: str
    title: str = ""
    sections: dict[str, list[tuple[int, str]]] = field(default_factory=dict)
    criteria: list[Criterion] = field(default_factory=list)
    decisions: list[tuple[int, int]] = field(default_factory=list)  # (D-n, line)

    @property
    def id(self) -> str:
        return f"S-{self.number:03d}"

    def text(self, section: str) -> str:
        return "\n".join(line for _, line in self.sections.get(section, []))


class SpecError(Exception):
    pass


def spec_files(directory: Path) -> list[Path]:
    if not directory.is_dir():
        raise SpecError(f"no {directory}: nothing has been specified yet")
    return sorted(p for p in directory.glob("*.md") if p.name not in NOT_SPECS)


def parse(path: Path) -> tuple[Spec | None, list[str]]:
    """The spec, and every format problem found in it"""
    name = NAME.match(path.name)
    if name is None:
        return None, [f"{path}: not named NNN-<slug>.md (lowercase words joined by '-')"]
    spec = Spec(path, int(name.group(1)), name.group(2))
    problems: list[str] = []

    def problem(line: int, message: str) -> None:
        problems.append(f"{path}:{line}: {message}")

    current: str | None = None
    seen: list[str] = []
    for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if line.startswith("## "):
            current = line[3:].strip()
            if current not in SECTIONS:
                problem(n, f"unknown section '{current}'; a spec has: {', '.join(SECTIONS)}")
            elif current in seen:
                problem(n, f"repeats the section '{current}'")
            seen.append(current)
            spec.sections.setdefault(current, [])
            continue
        if current is None:
            title = TITLE.match(line)
            if title and not spec.title:
                spec.title = title.group(2).strip()
                if int(title.group(1)) != spec.number:
                    problem(n, f"the title says S-{title.group(1)}, the file name {spec.id}")
            elif line.strip():
                problem(n, f"expected '# {spec.id}: <title>' and then the sections")
            continue
        spec.sections[current].append((n, line))
        wrapped = current == "Acceptance criteria" and spec.criteria and spec.criteria[-1].end == n - 1
        if line.startswith("# "):
            problem(n, "a second title line")
        elif line.startswith("- "):
            item(spec, current, n, line, problem)
        elif wrapped and line[:1] in (" ", "\t") and line.strip():
            # A criterion wrapped onto an indented line goes on
            spec.criteria[-1].text += " " + line.strip()
            spec.criteria[-1].end = n
    if not spec.title:
        problem(1, f"no title line '# {spec.id}: <title>'")
    for section in SECTIONS:
        if section not in seen:
            problem(1, f"missing the section '{section}'")
    if len(seen) == len(SECTIONS) and set(seen) == set(SECTIONS) and seen != list(SECTIONS):
        problem(1, f"sections out of order; a spec has: {', '.join(SECTIONS)}")
    if "Acceptance criteria" in seen and not spec.criteria:
        problem(1, "no acceptance criteria")
    for expected, criterion in enumerate(spec.criteria, 1):
        if criterion.number != expected:
            problem(criterion.line, f"expected {spec.id}-{expected}; criterion ids run 1..n in order")
    none = [n for n, line in spec.sections.get("Decisions relied on", []) if line.strip() == "- none"]
    if none and spec.decisions:
        problem(none[0], "'- none' next to named decisions")
    return spec, problems


def item(spec: Spec, section: str, n: int, line: str, problem: ProblemSink) -> None:
    """One list item of a section that has a fixed item format"""
    if section == "Acceptance criteria":
        criterion = CRITERION.match(line)
        if criterion is None:
            problem(n, f"a criterion must read '- {spec.id}-<k>: <statement>'")
        elif int(criterion.group(1)) != spec.number:
            problem(n, f"criterion S-{criterion.group(1)}-{criterion.group(2)} in spec {spec.id}")
        else:
            spec.criteria.append(Criterion(int(criterion.group(2)), criterion.group(3).strip(), n))
    elif section == "Decisions relied on":
        decision = DECISION_ITEM.match(line)
        if decision:
            spec.decisions.append((int(decision.group(1)), n))
        elif line.strip() != "- none":
            problem(n, "a decision must read '- D-<n>' (a title may follow), or '- none'")
    elif section == "Issues" and not ISSUE_ITEM.match(line.strip()):
        problem(n, "an issue must read '- owner/repo#N' or '- #N'")


def decision_states(given: str | None) -> dict[int, str | None] | None:
    """Each D-n in the log, mapped to the entry superseding it (None when active); None
    when there is no log"""
    path = Path(given) if given else next((p for p in DECISION_LOGS if p.is_file()), None)
    if path is None:
        return None
    if not path.is_file():
        raise SpecError(f"no decisions log at {path}")
    entries, _ = parse_log(path.read_text(encoding="utf-8"))
    return {e.number: e.fields.get("Superseded by") for e in entries}


def load(directory: Path) -> tuple[list[Spec], list[str]]:
    specs: list[Spec] = []
    problems: list[str] = []
    for path in spec_files(directory):
        spec, found = parse(path)
        problems += found
        if spec is not None:
            specs.append(spec)
    numbers: dict[int, Path] = {}
    for spec in specs:
        if spec.number in numbers:
            problems.append(f"{spec.path}: {spec.id} is also {numbers[spec.number]}")
        numbers.setdefault(spec.number, spec.path)
    return specs, problems


def check_decisions(specs: list[Spec], log: dict[int, str | None] | None) -> list[str]:
    problems = []
    for spec in specs:
        for number, line in spec.decisions:
            where = f"{spec.path}:{line}"
            if log is None:
                problems.append(f"{where}: names D-{number}, but there is no decisions log")
            elif number not in log:
                problems.append(f"{where}: names D-{number}, which the decisions log lacks")
            elif log[number] is not None:
                problems.append(f"{where}: names D-{number}, superseded by {log[number]}")
    return problems


def touches(spec: Spec, term: str) -> bool:
    behaviour = spec.text("Behaviour")
    for token in re.findall(r"`([^`]+)`", behaviour):
        if term == token or fnmatch.fnmatch(term, token) or term.startswith(token.rstrip("*").rstrip("/") + "/"):
            return True
    return term.lower() in behaviour.lower()


def new(directory: Path, slug: str, title: str | None) -> int:
    if not SLUG.match(slug):
        raise SpecError(f"slug must be lowercase words joined by '-', got {slug!r}")
    existing = [NAME.match(p.name) for p in directory.glob("*.md")] if directory.is_dir() else []
    taken = [m for m in existing if m]
    clash = next((m.group(0) for m in taken if m.group(2) == slug), None)
    if clash:
        raise SpecError(f"{directory / clash} already has the slug {slug!r}")
    template_path = directory / "TEMPLATE.md"
    template = template_path.read_text(encoding="utf-8") if template_path.is_file() else TEMPLATE
    for placeholder in ("S-NNN", "<title>"):
        if placeholder not in template:
            raise SpecError(f"{template_path} lacks the placeholder {placeholder!r}")
    number = max((int(m.group(1)) for m in taken), default=0) + 1
    text = template.replace("S-NNN", f"S-{number:03d}").replace("<title>", title or slug.replace("-", " ").capitalize())
    path = directory / f"{number:03d}-{slug}.md"
    directory.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    print(path)
    return 0


def criteria(directory: Path, wanted: str) -> int:
    match = ID.match(wanted)
    if match is None:
        raise SpecError(f"a spec is named NNN, 7, or S-007, got {wanted!r}")
    specs, _ = load(directory)
    spec = next((s for s in specs if s.number == int(match.group(1))), None)
    if spec is None:
        raise SpecError(f"no spec S-{int(match.group(1)):03d} in {directory}")
    _, problems = parse(spec.path)
    for criterion in spec.criteria:
        print(f"{spec.id}-{criterion.number}: {criterion.text}")
    for problem in problems:
        print(problem, file=sys.stderr)
    return 1 if problems else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("new", "check", "find", "criteria"):
        p = sub.add_parser(name)
        p.add_argument("--dir", type=Path, default=DEFAULT_DIR, help="the specs folder (default: docs/specs)")
        if name == "new":
            p.add_argument("--title", help="the spec's title (default: from the slug)")
            p.add_argument("slug", metavar="SLUG")
        if name == "check":
            p.add_argument("--decisions", help="the decisions log (default: DECISIONS.md or docs/decisions.md)")
        if name == "find":
            p.add_argument("terms", nargs="+", metavar="TERM")
        if name == "criteria":
            p.add_argument("spec", metavar="NNN")
    args = parser.parse_args()
    try:
        if args.command == "new":
            return new(args.dir, args.slug, args.title)
        if args.command == "criteria":
            return criteria(args.dir, args.spec)
        specs, problems = load(args.dir)
        if args.command == "check":
            problems += check_decisions(specs, decision_states(args.decisions))
            for problem in problems:
                print(problem)
            return 1 if problems else 0
        found = [s for s in specs if any(touches(s, t) for t in args.terms)]
        for spec in found:
            print(f"{spec.id}: {spec.title}  ({spec.path})")
        return 0 if found else 1
    except (SpecError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
