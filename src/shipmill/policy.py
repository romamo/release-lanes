"""The release policy, in .github/shipmill.toml:
which lanes a project releases on, what makes each one due, what holds it, and what a
release rewrites and publishes"""

import re
import shlex
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import Any

from shipmill import environments
from shipmill.agents import AgentsConfig
from shipmill.autonomy import AutonomyPolicy
from shipmill.config import Table, read
from shipmill.environments import Environment, OperateConfig
from shipmill.errors import ReleaseError
from shipmill.lanes import Lane as Lane  # re-exported: most modules name a lane through the policy
from shipmill.roadmap import RoadmapConfig
from shipmill.schedule import Freeze, Window
from shipmill.version import Part

_MAX_QUIET = 300  # a GitHub job runs at most 6 hours; leave room for the rest of the run

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
    operate: OperateConfig  # the [operate] section, or its defaults
    autonomy: AutonomyPolicy = AutonomyPolicy()
    agents: AgentsConfig | None = None  # the gate's [agents] section; validated here, read by the gate
    roadmap: RoadmapConfig | None = None  # the [roadmap] section; validated here, read by product-intake
    fragments: str | None = None  # [changelog] fragments: the changelog fragments folder, or None (spec 013)

    @property
    def incident_label(self) -> str | None:
        """The label of the incident issues shipmill operate opens; None when no environment
        has a health URL, so no incident can open"""
        if not any(e.health for e in self.environments.values()):
            return None
        return self.operate.incident_label

    @classmethod
    def load(cls, path: Path) -> Policy:
        if not path.is_file():
            raise ReleaseError(f"no release policy at {path}")
        return cls.parse(read(path), path.name)

    @classmethod
    def parse(cls, raw: Mapping[str, Any], where: str) -> Policy:
        top = Table(raw, where)
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
            "agents",
            "operate",
            "roadmap",
        )
        changelog = top.table("changelog")
        changelog.allow("path", "style", "fragments")
        fragments = _fragments(changelog) if "fragments" in changelog.raw else None
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
        declared = environments.parse(top.table("environments", optional=True), lanes)
        operate = OperateConfig.parse(top.table("operate", optional=True))
        autonomy_table = top.table("autonomy", optional=True)
        autonomy = AutonomyPolicy.parse(autonomy_table)
        autonomy.require_environments(declared.keys(), autonomy_table.where)
        agents = AgentsConfig.parse(top.table("agents")) if "agents" in raw else None
        roadmap = RoadmapConfig.parse(top.table("roadmap")) if "roadmap" in raw else None
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
            agents=agents,
            operate=operate,
            roadmap=roadmap,
            fragments=fragments,
        )

    def rule(self, lane: Lane) -> LaneRule:
        if lane not in self.lanes:
            raise ReleaseError(f"lane {lane} is not enabled in the policy's [lanes]")
        return self.lanes[lane]

    @property
    def quiet_minutes(self) -> int:
        """The longest quiet trigger across the lanes"""
        return max((r.quiet_minutes or 0 for r in self.lanes.values()), default=0)

    @property
    def settle_minutes(self) -> int:
        """How long the settle job waits after a push: none when the mode is off, since plan then skips"""
        return 0 if self.mode is Mode.OFF else self.quiet_minutes


def _lane(lane: Lane, t: Table) -> LaneRule:
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


def _fragments(t: Table) -> str:
    """[changelog] fragments: a folder relative to the repo root, as git names it"""
    text = t.string("fragments")
    path = PurePosixPath(text)
    if not text or text == "." or path.is_absolute() or ".." in path.parts or path.as_posix() != text:
        raise ReleaseError(
            f"{t.where}: fragments is a folder relative to the repo root, such as 'changelog.d'; got {text!r}"
        )
    return text


def _version_line(i: int, t: Table) -> VersionLine:
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
