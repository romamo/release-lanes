#!/usr/bin/env python3
"""Keep a repo's feature specs: files docs/specs/NNN-<slug>.md, each merged through a pull
request before its build starts, with numbered acceptance criteria S-NNN-k.

Usage:
  specs.py new [--dir D] [--title T] SLUG
  specs.py check [--dir D] [--decisions F]
  specs.py find [--dir D] TERM...
  specs.py criteria [--dir D] NNN
  specs.py coverage [--dir D] [--root R] [--spec NNN]...
  specs.py split [--dir D] [--repo OWNER/REPO] [--group K,K...]... [--after B:A]... [--json] NNN

A spec has a title line, a status line, and fixed sections, in this order:

  # S-007: <title>

  status: draft             (draft, approved, or built)

  ## Problem
  ## Behaviour              (name touched paths and areas in backticks)
  ## Acceptance criteria    (- S-007-1: <checkable statement>, numbered from 1)
  ## Out of scope
  ## Decisions relied on    (- D-5 ..., or - none)
  ## Issues                 (- owner/repo#N: S-007-1, S-007-2: each build issue and the
                            criteria it delivers, filled in once the build issues exist)
  ## Verification           (once built: each criterion id, how it was checked on main)

new       write the next spec from D/TEMPLATE.md (or the built-in template), replacing
          S-NNN and <title>; prints the new path
check     every spec is well formed: its file name, title, and sections; criterion ids
          unique and sequential; the decisions it names exist in the log and are active;
          a built spec lists its issues and names every criterion under Verification;
          once an approved or built spec lists build issues, each criterion is assigned
          to exactly one of them, save a dropped one (its text starts "dropped in #N"),
          which needs no build issue and may be in one. A draft may keep the template's placeholder text
          (D/TEMPLATE.md, or the built-in template: each paragraph of a section, word
          for word, past "- none"); an approved spec may not in Problem, Behaviour,
          Acceptance criteria, or Out of scope, and a built spec in no section. No
          folder D: "no specs", exit 0
find      specs whose Behaviour touches a TERM (a path matches a backticked path, glob,
          or directory there; any term matches as a substring of the section).
          `find $(git diff --name-only origin/main...)` lists the specs a diff touches
criteria  print one spec's acceptance criteria (NNN, 7, or S-007)
coverage  for every built spec (or each --spec, whatever its status), list each criterion
          and the tests under R (default .) that prove it. A test proves S-007-2 by its
          name (test_s007_2_..., in Go TestS007_2...) or by a comment line naming the id
          after "proves:" (several ids may follow, comma-separated). Test files are read
          as text: Python test_*.py and *_test.py; JS/TS *.test.* and *.spec.* and files
          under __tests__; Go *_test.go; every Rust .rs file. Hidden folders and
          node_modules, target, vendor, venv, dist, build are skipped, and so are the
          lines inside a Python multi-line string (a fixture, not a test). A dropped
          criterion needs no test and prints as dropped. No folder
          D (and no --spec): "no specs", exit 0
split    propose the build issues of an approved spec, for the criteria its Issues
          section doesn't assign yet, dropped ones aside: one issue per --group of criterion numbers (default:
          all of them in one), in the order given, each depending on the earlier groups
          named by --after B:A (B depends on A). Prints each issue's title and body, whose
          "Depends on #{Bk}" lines and the Issues lines to add name the issues by key
          until they are filed: replace {Bk} with each issue's number as it is created.
          --json prints the same as one JSON object

Exit 0 on success (find: at least one match), 1 when check (or criteria or split, for
its spec) finds a problem, find matches nothing, or coverage finds a criterion with no test (or a
test naming a criterion its spec lacks), 2 on bad input. Python 3.10+, standard library
only.
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import os
import re
import sys
import tokenize
from collections.abc import Callable
from dataclasses import dataclass, field
from io import StringIO
from pathlib import Path
from typing import Any

from decisions import CANDIDATES as DECISION_LOGS
from decisions import parse as parse_log

DEFAULT_DIR = Path("docs/specs")
NOT_SPECS = ("TEMPLATE.md", "README.md")
NAME = re.compile(r"^(\d{3,})-([a-z0-9]+(?:-[a-z0-9]+)*)\.md$")
SLUG = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
TITLE = re.compile(r"^# S-(\d{3,}): (\S.*)$")
SECTIONS = (
    "Problem",
    "Behaviour",
    "Acceptance criteria",
    "Out of scope",
    "Decisions relied on",
    "Issues",
    "Verification",
)
STATUS = re.compile(r"^status: (\S+)$")
STATUSES = ("draft", "approved", "built")
# The sections an approved spec must have filled in past the template's placeholders; a
# built spec must have filled in every section (Issues and Verification come last)
FILLED_WHEN_APPROVED = ("Problem", "Behaviour", "Acceptance criteria", "Out of scope")
CRITERION = re.compile(r"^- S-(\d{3,})-(\d+): (\S.*)$")
# A criterion dropped by a later change keeps its id and says so first: "dropped in #81, see
# S-007-5". It needs no test, no build issue, and a Verification line such as "dropped in #81"
DROPPED = re.compile(r"^dropped in (?:[\w.-]+/[\w.-]+)?#\d+\b")
DECISION_ITEM = re.compile(r"^- D-(\d+)\b")
ISSUE_ITEM = re.compile(r"^- ((?:[\w.-]+/[\w.-]+)?#\d+)(?::[ \t]*(.*))?$")
ASSIGNED = re.compile(r"^S-(\d{3,})-(\d+)$")
REPO = re.compile(r"^[\w.-]+/[\w.-]+$")
UNASSIGNED = "is in no build issue under Issues"
ID = re.compile(r"^(?:S-)?(\d+)$")
# Reports one problem at a line of the spec being parsed
ProblemSink = Callable[[int, str], None]

# Coverage: a test names the criterion it proves, S-007-2, as test_s007_2_... (group 1 the
# test, 2 the spec, 3 the criterion), or a comment line says "proves: S-007-2, S-007-3"
PY_NAME = re.compile(r"^[ \t]*(?:async[ \t]+)?def[ \t]+(test_s(\d{3,})_(\d+)(?!\d)\w*)[ \t]*\(", re.MULTILINE)
JS_NAME = re.compile(
    r"^[ \t]*(?:(?:it|test)(?:\.\w+)*[ \t]*\([ \t]*['\"`]((?:test_)?s(\d{3,})_(\d+)(?!\d)[^'\"`\n]*)"
    r"|(?:export[ \t]+)?(?:async[ \t]+)?function[ \t]+(test_s(\d{3,})_(\d+)(?!\d)\w*)[ \t]*\()",
    re.MULTILINE,
)
GO_NAME = re.compile(r"^[ \t]*func[ \t]+(Test_?[Ss](\d{3,})_(\d+)(?!\d)\w*)[ \t]*\(", re.MULTILINE)
RS_NAME = re.compile(
    r"^[ \t]*(?:pub[ \t]+)?(?:async[ \t]+)?fn[ \t]+(test_s(\d{3,})_(\d+)(?!\d)\w*)[ \t]*\(", re.MULTILINE
)
HASH_PROVES = re.compile(r"^[ \t]*#[ \t]*proves:[ \t]*(.+)$", re.MULTILINE)
SLASH_PROVES = re.compile(r"^[ \t]*//[ \t]*proves:[ \t]*(.+)$", re.MULTILINE)
PROVEN_ID = re.compile(r"S-(\d{3,})-(\d+)(?!\d)")
JS_SUFFIXES = (".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".mts", ".cts")
SKIP_DIRS = {"node_modules", "target", "vendor", "venv", "dist", "build", "__pycache__"}
TEMPLATE = """\
# S-NNN: <title>

