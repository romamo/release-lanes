"""The config upgrades shipmill can propose (spec 014): features that need an opt-in in a
repo's .github/shipmill.toml, which shipmill never turns on by itself (D-10, D-22).

An upgrade is decided once the config holds its key, whatever the value: `plugin_update =
false` written by hand ends the proposal as surely as accepting it. Applying one edits the
config's text, adding its line at the end of its table and keeping every other byte (comments
included), and creates the files it owns; it commits nothing.

Proposing one opens an issue labelled shipmill-upgrade whose first line is the upgrade's
marker, kept up to date on later runs. Closing that issue while the config lacks the key is a
decline: the upgrade is never proposed again, nor listed as pending."""

import copy
import json
import re
import tomllib
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from shipmill import UVX
from shipmill.autonomy import Autonomy
from shipmill.config import CONFIG_PATH
from shipmill.errors import ReleaseError
from shipmill.github import UPGRADE_LABEL, GitHub, Issue
from shipmill.version import Version

_ID = re.compile(r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$")
_KEY = re.compile(r"^[A-Za-z0-9_-]+$")
_HEADER = re.compile(r"^\s*\[\s*([A-Za-z0-9_-]+)\s*\]\s*(?:#.*)?$")
_ANY_HEADER = re.compile(r"^\s*\[")
_LINE = re.compile(r"[^\n]*\n|[^\n]+\Z")
_MARKER = re.compile(r"^<!-- shipmill-upgrade: (\S+) (\S+) -->\s*$")


@dataclass(frozen=True, slots=True)
class UpgradeId:
    """An upgrade's id, lowercase words joined by '-', such as changelog-fragments"""

    value: str

    def __post_init__(self) -> None:
        if not _ID.match(self.value):
            raise ReleaseError(
                f"an upgrade id is lowercase words joined by '-', such as plugin-update; got {self.value!r}"
            )

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True, slots=True)
class OwnedFile:
    """A file an upgrade creates, relative to the repo root"""

    path: Path
    text: str

    def __post_init__(self) -> None:
        if self.path.is_absolute() or ".." in self.path.parts:
            raise ReleaseError(f"an upgrade's file is relative to the repo root, got {self.path}")


@dataclass(frozen=True, slots=True)
class Upgrade:
    id: UpgradeId
    version: Version  # the shipmill release that last changed it
    table: str  # the config table its key goes in
    key: str
    value: str | bool
    needs_table: bool  # applies only to a config that has the table, such as [agents]
    changes: str  # what it changes, for people
    why: str
    off: str  # how to turn it off
    files: tuple[OwnedFile, ...] = ()

    def __post_init__(self) -> None:
        for name in (self.table, self.key):
            if not _KEY.match(name):
                raise ReleaseError(f"upgrade {self.id}: {name!r} is not a bare TOML key")

    @property
    def line(self) -> str:
        """The config line it adds, such as fragments = "changelog.d" """
        value = ("true" if self.value else "false") if isinstance(self.value, bool) else json.dumps(self.value)
        return f"{self.key} = {value}"

    def _table(self, raw: Mapping[str, Any]) -> Mapping[str, Any] | None:
        table = raw.get(self.table)
        if table is not None and not isinstance(table, dict):
            raise ReleaseError(f"{CONFIG_PATH}: {self.table} must be a table, got {table!r}")
        return table

    def applies(self, raw: Mapping[str, Any]) -> bool:
        return not self.needs_table or self._table(raw) is not None

    def decided(self, raw: Mapping[str, Any]) -> bool:
        """The config holds the key, whatever its value: a maintainer's decision either way"""
        table = self._table(raw)
        return table is not None and self.key in table

    def record(self) -> dict[str, object]:
        return {
            "id": str(self.id),
            "version": str(self.version),
            "table": self.table,
            "line": self.line,
            "files": [p.path.as_posix() for p in self.files],
            "changes": self.changes,
            "why": self.why,
            "off": self.off,
        }


