"""The release policy, in .github/shipyard.toml (or its alias .github/release-policy.toml):
which lanes a project releases on, what makes each one due, what holds it, and what a
release rewrites and publishes"""

import re
import shlex
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any

from shipyard.autonomy import AutonomyPolicy
from shipyard.errors import ReleaseError
from shipyard.schedule import Freeze, Window
from shipyard.version import Part

if TYPE_CHECKING:
    from shipyard.environments import Environment

CONFIG_PATH = Path(".github") / "shipyard.toml"
ALIAS_PATH = Path(".github") / "release-policy.toml"  # the policy's first name; the same keys
_MAX_QUIET = 300  # a GitHub job runs at most 6 hours; leave room for the rest of the run


def config_path(root: Path) -> Path:
    """The file shipyard reads its settings from: .github/shipyard.toml, else its alias
    .github/release-policy.toml. A repository may not have both."""
    found = [root / p for p in (CONFIG_PATH, ALIAS_PATH) if (root / p).is_file()]
    if len(found) > 1:
        raise ReleaseError(f"both {CONFIG_PATH} and {ALIAS_PATH} exist; keep one, git rm the other")
    if not found:
        raise ReleaseError(f"no release policy at {root / CONFIG_PATH} (or its alias {ALIAS_PATH})")
    return found[0]


class Lane(StrEnum):
    DEV = "dev"
    RC = "rc"
    STABLE = "stable"
    HOTFIX = "hotfix"

    @property
    def is_pre(self) -> bool:
        return self in (Lane.DEV, Lane.RC)


# When several lanes are due in one run, the first one here releases; the next run takes the rest
PRIORITY = (Lane.HOTFIX, Lane.STABLE, Lane.RC, Lane.DEV)


class Mode(StrEnum):
    OFF = "off"
    DRY_RUN = "dry-run"
    RELEASE = "release"


class Style(StrEnum):
    KEEP_A_CHANGELOG = "keep-a-changelog"  # "## [1.2.0] - 2026-10-01", category headings, compare links
    DASH = "dash"  # "## 1.2.0 — 2026-10-01", one ### block per entry


class VersionFiles(StrEnum):
    PYPROJECT = "pyproject"  # pyproject.toml's [project] version and the project's uv.lock entry
    NONE = "none"  # the version lives only in tags and the CHANGELOG


class BumpFrom(StrEnum):
    HEADINGS = "headings"  # the category headings of the pending entries
    PATHS = "paths"  # whether files matching minor_paths changed since the last stable tag


@dataclass(frozen=True, slots=True)
class VersionLine:
    file: str
    pattern: re.Pattern[str]
    replace: str


@dataclass(frozen=True, slots=True)
class LaneRule:
    lane: Lane
    quiet_minutes: int | None  # due once main has been quiet this long
    schedule: tuple[Window, ...]  # due once a window opened after the lane's last release
    milestone: bool  # due once the milestone named after the version has no open issues
    promote: bool  # stable only: promote a soaked rc instead of releasing main's head
    min_soak_days: int  # stable only, with promote
    marker: str  # rc only: a, b, or rc
    github_release: bool
    dispatch: tuple[str, ...]  # workflows started with -f tag=v<version>


