"""shipyard operate: watch each environment in [environments] and move releases along.

Each run reads what runs where from GitHub's deployments, checks each environment's health
URL, records the result as a deployment status (the health history: no state lives outside
GitHub, D-6), and then:

- promotes: an environment with `from` gets the tag its source has run healthily for
  `bake_minutes`, counted from the source deployment's success, every check since passing
- catches a missed deploy: an environment with `lane` that is behind its lane's newest tag
  (land's dispatch failed, say) gets that tag

Either deploy follows the environment's deploy autonomy under the hold: act dispatches its
workflow, propose opens or updates one issue that `--approve <env>` acts on, observe only
reports. shipyard deploys a tag to an environment at most once: a deployment of that tag
there, in any state, means it was tried.

Health: a GET of `health` answering 2xx within 10 s. When the body is a JSON object with a
string `version`, it must name the deployed release (`1.2.0` or `v1.2.0`), so a stale
instance answering 200 doesn't count as healthy. Statuses are written only when the state
changes: `in_progress` while baking, `success` once baked (or at once when nothing is
promoted from the environment), `failure` on a failed check, which restarts the bake."""

import datetime as dt
import http.client
import json
import re
import time
import urllib.error
import urllib.request
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol

from shipyard.autonomy import HOLD_LABEL, Autonomy, EnvironmentName, Hold, Stage
from shipyard.doctor import OPERATE_CALLER
from shipyard.environments import Environment
from shipyard.errors import ReleaseError
from shipyard.github import Deployment, DeploymentState, DeploymentStatus, GitHub
from shipyard.gitrepo import Tag
from shipyard.policy import Lane, Policy
from shipyard.propose import upsert
from shipyard.version import PATTERN, TAG_PREFIX, Version

HEALTH_TIMEOUT = 10.0  # seconds
MISSED_GRACE = dt.timedelta(minutes=30)  # land dispatches right after tagging; give that deploy time to show
STATUS_PREFIX = "shipyard health"  # starts the description of every status shipyard writes
_BODY_LIMIT = 64 * 1024
_DESCRIPTION_LIMIT = 140  # GitHub's limit on a status description
_SHA = re.compile(r"[0-9a-f]{40}")


# -- health ----------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class HttpResponse:
    status: int
    body: str
    seconds: float  # from the request to the end of the body


class Unreachable(Exception):
    """The health URL gave no HTTP answer: refused, timed out, or a broken response"""


class Http(Protocol):
    def get(self, url: str, timeout: float) -> HttpResponse:
        """GET the URL; raises Unreachable when no HTTP answer comes back"""
        ...


class UrllibHttp:
    def get(self, url: str, timeout: float) -> HttpResponse:
        request = urllib.request.Request(url, headers={"User-Agent": "shipyard-operate"})
        start = time.monotonic()
        try:
            with urllib.request.urlopen(request, timeout=timeout) as answer:
                body = answer.read(_BODY_LIMIT)
                status = int(answer.status)
        except urllib.error.HTTPError as exc:  # 4xx and 5xx: an answer, not a broken connection
            return HttpResponse(exc.code, "", time.monotonic() - start)
        except (OSError, http.client.HTTPException) as exc:  # URLError and timeouts are OSErrors
            raise Unreachable(str(getattr(exc, "reason", exc))) from exc
        return HttpResponse(status, body.decode(errors="replace"), time.monotonic() - start)


@dataclass(frozen=True, slots=True)
class Probe:
    healthy: bool
    detail: str


def probe(http: Http, url: str, deployed: Version) -> Probe:
    try:
        answer = http.get(url, HEALTH_TIMEOUT)
    except Unreachable as exc:
        return Probe(False, f"unreachable: {exc}")
    if not 200 <= answer.status < 300:
        return Probe(False, f"HTTP {answer.status}")
    if answer.seconds > HEALTH_TIMEOUT:
        return Probe(False, f"answered in {answer.seconds:.1f} s, over {HEALTH_TIMEOUT:.0f} s")
    named = named_version(answer.body)
    if named is not None and named not in (str(deployed), deployed.tag):
        return Probe(False, f"answers version {named}, but {deployed.tag} is deployed")
    return Probe(True, f"HTTP {answer.status}" + (f", version {named}" if named else ""))


def named_version(body: str) -> str | None:
    """The version a health body names: a JSON object's top-level string `version`; any
    other body names none"""
    try:
        found = json.loads(body)
    except json.JSONDecodeError:
        return None
    if isinstance(found, dict) and isinstance(found.get("version"), str):
        return str(found["version"])
    return None


# -- what runs where -------------------------------------------------------------------------