status: draft

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

The build issues, filled in once they are filed: one `- owner/repo#N: S-NNN-1, S-NNN-2` per
line, naming the criteria that issue delivers. Each criterion belongs to exactly one.

## Verification

Filled in when the spec reaches `built`: one line per criterion id, saying how it was
checked against the default branch (beyond its tests) and the result.
"""


@dataclass
class Criterion:
    number: int
    text: str
    line: int
    end: int = 0  # its last line, past the first when it wraps

    def __post_init__(self) -> None:
        self.end = self.end or self.line

    @property
    def dropped(self) -> bool:
        return DROPPED.match(self.text) is not None


@dataclass
class BuildIssue:
    ref: str  # owner/repo#N or #N
    criteria: list[int]  # the criterion numbers it delivers
    line: int


@dataclass
class Spec:
    path: Path
    number: int
    slug: str
    title: str = ""
    status: str = ""
    sections: dict[str, list[tuple[int, str]]] = field(default_factory=dict)
    criteria: list[Criterion] = field(default_factory=list)
    decisions: list[tuple[int, int]] = field(default_factory=list)  # (D-n, line)
    issues: list[BuildIssue] = field(default_factory=list)

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
            status = STATUS.match(line)
            if title and not spec.title:
                spec.title = title.group(2).strip()
                if int(title.group(1)) != spec.number:
                    problem(n, f"the title says S-{title.group(1)}, the file name {spec.id}")
            elif status and spec.title and not spec.status:
                spec.status = status.group(1)
                if spec.status not in STATUSES:
                    problem(n, f"status must be one of {', '.join(STATUSES)}, got '{spec.status}'")
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
    if not spec.status:
        problem(1, f"no status line ('status: {'|'.join(STATUSES)}') after the title")
    if spec.status == "built":
        if not spec.issues:
            problem(1, "built, but lists no build issues")
        verified = {
            int(m.group(2)) for m in PROVEN_ID.finditer(spec.text("Verification")) if int(m.group(1)) == spec.number
        }
        for criterion in spec.criteria:
            if criterion.number not in verified:
                problem(1, f"built, but Verification doesn't name {spec.id}-{criterion.number}")
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
    check_assignment(spec, problem)
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
    elif section == "Issues":
        build_issue(spec, n, line, problem)


def build_issue(spec: Spec, n: int, line: str, problem: ProblemSink) -> None:
    """One line of the Issues section: a build issue and the criteria it delivers"""
    match = ISSUE_ITEM.match(line.strip())
    if match is None:
        problem(n, f"an issue must read '- owner/repo#N: {spec.id}-1, {spec.id}-2' (or '- #N: ...')")
        return
    numbers = []
    for name in (match.group(2) or "").split(","):
        if not name.strip():
            continue
        criterion = ASSIGNED.match(name.strip())
        if criterion is None:
            problem(n, f"{name.strip()!r} is not a criterion id; list them as {spec.id}-1, {spec.id}-2")
        elif int(criterion.group(1)) != spec.number:
            problem(n, f"names {name.strip()}, a criterion of another spec than {spec.id}")
        else:
            numbers.append(int(criterion.group(2)))
    spec.issues.append(BuildIssue(match.group(1), numbers, n))


def check_assignment(spec: Spec, problem: ProblemSink) -> None:
    """Once an approved or built spec lists its build issues, each criterion is delivered by
    exactly one of them. An approved spec with no build issues yet hasn't been split"""
    if spec.status not in ("approved", "built") or not spec.issues:
        return
    known = {c.number for c in spec.criteria}
    owner: dict[int, str] = {}
    refs: set[str] = set()
    for issue in spec.issues:
        if issue.ref in refs:
            problem(issue.line, f"lists {issue.ref} twice")
        refs.add(issue.ref)
        if not issue.criteria:
            problem(issue.line, f"{issue.ref} names no criteria; write '- {issue.ref}: {spec.id}-1, ...'")
        for number in issue.criteria:
            if number not in known:
                problem(issue.line, f"{issue.ref} names {spec.id}-{number}, which the spec lacks")
            elif number in owner:
                problem(issue.line, f"{spec.id}-{number} is in both {owner[number]} and {issue.ref}")
            else:
                owner[number] = issue.ref
    for criterion in spec.criteria:
        if criterion.number not in owner and not criterion.dropped:
            problem(criterion.line, f"{spec.id}-{criterion.number} {UNASSIGNED}")


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


