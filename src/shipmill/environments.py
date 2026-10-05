"""The [environments] section of the config: where releases deploy. An environment either
takes every release of a lane straight away (`lane`) or is promoted from another
environment later (`from`); its `workflow` does the deploying, and GitHub's deployments,
recorded because that workflow's job sets `environment:`, say what runs where"""

import re
from collections.abc import Mapping
from dataclasses import dataclass

from shipmill.config import Table
from shipmill.errors import ReleaseError
from shipmill.lanes import Lane

_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
_MAX_BAKE = 7 * 24 * 60  # a week
_ROLLBACK_AFTER = 3  # the default
_INCIDENT_LABEL = "incident"  # the default
_MAX_ROLLBACK_AFTER = 20  # failed checks in a row; at the 10-minute schedule, over three hours


@dataclass(frozen=True, slots=True)
class Environment:
    name: str
    workflow: str  # a file in .github/workflows, started with -f tag=<tag> -f environment=<name>
    lane: Lane | None  # deployed from every release of this lane; None when promoted
    source: str | None  # `from`: the environment this one is promoted from; None when it takes a lane
    health: str | None  # a URL that answers while the environment is healthy
    bake_minutes: int  # with `from`: how long the source must stay healthy before promotion

    @property
    def inputs(self) -> Mapping[str, str]:
        """The dispatch inputs besides the tag"""
        return {"environment": self.name}


@dataclass(frozen=True, slots=True)
class OperateConfig:
    """The [operate] section: when shipmill operate rolls an environment back, and the label
    of the incident issue it opens, which holds releases like the blocker label"""

    rollback_after: int  # failed health checks in a row
    incident_label: str

    @classmethod
    def parse(cls, t: Table) -> OperateConfig:
        t.allow("rollback_after", "incident_label")
        after = t.integer("rollback_after", default=_ROLLBACK_AFTER, low=1, high=_MAX_ROLLBACK_AFTER)
        assert after is not None  # a default was given
        label = t.string("incident_label", default=_INCIDENT_LABEL).strip()
        if not label:
            raise ReleaseError(f"{t.where}: incident_label must not be empty")
        return cls(after, label)


def parse(t: Table, lanes: Mapping[Lane, object]) -> Mapping[str, Environment]:
    """Each [environments.<name>] table, in the order the config declares them"""
    found = {name: _environment(name, t.table(name), lanes) for name in t.raw}
    for env in found.values():
        _chain(env, found, t.where)
    return found


def deployed_from(environments: Mapping[str, Environment], lane: Lane) -> tuple[Environment, ...]:
    """The environments a release of the lane deploys to"""
    return tuple(e for e in environments.values() if e.lane is lane)


def _environment(name: str, t: Table, lanes: Mapping[Lane, object]) -> Environment:
    if not _NAME.fullmatch(name):
        raise ReleaseError(f"{t.where}: an environment name is letters, digits, '.', '_', and '-'")
    t.allow("lane", "from", "workflow", "health", "bake_minutes")
    lane_text = t.string("lane", default="")
    source = t.string("from", default="")
    if bool(lane_text) == bool(source):
        raise ReleaseError(f"{t.where}: set exactly one of lane (deploy each release) or from (promote)")
    lane = None
    if lane_text:
        try:
            lane = Lane(lane_text)
        except ValueError:
            raise ReleaseError(f"{t.where}: lane must be one of {[e.value for e in Lane]}, got {lane_text!r}") from None
        if lane not in lanes:
            raise ReleaseError(f"{t.where}: lane {lane} is not enabled in [lanes]")
    workflow = t.string("workflow")
    if "/" in workflow or not workflow.endswith((".yml", ".yaml")):
        raise ReleaseError(f"{t.where}: workflow names a file in .github/workflows, got {workflow!r}")
    health = t.string("health", default="")
    if health and not re.fullmatch(r"https?://\S+", health):
        raise ReleaseError(f"{t.where}: health must be an http(s) URL, got {health!r}")
    bake = t.integer("bake_minutes", default=None, low=0, high=_MAX_BAKE)
    if bake is not None and lane is not None:
        raise ReleaseError(f"{t.where}: bake_minutes applies to an environment promoted with from, not lane")
    return Environment(name, workflow, lane, source or None, health or None, bake or 0)


def _chain(env: Environment, found: Mapping[str, Environment], where: str) -> None:
    """A `from` chain ends at an environment that takes a lane, with no cycle on the way"""
    seen = [env.name]
    current = env
    while current.source is not None:
        if current.source not in found:
            raise ReleaseError(f"{where} [{current.name}]: from names an unknown environment {current.source!r}")
        if current.source in seen:
            raise ReleaseError(f"{where}: environments promote in a cycle: {' -> '.join([*seen, current.source])}")
        seen.append(current.source)
        current = found[current.source]