FRAGMENTS = "changelog.d"
FRAGMENTS_README = f"""\
# Changelog fragments

Each pull request adds its changelog entry here as its own file instead of editing
`CHANGELOG.md`, so two pull requests never conflict on it. A stable release writes the
fragments into its section of `CHANGELOG.md` and deletes them; this README stays.

Name a fragment `<issue>-<slug>.md`, such as `{FRAGMENTS}/244-merged-pr-head-landed.md`, and
write what the pull request would have added under Unreleased, in the CHANGELOG's style:

```markdown
### Fixed

- Worktrees: a branch whose tip was a merged PR's head landed (#244)
```
"""

CATALOGUE: tuple[Upgrade, ...] = (
    Upgrade(
        id=UpgradeId("changelog-fragments"),
        version=Version.parse("0.37.0"),
        table="changelog",
        key="fragments",
        value=FRAGMENTS,
        needs_table=False,
        changes=f"each pull request adds its changelog entry as a file in {FRAGMENTS}/ instead of editing CHANGELOG.md",
        why="pull requests stop conflicting on CHANGELOG.md; a stable release writes the fragments into its section",
        off="delete fragments from [changelog] once no fragment is pending",
        files=(OwnedFile(Path(FRAGMENTS) / "README.md", FRAGMENTS_README),),
    ),
    Upgrade(
        id=UpgradeId("plugin-update"),
        version=Version.parse("0.37.0"),
        table="agents",
        key="plugin_update",
        value=True,
        needs_table=True,
        changes="shipmill gate updates the shipmill plugin keyed on its checkout, at most once a day (D-23)",
        why="the gate's sessions run the latest skills instead of the ones installed when it was set up",
        off="set plugin_update = false in [agents]",
    ),
)


def find(upgrade_id: UpgradeId) -> Upgrade:
    for upgrade in CATALOGUE:
        if upgrade.id == upgrade_id:
            return upgrade
    known = ", ".join(str(u.id) for u in CATALOGUE)
    raise ReleaseError(f"unknown upgrade {upgrade_id}; known: {known}")


def pending(raw: Mapping[str, Any]) -> tuple[Upgrade, ...]:
    """The upgrades that apply to the config and that it hasn't decided"""
    return tuple(u for u in CATALOGUE if u.applies(raw) and not u.decided(raw))


# -- proposal issues -------------------------------------------------------------------------


def marker(upgrade: Upgrade) -> str:
    """The first line of the upgrade's issue: its id, and the release that last changed it"""
    return f"<!-- shipmill-upgrade: {upgrade.id} {upgrade.version} -->"


def marked(issue: Issue) -> UpgradeId | None:
    """The upgrade an issue proposes, from the marker on its body's first line; None for an
    issue without one, or whose marker names no well-formed id"""
    first = issue.body.replace("\r\n", "\n").split("\n", 1)[0]
    found = _MARKER.match(first)
    return UpgradeId(found[1]) if found and _ID.match(found[1]) else None


@dataclass(frozen=True, slots=True)
class Offer:
    """A pending upgrade with no declined proposal, and its open issue when it has one"""

    upgrade: Upgrade
    issue: int | None


def _issues(issues: Sequence[Issue], upgrade: Upgrade) -> tuple[list[Issue], list[Issue]]:
    """The upgrade's open issues and its closed ones, each by number"""
    mine = sorted((i for i in issues if marked(i) == upgrade.id), key=lambda i: i.number)
    return [i for i in mine if not i.closed], [i for i in mine if i.closed]


def offers(raw: Mapping[str, Any], issues: Sequence[Issue]) -> tuple[Offer, ...]:
    """The pending upgrades, less those declined: an upgrade whose issue was closed, and has
    no open one, while the config lacks its key. issues are the shipmill-upgrade issues, open
    and closed (GitHub.labelled_issues)"""
    found = []
    for upgrade in pending(raw):
        opened, closed = _issues(issues, upgrade)
        if opened:
            found.append(Offer(upgrade, opened[0].number))
        elif not closed:
            found.append(Offer(upgrade, None))
    return tuple(found)


def title(upgrade: Upgrade) -> str:
    return f"shipmill upgrade: turn on {upgrade.id}"