def template(directory: Path) -> str:
    """The template `new` writes a spec from: D/TEMPLATE.md, or the built-in one"""
    path = directory / "TEMPLATE.md"
    return path.read_text(encoding="utf-8") if path.is_file() else TEMPLATE


def placeholders(text: str, spec_id: str) -> dict[str, list[list[str]]]:
    """Each section's placeholder paragraphs in the template text (runs of non-blank
    lines, S-NNN read as the spec's id), past a bare "- none", which is a real answer"""
    found: dict[str, list[list[str]]] = {}
    current: str | None = None
    paragraph: list[str] = []

    def close() -> None:
        if current in SECTIONS and paragraph and paragraph != ["- none"]:
            found.setdefault(current, []).append(list(paragraph))
        paragraph.clear()

    for line in text.replace("S-NNN", spec_id).splitlines():
        if line.startswith("## "):
            close()
            current = line[3:].strip()
        elif line.strip():
            paragraph.append(line.rstrip())
        else:
            close()
    close()
    return found


def placeholder_problems(spec: Spec, template_text: str) -> list[str]:
    """A placeholder paragraph of the template left word for word in a section the
    spec's status says is filled in"""
    if spec.status == "approved":
        sections: tuple[str, ...] = FILLED_WHEN_APPROVED
    elif spec.status == "built":
        sections = SECTIONS
    else:
        return []
    problems = []
    for section, paragraphs in placeholders(template_text, spec.id).items():
        if section not in sections:
            continue
        lines = spec.sections.get(section, [])
        body = [line.rstrip() for _, line in lines]
        for paragraph in paragraphs:
            at = next(
                (i for i in range(len(body) - len(paragraph) + 1) if body[i : i + len(paragraph)] == paragraph), None
            )
            if at is not None:
                problems.append(
                    f"{spec.path}:{lines[at][0]}: {spec.status}, but '{section}' still holds the template's "
                    f"placeholder: {paragraph[0]}"
                )
    return problems


