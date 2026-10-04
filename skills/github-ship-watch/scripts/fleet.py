#!/usr/bin/env python3
"""Read a fleet file: the shipyard repos one ship-watch report covers.

A fleet file lists the repos, in TOML:

  [[repos]]
  repo = "romamo/shipyard"

  [[repos]]
  repo = "owner/other"
  incident_label = "sev"   # optional; the label that repo's incidents carry (default "incident")

The file is refused (exit 2, with a message naming the file and the problem) when it is
missing, isn't TOML, has no [[repos]], has an entry without `repo`, has a key other than
`repo` and `incident_label`, names a repo not in owner/name form, or lists a repo twice.
On Python 3.10, where tomllib doesn't exist, the file is read in the plain form above
([[repos]] tables of quoted strings, comments, blank lines) and anything else is refused.

Each repo's watch is watch_state.py, run as a subprocess with the repo's incident_label
passed as --incident-label. Python 3.10+, standard library only.
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10: the plain form is read line by line instead
    tomllib = None

SCRIPTS = Path(__file__).resolve().parent
WATCH_STATE = SCRIPTS / "watch_state.py"
KEYS = ("repo", "incident_label")  # the keys of a [[repos]] entry
REPO = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]*/(?!\.+$)[A-Za-z0-9._-]+$")  # owner/name; a name isn't only dots
# the plain form Python 3.10 reads: a [[repos]] header or key = "string", each with an optional comment
PLAIN_HEADER = re.compile(r"^\[\[\s*repos\s*\]\]\s*(?:#.*)?$")
PLAIN_KEY = re.compile(r"""^([A-Za-z0-9_-]+)\s*=\s*(?:"([^"\\]*)"|'([^']*)')\s*(?:#.*)?$""")


class Refused(SystemExit):
    """Bad input: the message goes to stderr, and the run exits 2"""

    def __init__(self, message: str) -> None:
        super().__init__(2)
        self.message = message

    def __str__(self) -> str:
        return self.message


@dataclass(frozen=True)
class Entry:
    """One repo of the fleet"""

    repo: str
    incident_label: str | None  # None: the repo's own config decides (default "incident")


def parse_plain(text: str, path: Path) -> dict[str, Any]:
    """The plain form of a fleet file, read as tomllib would read it, for Python 3.10"""
    raw: dict[str, Any] = {}
    table = raw
    for number, line in enumerate(text.splitlines(), 1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if PLAIN_HEADER.match(stripped):
            table = {}
            raw.setdefault("repos", []).append(table)
            continue
        found = PLAIN_KEY.match(stripped)
        if found is None:
            raise Refused(
                f"error: {path}: line {number}: can't read {stripped!r} on Python 3.10: "
                'write [[repos]] tables of key = "string" lines, or use Python 3.11+'
            )
        key = found.group(1)
        if key in table:
            raise Refused(f"error: {path}: line {number}: {key} is set twice")
        table[key] = found.group(2) if found.group(2) is not None else found.group(3)
    return raw


def parse(text: str, path: Path) -> dict[str, Any]:
    """The fleet file as TOML: tomllib on Python 3.11+, the plain form on 3.10"""
    if tomllib is None:
        return parse_plain(text, path)
    try:
        return tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise Refused(f"error: {path}: not TOML: {exc}") from None


def entry(table: object, index: int, path: Path) -> Entry:
    """One [[repos]] table, checked"""
    where = f"error: {path}: repos[{index}]"
    if not isinstance(table, dict):
        raise Refused(f"{where} is not a table")
    unknown = sorted(set(table) - set(KEYS))
    if unknown:
        raise Refused(f"{where} has an unknown key {unknown[0]!r}: only repo and incident_label")
    if "repo" not in table:
        raise Refused(f"{where} has no repo")
    repo = table["repo"]
    if not isinstance(repo, str) or not REPO.match(repo):
        raise Refused(f"{where}: repo {repo!r} is not in owner/name form")
    label = table.get("incident_label")
    if label is not None and (not isinstance(label, str) or not label.strip()):
        raise Refused(f"{where}: incident_label must be a non-empty string")
    return Entry(repo, label)


def load(path: Path) -> list[Entry]:
    """The fleet file's repos, in order; Refused when the file is malformed"""
    if not path.is_file():
        raise Refused(f"error: {path}: no such file")
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise Refused(f"error: {path}: not TOML: {exc}") from None
    raw = parse(text, path)
    unknown = sorted(set(raw) - {"repos"})
    if unknown:
        raise Refused(f"error: {path}: unknown key {unknown[0]!r}: a fleet file holds only [[repos]] tables")
    tables = raw.get("repos")
    if not isinstance(tables, list) or not tables:
        raise Refused(f"error: {path}: no [[repos]]")
    entries = [entry(table, i, path) for i, table in enumerate(tables)]
    seen: set[str] = set()
    for e in entries:
        key = e.repo.lower()  # GitHub's owner and repo names ignore case
        if key in seen:
            raise Refused(f"error: {path}: lists {e.repo} twice")
        seen.add(key)
    return entries


def watch_command(e: Entry, checkout: Path) -> list[str]:
    """watch_state.py for one repo, on its checkout, as JSON lines"""
    cmd = [sys.executable, str(WATCH_STATE), e.repo, "--repo-dir", str(checkout), "--json"]
    if e.incident_label is not None:
        cmd += ["--incident-label", e.incident_label]
    return cmd