def body(upgrade: Upgrade) -> str:
    created = "".join(f" and creates `{f.path.as_posix()}`" for f in upgrade.files)
    lines = [
        marker(upgrade),
        f"shipmill {upgrade.version} offers **{upgrade.id}**, which needs an opt-in in `{CONFIG_PATH.as_posix()}`."
        " shipmill never turns it on by itself.",
        "",
        f"- What it changes: {upgrade.changes}",
        f"- Why: {upgrade.why}",
        f"- To turn it off later: {upgrade.off}",
        "",
        f"It adds this line under `[{upgrade.table}]` in `{CONFIG_PATH.as_posix()}`{created}:",
        "",
        "```toml",
        f"[{upgrade.table}]",
        upgrade.line,
        "```",
        "",
        "### Accept",
        "",
        "- Answer the question shipmill's agent asks on this issue, or",
        "- Make the change yourself and open a pull request with it:",
        "",
        "```",
        f"{UVX} upgrade --apply {upgrade.id}",
        "```",
        "",
        "### Decline",
        "",
        f"Say so here, or close this issue: shipmill won't propose {upgrade.id} again.",
        "",
        f"Each release shipmill cuts updates this issue while the upgrade is pending, and closes it once"
        f" `[{upgrade.table}]` sets `{upgrade.key}`, to any value.",
    ]
    return "\n".join(lines) + "\n"


class Outcome(StrEnum):
    OPENED = "opened"
    UPDATED = "updated"
    UNCHANGED = "unchanged"
    CLOSED = "closed"  # the config holds the key: closed as completed


@dataclass(frozen=True, slots=True)
class Proposed:
    upgrade: Upgrade
    issue: int
    outcome: Outcome


def propose(raw: Mapping[str, Any], level: Autonomy, github: GitHub, fix: str) -> list[Proposed]:
    """Close as completed the open issue of each upgrade the config decided; then, unless the
    upgrade autonomy is observe, open or update one issue per offer. A failure names the
    upgrade, and fix says how to grant `issues: write` when GitHub refused it"""
    waiting = pending(raw)  # first: a malformed config fails before GitHub is asked
    try:
        issues = github.labelled_issues(UPGRADE_LABEL)
    except ReleaseError as exc:
        named = ", ".join(str(u.id) for u in waiting) or "no pending upgrade"
        raise ReleaseError(f"can't read the {UPGRADE_LABEL} issues ({named}): {exc}") from exc
    done: list[Proposed] = []
    for upgrade in CATALOGUE:
        if not upgrade.decided(raw):
            continue
        for issue in _issues(issues, upgrade)[0]:
            text = (
                f"`[{upgrade.table}]` in `{CONFIG_PATH.as_posix()}` sets `{upgrade.key}`: {upgrade.id} is decided,"
                " so this issue is done."
            )
            with _named(upgrade, fix):
                github.close_issue(issue.number, text)
            done.append(Proposed(upgrade, issue.number, Outcome.CLOSED))
    if level is Autonomy.OBSERVE:
        return done
    for offer in offers(raw, issues):
        upgrade = offer.upgrade
        wanted_title, wanted = title(upgrade), body(upgrade)
        with _named(upgrade, fix):
            if offer.issue is None:
                number = github.create_issue(wanted_title, wanted, (UPGRADE_LABEL,))
                done.append(Proposed(upgrade, number, Outcome.OPENED))
                continue
            issue = next(i for i in issues if i.number == offer.issue)
            if (issue.title, issue.body.replace("\r\n", "\n").strip()) == (wanted_title, wanted.strip()):
                done.append(Proposed(upgrade, issue.number, Outcome.UNCHANGED))
                continue
            github.update_issue(issue.number, wanted_title, wanted)
            done.append(Proposed(upgrade, issue.number, Outcome.UPDATED))
    return done


@contextmanager
def _named(upgrade: Upgrade, fix: str) -> Iterator[None]:
    """Re-raise a GitHub failure naming the upgrade, with the fix for a refused write"""
    try:
        yield
    except ReleaseError as exc:
        raise ReleaseError(f"upgrade {upgrade.id}: {exc}; if GitHub refused it (403): {fix}") from exc


@dataclass(frozen=True, slots=True)
class Applied:
    upgrade: Upgrade
    created: tuple[Path, ...]  # relative to the repo root
    kept: tuple[Path, ...]  # owned files that already existed, left as they were