def load(directory: Path) -> tuple[list[Spec], list[str]]:
    specs: list[Spec] = []
    problems: list[str] = []
    template_text = template(directory)
    for path in spec_files(directory):
        spec, found = parse(path)
        problems += found
        if spec is not None:
            specs.append(spec)
            problems += placeholder_problems(spec, template_text)
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
    template_text = template(directory)
    for placeholder in ("S-NNN", "<title>"):
        if placeholder not in template_text:
            raise SpecError(f"{directory / 'TEMPLATE.md'} lacks the placeholder {placeholder!r}")
    number = max((int(m.group(1)) for m in taken), default=0) + 1
    text = template_text.replace("S-NNN", f"S-{number:03d}").replace(
        "<title>", title or slug.replace("-", " ").capitalize()
    )
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
    problems += placeholder_problems(spec, template(directory))
    for criterion in spec.criteria:
        print(f"{spec.id}-{criterion.number}: {criterion.text}")
    for problem in problems:
        print(problem, file=sys.stderr)
    return 1 if problems else 0


def test_files(root: Path) -> list[tuple[Path, list[re.Pattern[str]]]]:
    """Each test file under root, with the patterns that find its proofs"""
    found = []
    for folder, dirs, files in os.walk(root):
        dirs[:] = sorted(d for d in dirs if not d.startswith(".") and d not in SKIP_DIRS)
        for name in sorted(files):
            path = Path(folder) / name
            if name.endswith(".py") and (name.startswith("test_") or name.endswith("_test.py")):
                found.append((path, [PY_NAME, HASH_PROVES]))
            elif name.endswith(JS_SUFFIXES) and (
                ".test." in name or ".spec." in name or "__tests__" in path.relative_to(root).parts
            ):
                found.append((path, [JS_NAME, SLASH_PROVES]))
            elif name.endswith("_test.go"):
                found.append((path, [GO_NAME, SLASH_PROVES]))
            elif name.endswith(".rs"):
                found.append((path, [RS_NAME, SLASH_PROVES]))
    return found