@dataclass(frozen=True, slots=True)
class Policy:
    name: str
    mode: Mode
    branch: str
    version_files: VersionFiles
    changelog: str
    style: Style
    bump_from: BumpFrom
    bump_headings: Mapping[str, Part]
    minor_paths: tuple[str, ...]
    lanes: Mapping[Lane, LaneRule]
    blocker_label: str | None
    blocker_lanes: frozenset[Lane]
    freezes: tuple[Freeze, ...]
    freeze_lanes: frozenset[Lane]
    version_lines: tuple[VersionLine, ...]
    after_stamp: tuple[tuple[str, ...], ...]
    tag_message: str
    release_title: str
    bot_name: str
    bot_email: str
    environments: Mapping[str, Environment]
    autonomy: AutonomyPolicy = AutonomyPolicy()

    @classmethod
    def load(cls, path: Path) -> Policy:
        if not path.is_file():
            raise ReleaseError(f"no release policy at {path}")
        try:
            raw = tomllib.loads(path.read_text(encoding="utf-8"))
        except tomllib.TOMLDecodeError as exc:
            raise ReleaseError(f"{path}: {exc}") from None
        return cls.parse(raw, path.name)

    @classmethod
    def parse(cls, raw: Mapping[str, Any], where: str) -> Policy:
        top = _Table(raw, where)
        top.allow(
            "name",
            "mode",
            "branch",
            "version_files",
            "after_stamp",
            "changelog",
            "bump",
            "gates",
            "publish",
            "bot",
            "lanes",
            "version_lines",
            "environments",
            "autonomy",
        )
        changelog = top.table("changelog")
        changelog.allow("path", "style")
        bump = top.table("bump")
        bump_from = bump.enum("from", BumpFrom)
        headings: dict[str, Part] = {}
        minor_paths: tuple[str, ...] = ()
        if bump_from is BumpFrom.HEADINGS:
            bump.allow("from", *Part)
            for part in Part:
                for heading in bump.strings(part.value, default=()):
                    if heading in headings:
                        raise ReleaseError(f"{where}: [bump] lists heading {heading!r} twice")
                    headings[heading] = part
            if not headings:
                raise ReleaseError(f"{where}: [bump] from = 'headings' needs major, minor, or patch lists")
        else:
            bump.allow("from", "minor_paths")
            minor_paths = bump.strings("minor_paths")
            if not minor_paths:
                raise ReleaseError(f"{where}: [bump] from = 'paths' needs a non-empty minor_paths")
        gates = top.table("gates", optional=True)
        gates.allow("blocker_label", "blocker_lanes", "freeze", "freeze_lanes")
        publish = top.table("publish", optional=True)
        publish.allow("tag_message", "release_title")
        bot = top.table("bot", optional=True)
        bot.allow("name", "email")
        lanes_table = top.table("lanes")
        lanes_table.allow(*Lane)
        lanes = {Lane(k): _lane(Lane(k), lanes_table.table(k)) for k in lanes_table.raw}
        if not lanes:
            raise ReleaseError(f"{where}: [lanes] enables no lane")
        if Lane.STABLE in lanes and lanes[Lane.STABLE].promote and Lane.RC not in lanes:
            raise ReleaseError(f"{where}: lanes.stable promotes from rc, but lanes.rc is not enabled")
        from shipyard import environments  # it reads tables as this module does, so it imports from here

        declared = environments.parse(top.table("environments", optional=True), lanes)
        autonomy = AutonomyPolicy.parse(top.table("autonomy", optional=True).raw, f"{where} [autonomy]")
        autonomy.require_environments(declared.keys(), f"{where} [autonomy]")
        return cls(
            name=top.string("name"),
            mode=top.enum("mode", Mode),
            branch=top.string("branch", default="main"),
            version_files=top.enum("version_files", VersionFiles),
            changelog=changelog.string("path", default="CHANGELOG.md"),
            style=changelog.enum("style", Style),
            bump_from=bump_from,
            bump_headings=headings,
            minor_paths=minor_paths,
            lanes=lanes,
            blocker_label=gates.string("blocker_label", default="release-blocker") or None,
            blocker_lanes=frozenset(gates.enums("blocker_lanes", Lane, default=(Lane.RC, Lane.STABLE))),
            freezes=tuple(Freeze.parse(f) for f in gates.strings("freeze", default=())),
            freeze_lanes=frozenset(gates.enums("freeze_lanes", Lane, default=(Lane.DEV, Lane.RC, Lane.STABLE))),
            version_lines=tuple(_version_line(i, t) for i, t in enumerate(top.tables("version_lines"))),
            after_stamp=tuple(_command(c, where) for c in top.strings("after_stamp", default=())),
            tag_message=publish.string("tag_message", default="{name} {version}"),
            release_title=publish.string("release_title", default="{name} {version}"),
            bot_name=bot.string("name", default="github-actions[bot]"),
            bot_email=bot.string("email", default="41898282+github-actions[bot]@users.noreply.github.com"),
            environments=declared,
            autonomy=autonomy,
        )

    def rule(self, lane: Lane) -> LaneRule:
        if lane not in self.lanes:
            raise ReleaseError(f"lane {lane} is not enabled in the policy's [lanes]")
        return self.lanes[lane]

    @property
    def quiet_minutes(self) -> int:
        """How long the settle job waits after a push: the longest quiet trigger"""
        return max((r.quiet_minutes or 0 for r in self.lanes.values()), default=0)


