"""How far shipyard goes by itself at each stage, from the config's [autonomy] section, and
the stop switch: an open issue labelled shipyard-hold.

- observe: plan and report only
- propose: open or update an issue saying what it would do; a person does it
- act: do it

The hold turns every act into propose for the whole repository. Release autonomy is read by
the planner; deploy and rollback autonomy are parsed and exposed here for the deploy and
rollback stages to call once they exist."""

import re
from collections.abc import Collection, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol

from shipyard.errors import ReleaseError

HOLD_LABEL = "shipyard-hold"
_ENVIRONMENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")


class Autonomy(StrEnum):
    OBSERVE = "observe"
    PROPOSE = "propose"
    ACT = "act"


class StageKind(StrEnum):
    RELEASE = "release"
    DEPLOY = "deploy"
    ROLLBACK = "rollback"


@dataclass(frozen=True, slots=True)
class EnvironmentName:
    """A deploy environment's name, as a key under [autonomy.deploy]"""

    name: str

    def __post_init__(self) -> None:
        if not _ENVIRONMENT.match(self.name):
            raise ReleaseError(f"an environment name is letters, digits, '.', '_', or '-'; got {self.name!r}")

    def __str__(self) -> str:
        return self.name


@dataclass(frozen=True, slots=True)
class Stage:
    kind: StageKind
    environment: EnvironmentName | None = None

    def __post_init__(self) -> None:
        if (self.kind is StageKind.DEPLOY) != (self.environment is not None):
            raise ReleaseError("a deploy stage names its environment, and only a deploy stage does")

    @classmethod
    def release(cls) -> Stage:
        return cls(StageKind.RELEASE)

    @classmethod
    def rollback(cls) -> Stage:
        return cls(StageKind.ROLLBACK)

    @classmethod
    def deploy(cls, environment: EnvironmentName) -> Stage:
        return cls(StageKind.DEPLOY, environment)

    def __str__(self) -> str:
        return f"deploy.{self.environment}" if self.environment else self.kind.value


class _Issues(Protocol):
    def open_issues(self, label: str) -> list[str]: ...


@dataclass(frozen=True, slots=True)
class Hold:
    """The open shipyard-hold issues, as '#N title'; none means no hold"""

    issues: tuple[str, ...] = ()

    @classmethod
    def read(cls, github: _Issues) -> Hold:
        return cls(tuple(github.open_issues(HOLD_LABEL)))

    @property
    def on(self) -> bool:
        return bool(self.issues)

    @property
    def reason(self) -> str:
        numbers = ", ".join(issue.split(maxsplit=1)[0] for issue in self.issues)
        return f"held by {HOLD_LABEL} {numbers}"


@dataclass(frozen=True, slots=True)
class AutonomyPolicy:
    """The configured autonomy per stage; an environment not listed deploys at act (D-7)"""

    release: Autonomy = Autonomy.ACT
    rollback: Autonomy = Autonomy.ACT
    deploy: Mapping[EnvironmentName, Autonomy] = field(default_factory=dict)

    @classmethod
    def parse(cls, raw: Mapping[str, Any], where: str) -> AutonomyPolicy:
        if unknown := sorted(set(raw) - {kind.value for kind in StageKind}):
            raise ReleaseError(f"{where}: unknown keys {unknown}; allowed: {sorted(StageKind)}")
        deploy = raw.get("deploy", {})
        if not isinstance(deploy, dict):
            raise ReleaseError(f"{where}: deploy is a table of environments, such as deploy.production = 'propose'")
        return cls(
            release=_level(raw, "release", where),
            rollback=_level(raw, "rollback", where),
            deploy={EnvironmentName(name): _level(deploy, name, f"{where} [deploy]") for name in deploy},
        )

    def require_environments(self, known: Collection[str], where: str) -> None:
        """Each deploy.<name> names an environment the config declares in [environments]"""
        for name in self.deploy:
            if name.name not in known:
                declared = ", ".join(known) or "none"
                raise ReleaseError(f"{where}: deploy.{name} names no environment in [environments]; known: {declared}")

    def configured(self, stage: Stage) -> Autonomy:
        if stage.kind is StageKind.RELEASE:
            return self.release
        if stage.kind is StageKind.ROLLBACK:
            return self.rollback
        assert stage.environment is not None  # Stage guarantees it
        return self.deploy.get(stage.environment, Autonomy.ACT)

    def effective(self, stage: Stage, hold: Hold) -> Autonomy:
        """The stage's autonomy under the hold: an open hold turns act into propose"""
        level = self.configured(stage)
        return Autonomy.PROPOSE if level is Autonomy.ACT and hold.on else level

    def cause(self, stage: Stage, hold: Hold) -> str:
        """Why the stage doesn't act: the hold, or the configured level. Under propose the
        hold still names itself, as it also refuses a person starting the stage by hand"""
        if self.configured(stage) is not Autonomy.OBSERVE and hold.on:
            return hold.reason
        return f"{stage} autonomy is {self.configured(stage)}"

    def stages(self) -> tuple[Stage, ...]:
        """The stages doctor reports: release, rollback, and each configured environment"""
        envs = sorted(self.deploy, key=str)
        return (Stage.release(), *(Stage.deploy(e) for e in envs), Stage.rollback())


def _level(raw: Mapping[str, Any], key: str, where: str) -> Autonomy:
    value = raw.get(key, Autonomy.ACT.value)
    if not isinstance(value, str) or value not in set(Autonomy):
        raise ReleaseError(f"{where}: {key} must be one of {[a.value for a in Autonomy]}, got {value!r}")
    return Autonomy(value)