def string_lines(path: Path, text: str) -> set[int]:
    """The lines of a Python file that sit inside a multi-line string, past its first"""
    inside: set[int] = set()
    opened: list[int] = []
    try:
        for token in tokenize.generate_tokens(StringIO(text).readline):
            kind = tokenize.tok_name[token.type]
            if token.type == tokenize.STRING:
                inside.update(range(token.start[0] + 1, token.end[0] + 1))
            elif kind.endswith("STRING_START"):  # an f-string (3.12+) or t-string (3.14+)
                opened.append(token.start[0])
            elif kind.endswith("STRING_END") and opened:
                inside.update(range(opened.pop() + 1, token.end[0] + 1))
    except (tokenize.TokenError, SyntaxError) as exc:
        raise SpecError(f"{path}: not valid Python, so its tests can't be read: {exc}") from exc
    return inside


def proofs(root: Path) -> dict[tuple[int, int], list[str]]:
    """(spec, criterion) to the places that prove it: "path:line test_name" for a named
    test, "path:line" for a comment"""
    found: dict[tuple[int, int], list[str]] = {}
    for path, patterns in test_files(root):
        text = path.read_text(encoding="utf-8", errors="replace")
        where = path.relative_to(root).as_posix()
        quoted = string_lines(path, text) if path.suffix == ".py" else set()
        for pattern in patterns:
            for match in pattern.finditer(text):
                line = text.count("\n", 0, match.start()) + 1
                if line in quoted:
                    continue  # a fixture inside a multi-line string, not a test
                groups = [g for g in match.groups() if g is not None]
                if pattern in (HASH_PROVES, SLASH_PROVES):
                    ids = [(int(m.group(1)), int(m.group(2))) for m in PROVEN_ID.finditer(groups[0])]
                    for key in ids:
                        found.setdefault(key, []).append(f"{where}:{line}")
                else:
                    name, spec, criterion = groups[:3]
                    found.setdefault((int(spec), int(criterion)), []).append(f"{where}:{line} {name}")
    return found


def coverage(directory: Path, root: Path, wanted: list[str]) -> int:
    if not root.is_dir():
        raise SpecError(f"no folder {root} to look for tests in")
    if not wanted and not directory.is_dir():
        print("no specs")
        return 0
    specs, problems = load(directory)
    numbers = []
    for name in wanted:
        match = ID.match(name)
        if match is None:
            raise SpecError(f"a spec is named NNN, 7, or S-007, got {name!r}")
        numbers.append(int(match.group(1)))
    missing_specs = sorted(set(numbers) - {s.number for s in specs})
    if missing_specs:
        raise SpecError(f"no spec {', '.join(f'S-{n:03d}' for n in missing_specs)} in {directory}")
    chosen = [s for s in specs if s.number in numbers] if numbers else [s for s in specs if s.status == "built"]
    if not chosen:
        print("no built specs")
        return 0
    found = proofs(root)
    failed = False
    for spec in chosen:
        print(f"{spec.id}: {spec.title} ({spec.status or 'no status'})")
        for problem in problems:
            if problem.startswith(f"{spec.path}:"):
                print(f"  {problem}")
                failed = True
        for criterion in spec.criteria:
            places = sorted(found.get((spec.number, criterion.number), []))
            print(f"  {spec.id}-{criterion.number}: {criterion.text}")
            for place in places:
                print(f"    {place}")
            if not places and criterion.dropped:
                print("    dropped, no test needed")
            elif not places:
                print("    no test proves it")
                failed = True
        known = {c.number for c in spec.criteria}
        for (number, criterion), places in sorted(found.items()):
            if number == spec.number and criterion not in known:
                print(f"  {spec.id}-{criterion}: no such criterion, yet named by {', '.join(places)}")
                failed = True
    return 1 if failed else 0