def apply(root: Path, upgrade: Upgrade) -> Applied:
    """Add the upgrade's line to the config and create its files; refuse one that doesn't
    apply or is decided, and a config whose table this edit can't find"""
    path = root / CONFIG_PATH
    if not path.is_file():
        raise ReleaseError(f"no {CONFIG_PATH} in {root}")
    text = path.read_bytes().decode("utf-8")
    try:
        raw = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ReleaseError(f"{path}: {exc}") from None
    if not upgrade.applies(raw):
        raise ReleaseError(f"upgrade {upgrade.id} doesn't apply: {CONFIG_PATH} has no [{upgrade.table}] section")
    if upgrade.decided(raw):
        raise ReleaseError(
            f"upgrade {upgrade.id} is already decided: [{upgrade.table}] {upgrade.key} is set in {CONFIG_PATH}"
        )
    if upgrade.table not in raw:
        raise ReleaseError(
            f"upgrade {upgrade.id}: {CONFIG_PATH} has no [{upgrade.table}] table to add {upgrade.key} to"
        )
    edited = _insert(text, upgrade)
    expected = copy.deepcopy(dict(raw))
    expected[upgrade.table][upgrade.key] = upgrade.value
    refused = ReleaseError(f"upgrade {upgrade.id}: can't add {upgrade.line} to {CONFIG_PATH}; add it by hand")
    try:
        meant = tomllib.loads(edited)
    except tomllib.TOMLDecodeError:
        raise refused from None
    if meant != expected:  # the text edit must mean exactly the one key more
        raise refused
    created: list[Path] = []
    kept: list[Path] = []
    for owned in upgrade.files:
        target = root / owned.path
        if target.exists():
            kept.append(owned.path)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(owned.text, encoding="utf-8")
        created.append(owned.path)
    path.write_bytes(edited.encode("utf-8"))
    return Applied(upgrade, tuple(created), tuple(kept))


def _insert(text: str, upgrade: Upgrade) -> str:
    """The text with the upgrade's line after the last line of its table's body (the header
    when the body is empty), in the file's own line ending"""
    lines = _LINE.findall(text)  # not splitlines: it also breaks on U+2028 and the like, TOML only on \n
    newline = "\r\n" if "\r\n" in text else "\n"
    outside = _outside_strings(lines)
    headers = [i for i, line in enumerate(lines) if outside[i] and _ANY_HEADER.match(line)]
    found = [i for i in headers if (m := _HEADER.match(lines[i])) and m[1] == upgrade.table]
    if len(found) != 1:
        raise ReleaseError(
            f"upgrade {upgrade.id}: no [{upgrade.table}] header line in {CONFIG_PATH} to add {upgrade.key} under"
        )
    start = found[0]
    end = next((i for i in headers if i > start), len(lines))
    last = start
    for i in range(start + 1, end):
        stripped = lines[i].strip()
        if not outside[i] or (stripped and not stripped.startswith("#")):
            last = i
    if not lines[last].endswith("\n"):
        lines[last] += newline
    lines.insert(last + 1, upgrade.line + newline)
    return "".join(lines)


def _outside_strings(lines: list[str]) -> list[bool]:
    """Per line, whether it starts outside a multi-line string"""
    starts: list[bool] = []
    state: str | None = None
    for line in lines:
        starts.append(state is None)
        state = _scan(line, state)
    return starts


def _scan(line: str, state: str | None) -> str | None:
    """The multi-line string (its delimiter) the line ends inside, given the one it starts in"""
    i = 0
    while i < len(line):
        if state is not None:
            if state == '"""' and line[i] == "\\":
                i += 2
            elif line.startswith(state, i):
                state, i = None, i + 3
            else:
                i += 1
            continue
        char = line[i]
        if char == "#":
            break
        if line.startswith(('"""', "'''"), i):
            state, i = line[i : i + 3], i + 3
        elif char == '"':
            i += 1
            while i < len(line) and line[i] != '"':
                i += 2 if line[i] == "\\" else 1
            i += 1
        elif char == "'":
            close = line.find("'", i + 1)
            i = len(line) if close < 0 else close + 1
        else:
            i += 1
    return state