def tag_of(deployment: Deployment, tags: Sequence[Tag]) -> Version | None:
    """The release a deployment runs: its ref when that is a release tag (shipyard dispatches
    deploys on the tag), else the newest release tag on its ref when the ref is a sha"""
    ref = deployment.ref.removeprefix("refs/tags/")
    if ref.startswith(TAG_PREFIX) and PATTERN.fullmatch(ref[len(TAG_PREFIX) :]):
        return Version.of_tag(ref)
    if _SHA.fullmatch(ref):
        on = [t.version for t in tags if t.commit == ref]
        return max(on) if on else None
    return None


def _ours(status: DeploymentStatus) -> bool:
    return status.description.startswith(STATUS_PREFIX)


@dataclass(frozen=True, slots=True)
class Observed:
    """One environment as this run found it"""

    env: Environment
    deployments: tuple[Deployment, ...]  # newest first
    current: Deployment | None  # the newest deployment that reached success
    statuses: tuple[DeploymentStatus, ...]  # the current deployment's, oldest first
    tag: Version | None
    probe: Probe | None  # None: no health URL, or nothing to check
    bake_start: dt.datetime | None  # when the current healthy stretch began
    failing_since: dt.datetime | None  # when the current run of failed checks began (#31 builds on it)

    def healthy_for(self, now: dt.datetime) -> dt.timedelta | None:
        """How long the current deployment has been healthy; None while it isn't. Without a
        health URL, the time since it succeeded"""
        if self.bake_start is None or (self.probe is not None and not self.probe.healthy):
            return None
        return now - self.bake_start

    def tried(self, tag: Version) -> Deployment | None:
        """The newest deployment of the tag here, in any state"""
        hits = [d for d in self.deployments if d.ref.removeprefix("refs/tags/") == tag.tag]
        return hits[0] if hits else None


def observe(github: GitHub, http: Http, env: Environment, tags: Sequence[Tag], now: dt.datetime) -> Observed:
    deployments = tuple(github.deployments(env.name))
    for deployment in deployments:
        statuses = tuple(github.deployment_statuses(deployment.id))
        succeeded = [s for s in statuses if s.state is DeploymentState.SUCCESS]
        if succeeded:
            break
    else:
        return Observed(env, deployments, None, (), None, None, None, None)
    tag = tag_of(deployment, tags)
    checked = probe(http, env.health, tag) if env.health and tag is not None else None
    ours = [s for s in statuses if _ours(s)]
    failures = [s for s in ours if s.state is DeploymentState.FAILURE]
    bake_start: dt.datetime | None = succeeded[0].created_at
    if failures:  # a failed check restarts the bake: it counts from the first passing one after
        recovered = [s for s in ours if s.id > failures[-1].id]
        bake_start = recovered[0].created_at if recovered else now
    failing_since = None
    if checked is not None and not checked.healthy:
        bake_start = None
        # one failure status per run of failed checks: the newest marks where this run began
        failing_since = ours[-1].created_at if ours and ours[-1].state is DeploymentState.FAILURE else now
    return Observed(env, deployments, deployment, statuses, tag, checked, bake_start, failing_since)


# -- one run ---------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Report:
    environment: str
    tag: str
    health: str
    action: str


@dataclass(frozen=True, slots=True)
class Wanted:
    """A deploy this run would make: the tag, and why"""

    tag: Version
    why: str
    promote: bool  # from a source environment, rather than a missed lane deploy