def criterion_number(spec: Spec, name: str) -> int:
    """A criterion named as 3 or S-007-3"""
    text = name.strip()
    full = ASSIGNED.match(text)
    if full and int(full.group(1)) == spec.number:
        return int(full.group(2))
    if text.isdigit():
        return int(text)
    raise SpecError(f"a criterion of {spec.id} is named k or {spec.id}-k, got {text!r}")


def groups_of(spec: Spec, groups: list[str], unassigned: list[int], assigned: dict[int, str]) -> list[list[int]]:
    """Each --group's criterion numbers (default: every unassigned criterion in one), each
    unassigned criterion in exactly one"""
    chosen = [[criterion_number(spec, n) for n in g.split(",") if n.strip()] for g in groups] or [unassigned]
    known = {c.number for c in spec.criteria}
    dropped = {c.number for c in spec.criteria if c.dropped}
    seen: dict[int, int] = {}
    for k, group in enumerate(chosen, 1):
        if not group:
            raise SpecError(f"--group {k} names no criteria")
        for number in group:
            if number not in known:
                raise SpecError(f"--group {k}: {spec.id} has no criterion {spec.id}-{number}")
            if number in dropped:
                raise SpecError(f"--group {k}: {spec.id}-{number} is dropped; no build issue delivers it")
            if number in assigned:
                raise SpecError(f"--group {k}: {spec.id}-{number} is already in {assigned[number]}")
            if number in seen:
                raise SpecError(f"{spec.id}-{number} is in both --group {seen[number]} and --group {k}")
            seen[number] = k
    left = [n for n in unassigned if n not in seen]
    if left:
        raise SpecError(f"{', '.join(f'{spec.id}-{n}' for n in left)} in no --group; each criterion needs one")
    return chosen


def dependencies(after: list[str], count: int) -> dict[int, list[int]]:
    """Each build issue (1..count) to the earlier ones it depends on, from --after B:A"""
    depends: dict[int, list[int]] = {k: [] for k in range(1, count + 1)}
    for edge in after:
        match = re.match(r"^(\d+):(\d+)$", edge.strip())
        if match is None:
            raise SpecError(f"--after is B:A (build issue B depends on A, numbered as the groups), got {edge!r}")
        later, earlier = int(match.group(1)), int(match.group(2))
        if later not in depends or earlier not in depends:
            raise SpecError(f"--after {edge}: the groups are numbered 1 to {count}")
        if earlier >= later:
            raise SpecError(f"--after {edge}: a build issue depends only on an earlier group; give them in build order")
        if earlier not in depends[later]:
            depends[later].append(earlier)
    return {k: sorted(v) for k, v in depends.items()}


def task_graph(spec: Spec, groups: list[str], after: list[str], repo: str | None) -> list[dict[str, Any]]:
    """The build issues proposed for an approved spec's criteria that no build issue has yet,
    in the order to file them. Each names the others by key, {B1}, until they have numbers"""
    if spec.status != "approved":
        raise SpecError(f"{spec.id} is {spec.status or 'without a status'}; only an approved spec is split")
    if repo is not None and REPO.match(repo) is None:
        raise SpecError(f"--repo must be owner/repo, got {repo!r}")
    assigned = {n: issue.ref for issue in spec.issues for n in issue.criteria}
    unassigned = [c.number for c in spec.criteria if c.number not in assigned and not c.dropped]
    chosen = groups_of(spec, groups, unassigned, assigned)
    depends = dependencies(after, len(chosen))
    texts = {c.number: c.text for c in spec.criteria}
    prefix = repo or ""
    issues = []
    for k, group in enumerate(chosen, 1):
        ids = [f"{spec.id}-{n}" for n in group]
        body = [
            f"Builds part of spec {spec.id}, `{spec.path.as_posix()}`: the acceptance criteria below. Each needs a "
            f"test that names its id (`test_s{spec.number:03d}_<k>_...`, or a `proves: {ids[0]}` comment line).",
            "",
            *(f"- {spec.id}-{n}: {texts[n]}" for n in group),
        ]
        if depends[k]:
            body += ["", *(f"Depends on {prefix}#{{B{d}}}" for d in depends[k])]
        issues.append(
            {
                "key": f"B{k}",
                "title": f"{spec.title} ({', '.join(ids)})",
                "body": "\n".join(body) + "\n",
                "criteria": ids,
                "depends_on": [f"B{d}" for d in depends[k]],
                "issues_line": f"- {prefix}#{{B{k}}}: {', '.join(ids)}",
            }
        )
    return issues