def _lane(lane: Lane, t: _Table) -> LaneRule:
    triggers = ("quiet_minutes", "schedule", "milestone")
    extra = {Lane.RC: ("marker",), Lane.STABLE: ("promote_from", "min_soak_days")}.get(lane, ())
    if lane is Lane.HOTFIX:
        t.allow("github_release", "dispatch")  # a hotfix is cut by hand only
    else:
        t.allow(*triggers, *extra, "github_release", "dispatch")
    quiet = t.integer("quiet_minutes", default=None, low=0, high=_MAX_QUIET)
    promote_from = t.string("promote_from", default="") if lane is Lane.STABLE else ""
    if promote_from not in ("", "rc"):
        raise ReleaseError(f"{t.where}: promote_from is 'rc' or absent, got {promote_from!r}")
    soak = t.integer("min_soak_days", default=0, low=0, high=365) or 0
    if soak and not promote_from:
        raise ReleaseError(f"{t.where}: min_soak_days needs promote_from = 'rc'")
    marker = t.string("marker", default="rc")
    if marker not in ("a", "b", "rc"):
        raise ReleaseError(f"{t.where}: marker is a, b, or rc, got {marker!r}")
    dispatch = t.strings("dispatch", default=())
    for workflow in dispatch:
        if "/" in workflow or not workflow.endswith((".yml", ".yaml")):
            raise ReleaseError(f"{t.where}: dispatch names a file in .github/workflows, got {workflow!r}")
    return LaneRule(
        lane=lane,
        quiet_minutes=quiet,
        schedule=tuple(Window.parse(w) for w in t.strings("schedule", default=())),
        milestone=t.boolean("milestone", default=False),
        promote=bool(promote_from),
        min_soak_days=soak,
        marker=marker,
        github_release=t.boolean("github_release", default=False),
        dispatch=dispatch,
    )


def _version_line(i: int, t: _Table) -> VersionLine:
    t.allow("file", "pattern", "replace")
    try:
        pattern = re.compile(t.string("pattern"), re.MULTILINE)
    except re.error as exc:
        raise ReleaseError(f"{t.where}.pattern: {exc}") from None
    return VersionLine(t.string("file"), pattern, t.string("replace"))


def _command(text: str, where: str) -> tuple[str, ...]:
    argv = tuple(shlex.split(text))
    if not argv:
        raise ReleaseError(f"{where}: after_stamp holds an empty command")
    return argv


class _Table:
    """A TOML table read strictly: unknown keys and wrong types fail with the key's path"""

    def __init__(self, raw: Mapping[str, Any], where: str) -> None:
        self.raw = raw
        self.where = where

    def keys(self) -> list[str]:
        return list(self.raw)

    def allow(self, *keys: str) -> None:
        if unknown := sorted(set(self.raw) - set(keys)):
            raise ReleaseError(f"{self.where}: unknown keys {unknown}; allowed: {sorted(keys)}")

    def _get(self, key: str, default: object, kind: type | tuple[type, ...], label: str) -> Any:
        if key not in self.raw:
            if default is _REQUIRED:
                raise ReleaseError(f"{self.where}: {key} is required")
            return default
        value = self.raw[key]
        if not isinstance(value, kind) or (kind is int and isinstance(value, bool)):
            raise ReleaseError(f"{self.where}: {key} must be {label}, got {value!r}")
        return value

    def string(self, key: str, default: object = None) -> str:
        value: str = self._get(key, _REQUIRED if default is None else default, str, "a string")
        return value

    def boolean(self, key: str, default: bool) -> bool:
        value: bool = self._get(key, default, bool, "true or false")
        return value

    def integer(self, key: str, default: int | None, low: int, high: int) -> int | None:
        value: int | None = self._get(key, default, int, "an integer")
        if value is not None and not low <= value <= high:
            raise ReleaseError(f"{self.where}: {key} must be in {low}..{high}, got {value}")
        return value

    def strings(self, key: str, default: tuple[str, ...] | None = None) -> tuple[str, ...]:
        value = self._get(key, _REQUIRED if default is None else default, (list, tuple), "a list of strings")
        if not all(isinstance(v, str) and v for v in value):
            raise ReleaseError(f"{self.where}: {key} must be a list of non-empty strings")
        return tuple(value)

    def enum[E: StrEnum](self, key: str, kind: type[E]) -> E:
        value = self.string(key)
        try:
            return kind(value)
        except ValueError:
            raise ReleaseError(f"{self.where}: {key} must be one of {[e.value for e in kind]}, got {value!r}") from None

    def enums[E: StrEnum](self, key: str, kind: type[E], default: tuple[E, ...]) -> tuple[E, ...]:
        values = self.strings(key, default=tuple(default))
        try:
            return tuple(kind(v) for v in values)
        except ValueError:
            raise ReleaseError(f"{self.where}: {key} holds values outside {[e.value for e in kind]}") from None

    def table(self, key: str, optional: bool = False) -> _Table:
        value = self._get(key, {} if optional else _REQUIRED, dict, "a table")
        return _Table(value, f"{self.where} [{key}]")

    def tables(self, key: str) -> list[_Table]:
        value = self._get(key, [], list, "an array of tables")
        if not all(isinstance(v, dict) for v in value):
            raise ReleaseError(f"{self.where}: {key} must be an array of tables, [[{key}]]")
        return [_Table(v, f"{self.where} [[{key}]][{i}]") for i, v in enumerate(value)]


_REQUIRED = object()