def _minutes(span: dt.timedelta) -> int:
    return int(span.total_seconds() // 60)


def _bake_target(env: Environment, environments: Mapping[str, Environment]) -> int:
    """The longest bake any environment promoted from this one waits for; 0 when none is"""
    return max((e.bake_minutes for e in environments.values() if e.source == env.name), default=0)


def _health_text(seen: Observed, target: int, now: dt.datetime) -> str:
    if seen.current is None:
        return "no deployment"
    if not seen.env.health:
        return "no health URL"
    if seen.tag is None or seen.probe is None:
        return "not checked"
    if not seen.probe.healthy:
        since = f", failing for {_minutes(now - seen.failing_since)} min" if seen.failing_since else ""
        return f"unhealthy: {seen.probe.detail}{since}"
    healthy = seen.healthy_for(now) or dt.timedelta()
    if target and _minutes(healthy) < target:
        return f"healthy ({seen.probe.detail}), baking {_minutes(healthy)} of {target} min"
    return f"healthy ({seen.probe.detail})"


def _status(seen: Observed, target: int, now: dt.datetime) -> tuple[DeploymentState, str] | None:
    """The status the current deployment should carry, when it differs from its latest"""
    if seen.current is None or seen.probe is None:
        return None
    if not seen.probe.healthy:
        wanted = (DeploymentState.FAILURE, f"{STATUS_PREFIX}: {seen.probe.detail}")
    else:
        healthy = seen.healthy_for(now) or dt.timedelta()
        if _minutes(healthy) >= target:
            wanted = (DeploymentState.SUCCESS, f"{STATUS_PREFIX}: baked" if target else f"{STATUS_PREFIX}: healthy")
        else:
            wanted = (DeploymentState.IN_PROGRESS, f"{STATUS_PREFIX}: baking for {target} min")
    if seen.statuses and seen.statuses[-1].state is wanted[0]:
        return None
    return wanted[0], wanted[1][:_DESCRIPTION_LIMIT]


def _lane_versions(lane: Lane, tags: Sequence[Tag]) -> list[Tag] | None:
    """The tags a lane releases; None for hotfix, whose tags look like stable ones"""
    if lane is Lane.DEV:
        return [t for t in tags if t.version.dev is not None]
    if lane is Lane.RC:
        return [t for t in tags if t.version.pre is not None and t.version.dev is None]
    if lane is Lane.STABLE:
        return [t for t in tags if t.version.is_stable]
    return None


def _wanted(seen: Observed, observed: Mapping[str, Observed], tags: Sequence[Tag], now: dt.datetime) -> Wanted | str:
    """The deploy this environment waits for, or why none"""
    env = seen.env
    if env.source is not None:
        source = observed[env.source]
        if source.current is None:
            return f"{env.source} has no deployment to promote"
        if source.tag is None:
            return f"{env.source}'s deployment ref {source.current.ref!r} names no release tag"
        if seen.tag is not None and seen.tag >= source.tag:
            return f"on {seen.tag.tag}" if seen.tag == source.tag else f"ahead of {env.source} ({source.tag.tag})"
        healthy = source.healthy_for(now)
        if healthy is None:
            return f"{env.source} is unhealthy on {source.tag.tag}"
        if _minutes(healthy) < env.bake_minutes:
            return f"{source.tag.tag} baking on {env.source}: {_minutes(healthy)} of {env.bake_minutes} min"
        return Wanted(source.tag, f"healthy on {env.source} for the {env.bake_minutes} min bake", promote=True)
    assert env.lane is not None  # an environment takes a lane or a source
    released = _lane_versions(env.lane, tags)
    if released is None:
        return "up to date (hotfix tags aren't told from stable ones, so a missed hotfix deploy isn't caught)"
    newest = max(released, key=lambda t: t.version) if released else None
    if newest is None:
        return f"no {env.lane} release yet"
    if seen.current is None:
        return f"no deployment yet; {newest.name} is the newest {env.lane} release"
    if seen.tag is None:
        return f"deployment ref {seen.current.ref!r} names no release tag"
    if seen.tag >= newest.version:
        return "up to date"
    if now - newest.date < MISSED_GRACE:
        return f"{newest.name} just landed; its deploy may still be starting"
    return Wanted(newest.version, f"the newest {env.lane} release, which its deploy missed", promote=False)


def deploy_marker(env: str) -> str:
    return f"<!-- shipyard:propose deploy={env} -->"


def _tag_line(tag: Version) -> str:
    return f"<!-- shipyard:tag={tag.tag} -->"


def proposal_title(env: Environment, wanted: Wanted) -> str:
    return f"Ready to {'promote' if wanted.promote else 'deploy'} {wanted.tag.tag} to {env.name}"


def proposal_body(env: Environment, wanted: Wanted, cause: str) -> str:
    approve = f"gh workflow run {OPERATE_CALLER.name} -f approve={env.name} -f dry-run=false"
    if cause.startswith(f"held by {HOLD_LABEL}"):
        how = f"Close the open `{HOLD_LABEL}` issues first; then approve it:"
    else:
        how = "To deploy it, approve it:"
    return "\n".join(
        [
            deploy_marker(env.name),
            _tag_line(wanted.tag),
            f"shipyard would deploy **{wanted.tag.tag}** to {env.name} now ({wanted.why}), but {cause}.",
            "",
            how,
            "",
            "```",
            approve,
            "```",
            "",
            f"Approving starts `{env.workflow}` with this tag once and closes this issue. Each operate run"
            " updates this issue while the deploy waits.",
        ]
    )


def _act(
    policy: Policy,
    github: GitHub,
    env: Environment,
    wanted: Wanted,
    hold: Hold,
    dry_run: bool,
) -> str:
    stage = Stage.deploy(EnvironmentName(env.name))
    level = policy.autonomy.effective(stage, hold)
    would = "would " if dry_run else ""
    if level is Autonomy.ACT:
        if not dry_run:
            github.dispatch(env.workflow, wanted.tag.tag, wanted.tag.tag, env.inputs)
        return f"{would}dispatch {env.workflow} with {wanted.tag.tag} ({wanted.why})"
    cause = policy.autonomy.cause(stage, hold)
    if level is Autonomy.OBSERVE:
        return f"would deploy {wanted.tag.tag} ({wanted.why}), but {cause}"
    if dry_run:
        return f"would propose {wanted.tag.tag} ({wanted.why}): {cause}"
    fix = f"grant `issues: write` to the job that calls operate.yml in {OPERATE_CALLER}"
    number, outcome = upsert(
        github, deploy_marker(env.name), proposal_title(env, wanted), proposal_body(env, wanted, cause), fix
    )
    return f"proposal #{number} {outcome}: {wanted.tag.tag} ({cause})"


def operate(
    policy: Policy, tags: Sequence[Tag], github: GitHub, http: Http, now: dt.datetime, dry_run: bool
) -> list[Report]:
    environments = policy.environments
    hold = Hold.read(github) if environments else Hold()
    observed = {name: observe(github, http, env, tags, now) for name, env in environments.items()}
    reports = []
    for name, seen in observed.items():
        target = _bake_target(seen.env, environments)
        actions = []
        if (status := _status(seen, target, now)) is not None:
            assert seen.current is not None  # a status is only wanted for a current deployment
            if not dry_run:
                github.create_deployment_status(seen.current.id, *status)
            actions.append(f"{'would mark' if dry_run else 'marked'} {status[0]}")
        wanted = _wanted(seen, observed, tags, now)
        if isinstance(wanted, str):
            actions.append(wanted)
        elif (tried := seen.tried(wanted.tag)) is not None:
            states = github.deployment_statuses(tried.id)
            state = states[-1].state if states else DeploymentState.PENDING
            actions.append(f"{wanted.tag.tag} was deployed here already ({state}); shipyard doesn't deploy it again")
        else:
            actions.append(_act(policy, github, seen.env, wanted, hold, dry_run))
        tag = seen.tag.tag if seen.tag else (seen.current.ref if seen.current else "-")
        reports.append(Report(name, tag, _health_text(seen, target, now), "; ".join(actions)))
    return reports


def approve(policy: Policy, github: GitHub, name: str, dry_run: bool) -> str:
    """Deploy the tag a proposal issue names, once, and close the issue; a person acting, so
    it goes under propose and observe, but never under the hold (D-8)"""
    env = policy.environments.get(name)
    if env is None:
        known = ", ".join(policy.environments) or "none"
        raise ReleaseError(f"no environment {name!r} in [environments]; known: {known}")
    hold = Hold.read(github)
    if hold.on:
        raise ReleaseError(f"{hold.reason}; close it to approve a deploy")
    issue = github.find_issue(deploy_marker(name))
    if issue is None:
        raise ReleaseError(f"no open proposal to deploy to {name}; operate opens one when a deploy waits on approval")
    found = re.search(r"<!-- shipyard:tag=(?P<tag>\S+) -->", issue.body)
    if found is None:
        raise ReleaseError(f"proposal #{issue.number} names no tag; the next operate run rewrites it")
    tag = Version.of_tag(found["tag"])
    tried = [d for d in github.deployments(name) if d.ref.removeprefix("refs/tags/") == tag.tag]
    if tried:
        raise ReleaseError(
            f"{tag.tag} was deployed to {name} already (deployment {tried[0].id}); close #{issue.number}"
        )
    if dry_run:
        return f"would dispatch {env.workflow} with {tag.tag} to {name} and close #{issue.number}"
    github.dispatch(env.workflow, tag.tag, tag.tag, env.inputs)
    github.close_issue(issue.number, f"Approved: started `{env.workflow}` with {tag.tag} for {name}.")
    return f"dispatched {env.workflow} with {tag.tag} to {name}; closed #{issue.number}"


def summary(reports: Sequence[Report], dry_run: bool) -> str:
    if not reports:
        return "No [environments] in the config: nothing to operate.\n"
    lines = [
        f"### shipyard operate{' (dry run)' if dry_run else ''}",
        "",
        "| Environment | Tag | Health | Action |",
        "|---|---|---|---|",
    ]
    lines += [
        f"| {r.environment} | {r.tag} | {r.health.replace('|', '/')} | {r.action.replace('|', '/')} |" for r in reports
    ]
    return "\n".join(lines) + "\n"