def split(directory: Path, wanted: str, groups: list[str], after: list[str], repo: str | None, as_json: bool) -> int:
    match = ID.match(wanted)
    if match is None:
        raise SpecError(f"a spec is named NNN, 7, or S-007, got {wanted!r}")
    specs, _ = load(directory)
    spec = next((s for s in specs if s.number == int(match.group(1))), None)
    if spec is None:
        raise SpecError(f"no spec S-{int(match.group(1)):03d} in {directory}")
    # A criterion in no build issue yet is what split is for; any other problem stops it
    problems = [p for p in parse(spec.path)[1] if not p.endswith(UNASSIGNED)]
    if problems:
        for problem in problems:
            print(problem, file=sys.stderr)
        print(f"{spec.id} must pass check before it is split", file=sys.stderr)
        return 1
    open_criteria = [c for c in spec.criteria if not c.dropped]
    if spec.status == "approved" and all(any(c.number in i.criteria for i in spec.issues) for c in open_criteria):
        if groups or after:
            raise SpecError(f"every criterion of {spec.id} is in a build issue already; there is nothing to group")
        print(f"{spec.id}: every criterion is in a build issue already")
        return 0
    issues = task_graph(spec, groups, after, repo)
    if as_json:
        print(json.dumps({"spec": spec.id, "path": spec.path.as_posix(), "issues": issues}, indent=2))
        return 0
    print(f"{spec.id}: {len(issues)} build issue(s) for {spec.path.as_posix()}, to file in this order.")
    print("Replace each {Bk} with that issue's number once it is filed.")
    for issue in issues:
        depends = ", ".join(issue["depends_on"]) or "nothing"
        print(f"\n=== {issue['key']} (depends on {depends})\ntitle: {issue['title']}\n\n{issue['body']}", end="")
    print("\nThe spec's Issues section, once they are filed:\n")
    for issue in issues:
        print(issue["issues_line"])
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("new", "check", "find", "criteria", "coverage", "split"):
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
        if name == "coverage":
            p.add_argument("--root", type=Path, default=Path("."), help="where the tests are (default: .)")
            p.add_argument("--spec", action="append", default=[], metavar="NNN", help="check this spec, built or not")
        if name == "split":
            p.add_argument("--repo", metavar="OWNER/REPO", help="name the issues OWNER/REPO#N (default: #N)")
            p.add_argument("--group", action="append", default=[], metavar="K,K", help="one build issue's criteria")
            p.add_argument("--after", action="append", default=[], metavar="B:A", help="build issue B depends on A")
            p.add_argument("--json", action="store_true", help="print the task graph as one JSON object")
            p.add_argument("spec", metavar="NNN")
    args = parser.parse_args()
    try:
        if args.command == "new":
            return new(args.dir, args.slug, args.title)
        if args.command == "criteria":
            return criteria(args.dir, args.spec)
        if args.command == "coverage":
            return coverage(args.dir, args.root, args.spec)
        if args.command == "split":
            return split(args.dir, args.spec, args.group, args.after, args.repo, args.json)
        if args.command == "check" and not args.dir.is_dir():
            print("no specs")
            return 0
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
