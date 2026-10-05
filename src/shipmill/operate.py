"""shipmill operate: watch each environment in [environments] and move releases along.

Each run reads what runs where from GitHub's deployments, checks each environment's health
URL, records the result as a deployment status (the health history: no state lives outside
GitHub, D-6), and then:

- promotes: an environment with `from` gets the tag its source has run healthily for
  `bake_minutes`, counted from the source deployment's success, every check since passing
- catches a missed deploy: an environment with `lane` that is behind its lane's newest tag
  (land's dispatch failed, say) gets that tag

Either deploy follows the environment's deploy autonomy under the hold: act dispatches its
workflow, propose opens or updates one issue that `--approve <env>` acts on, observe only
reports. A proposal closes once the environment runs its tag or a later one, whoever
deployed it. shipmill deploys a tag to an environment at most once: a deployment of that tag
there, in any state, means it was tried, and so does a run of the environment's workflow on
the tag (still queued, or failed before its deploy job made a deployment).

Health: a GET of `health` answering 2xx within 10 s. When the body is a JSON object with a
string `version`, it must name the deployed release (`1.2.0` or `v1.2.0`), so a stale
instance answering 200 doesn't count as healthy. Statuses are written only when the state
changes: `in_progress` while baking, `success` once baked (or at once when nothing is
promoted from the environment). A failed check writes `failure`, which restarts the bake,
once per check until `[operate] rollback_after` of them stand in a row: those statuses are
the count of failed checks, so a failing stretch writes at most that many.

Rollback and incidents: at `rollback_after` failed checks in a row, operate opens one issue
labelled `[operate] incident_label` per environment and bad tag (found again by a hidden
line holding the environment, the tags, and its state), and rolls the environment back to
the previous tag that reached success there, under the rollback autonomy and the hold: act
starts the environment's workflow on that tag; propose (or act under a hold, D-8) has the
incident say so, and `--approve-rollback <env>` does it once; observe only says so. A
rollback is the one deploy exempt from deploying a tag at most once: its tag ran there
before by definition. It is still started once: a deployment of it after the bad one, or a
run of the workflow on it since the checks began failing, means it was. Later runs comment
on the incident instead of opening another: when the environment is healthy again, and
when the rollback's tag fails its checks too, where shipmill stops rather than guess a
second time. The planner holds the blocker lanes while an incident is open."""

import datetime as dt
import http.client
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from shipmill.autonomy import HOLD_LABEL, Autonomy, EnvironmentName, Hold, Stage
from shipmill.doctor import OPERATE_CALLER
from shipmill.environments import Environment
from shipmill.errors import ReleaseError
from shipmill.github import Deployment, DeploymentState, DeploymentStatus, GitHub, Issue, WorkflowRun
from shipmill.gitrepo import Tag
from shipmill.policy import Lane, Policy
from shipmill.propose import find_proposal, upsert
from shipmill.version import PATTERN, TAG_PREFIX, Version

HEALTH_TIMEOUT = 10.0  # seconds
MISSED_GRACE = dt.timedelta(minutes=30)  # land dispatches right after tagging; give that deploy time to show
STATUS_PREFIX = "shipmill health"  # starts the description of every status shipmill writes
_BODY_LIMIT = 64 * 1024
_DESCRIPTION_LIMIT = 140  # GitHub's limit on a status description
_EXCERPT_LIMIT = 300  # characters of a failing health body an incident quotes
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
        request = urllib.request.Request(url, headers={"User-Agent": "shipmill-operate"})
        start = time.monotonic()
        try:
            with urllib.request.urlopen(request, timeout=timeout) as answer:
                body = answer.read(_BODY_LIMIT)
                status = int(answer.status)
        except urllib.error.HTTPError as exc:  # 4xx and 5xx: an answer, not a broken connection
            try:
                body = exc.read(_BODY_LIMIT) if exc.fp is not None else b""
            except OSError, http.client.HTTPException:  # the status is the answer; its body is a bonus
                body = b""
            return HttpResponse(exc.code, body.decode(errors="replace"), time.monotonic() - start)
        except (OSError, http.client.HTTPException) as exc:  # URLError and timeouts are OSErrors
            raise Unreachable(str(getattr(exc, "reason", exc))) from exc
        return HttpResponse(status, body.decode(errors="replace"), time.monotonic() - start)


@dataclass(frozen=True, slots=True)
class Probe:
    healthy: bool
    detail: str
    excerpt: str = ""  # the start of the body, on one line, for an incident to quote


_REDACTED = "[redacted]"
_SECRET_VALUE = re.compile(  # the value after a key that names a secret: "password": "x", token=x
    r"""(?i)(["']?[\w-]*(?:passw|pwd|secret|token|api[_-]?key|auth|cookie|session|credential)[\w-]*["']?"""
    r"""\s*[:=]\s*)("[^"]*"|'[^']*'|[^\s,;&}]+)"""
)
_BEARER = re.compile(r"(?i)\b(bearer|basic)\s+[^\s\"',;}]+")
_URL_USER = re.compile(r"(\w+://)[^/\s@]+@")  # credentials in a URL the body names
_OPAQUE = re.compile(r"(?<![\w+/=.-])(?=[\w+/=.-]*\d)(?=[\w+/=.-]*[A-Za-z])[\w+/=.-]{32,}")  # a key or hash


def _redacted(match: re.Match[str]) -> str:
    value = match[2]
    quote = value[0] if value[0] in "\"'" else ""
    return f"{match[1]}{quote}{_REDACTED}{quote}"


def excerpt(body: str) -> str:
    """The start of a health body on one line, capped, with no backticks to break a code span
    and nothing that looks like a secret: a secret key's value, a bearer token, a URL's
    credentials, or a long opaque string (an incident is an issue anyone with read sees)"""
    flat = " ".join(body.split()).replace("`", "'")
    flat = _SECRET_VALUE.sub(_redacted, flat)
    flat = _BEARER.sub(lambda m: f"{m[1]} {_REDACTED}", flat)
    flat = _URL_USER.sub(lambda m: f"{m[1]}{_REDACTED}@", flat)
    flat = _OPAQUE.sub(_REDACTED, flat)
    return flat if len(flat) <= _EXCERPT_LIMIT else flat[: _EXCERPT_LIMIT - 3] + "..."


def shown_url(url: str) -> str:
    """The health URL as an incident shows it: no user, password, query, or fragment, which
    may carry a token"""
    parts = urllib.parse.urlsplit(url)
    host = parts.hostname or ""
    return f"{parts.scheme}://{host}{f':{parts.port}' if parts.port else ''}{parts.path}"


def probe(http: Http, url: str, deployed: Version) -> Probe:
    try:
        answer = http.get(url, HEALTH_TIMEOUT)
    except Unreachable as exc:
        return Probe(False, f"unreachable: {exc}")
    quoted = excerpt(answer.body)
    if not 200 <= answer.status < 300:
        return Probe(False, f"HTTP {answer.status}", quoted)
    if answer.seconds > HEALTH_TIMEOUT:
        return Probe(False, f"answered in {answer.seconds:.1f} s, over {HEALTH_TIMEOUT:.0f} s", quoted)
    named = named_version(answer.body)
    if named is not None and named not in (str(deployed), deployed.tag):
        return Probe(False, f"answers version {named}, but {deployed.tag} is deployed", quoted)
    return Probe(True, f"HTTP {answer.status}" + (f", version {named}" if named else ""), quoted)


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
    """The release a deployment runs: its ref when that is a release tag (shipmill dispatches
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
    failing_since: dt.datetime | None  # when the current run of failed checks began
    failed_checks: int  # failed checks in a row, this run's included; 0 while healthy or unchecked

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
        return Observed(env, deployments, None, (), None, None, None, None, 0)
    tag = tag_of(deployment, tags)
    checked = probe(http, env.health, tag) if env.health and tag is not None else None
    ours = [s for s in statuses if _ours(s)]
    failures = [s for s in ours if s.state is DeploymentState.FAILURE]
    bake_start: dt.datetime | None = succeeded[0].created_at
    if failures:  # a failed check restarts the bake: it counts from the first passing one after
        recovered = [s for s in ours if s.id > failures[-1].id]
        bake_start = recovered[0].created_at if recovered else now
    failing_since = None
    failed_checks = 0
    if checked is not None and not checked.healthy:
        bake_start = None
        # each failed check writes a failure status (up to rollback_after): the ones that end
        # the history are the checks failed in a row before this one, the first of them its start
        trailing = 0
        for status in reversed(statuses):
            if not (_ours(status) and status.state is DeploymentState.FAILURE):
                break
            trailing += 1
        failing_since = statuses[-trailing].created_at if trailing else now
        failed_checks = trailing + 1
    return Observed(env, deployments, deployment, statuses, tag, checked, bake_start, failing_since, failed_checks)


# -- rollback and incidents ------------------------------------------------------------------


class IncidentState(StrEnum):
    FAILING = "failing"  # no rollback made: none to make, or rollback autonomy is observe
    PROPOSED = "proposed"  # a rollback waits on --approve-rollback
    ROLLED_BACK = "rolled-back"  # shipmill started the rollback
    STOPPED = "stopped"  # the rollback's tag fails its checks too: no second rollback
    HEALTHY = "healthy"  # healthy again, on the bad tag or the rollback's


_INCIDENT = re.compile(
    r"<!-- shipmill:incident env=(?P<env>\S+) tag=(?P<tag>\S+) to=(?P<to>\S+) state=(?P<state>\S+) -->"
)


def incident_line(env: str, tag: Version, to: Version | None, state: IncidentState) -> str:
    """The hidden line that finds an environment's incident again, and keeps its state"""
    return f"<!-- shipmill:incident env={env} tag={tag.tag} to={to.tag if to else '-'} state={state} -->"


@dataclass(frozen=True, slots=True)
class Incident:
    """An incident issue shipmill opened: the environment, its bad tag, the rollback's tag"""

    issue: Issue
    env: str
    tag: Version
    to: Version | None
    state: IncidentState

    @classmethod
    def read(cls, issue: Issue) -> Incident | None:
        """None for an issue with the label that shipmill didn't open"""
        found = _INCIDENT.search(issue.body)
        if found is None:
            return None
        try:
            state = IncidentState(found["state"])
        except ValueError:
            raise ReleaseError(f"incident #{issue.number} has an unknown state {found['state']!r}") from None
        to = None if found["to"] == "-" else Version.of_tag(found["to"])
        return cls(issue, found["env"], Version.of_tag(found["tag"]), to, state)

    @property
    def number(self) -> int:
        return self.issue.number

    def moved(self, state: IncidentState, to: Version | None) -> str:
        """The issue's body with its hidden line in the new state"""
        line = incident_line(self.env, self.tag, to, state)
        return _INCIDENT.sub(lambda _: line, self.issue.body, count=1)


def incidents(policy: Policy, github: GitHub) -> list[Incident]:
    """The incidents shipmill opened, open and closed, newest first"""
    found = (Incident.read(i) for i in github.labelled_issues(policy.operate.incident_label))
    return [i for i in found if i is not None]


def incident_title(env: str, tag: Version) -> str:
    return f"Incident: {env} fails its health checks on {tag.tag}"


def _last_good(github: GitHub, seen: Observed, tags: Sequence[Tag]) -> Version | None:
    """The tag of the newest deployment before the current one that reached success there,
    on another tag, whose last health check (if shipmill wrote one) didn't fail"""
    assert seen.current is not None and seen.tag is not None  # only a checked deployment rolls back
    for deployment in seen.deployments:
        tag = tag_of(deployment, tags)
        if deployment.id >= seen.current.id or tag is None or tag == seen.tag:
            continue
        statuses = github.deployment_statuses(deployment.id)
        ours = [s for s in statuses if _ours(s)]
        if not any(s.state is DeploymentState.SUCCESS for s in statuses):
            continue
        if ours and ours[-1].state is DeploymentState.FAILURE:
            continue
        return tag
    return None


def _rolled_back(github: GitHub, seen: Observed, to: Version) -> str | None:
    """How the rollback to the tag shows it was started: a deployment of it after the bad
    one, or a run of the workflow on it since the checks began failing (still queued, say)"""
    assert seen.current is not None
    newer = [d for d in seen.deployments if d.id > seen.current.id and d.ref.removeprefix("refs/tags/") == to.tag]
    if newer:
        return f"deployment {newer[0].id}"
    started = _started(github, seen.env, to, seen.failing_since or seen.current.created_at)
    return f"run {started.id}, {started.status}" if started is not None else None


def _approve_rollback_how(env: str, cause: str) -> tuple[str, ...]:
    command = f"gh workflow run {OPERATE_CALLER.name} -f approve-rollback={env} -f dry-run=false"
    if cause.startswith(f"held by {HOLD_LABEL}"):
        how = f"Close the open `{HOLD_LABEL}` issues first; then approve the rollback:"
    else:
        how = "To roll back, approve it:"
    return (how, "", "```", command, "```")


@dataclass(frozen=True, slots=True)
class Rollback:
    """What this run did about rolling an environment back"""

    to: Version | None
    state: IncidentState
    text: str  # a sentence for the incident and the summary
    how: tuple[str, ...] = ()  # the approve lines, for a proposed rollback


def _rollback(
    policy: Policy, github: GitHub, seen: Observed, tags: Sequence[Tag], hold: Hold, dry_run: bool
) -> Rollback:
    to = _last_good(github, seen, tags)
    if to is None:
        return Rollback(None, IncidentState.FAILING, "no rollback: no earlier tag reached success here")
    stage = Stage.rollback()
    level = policy.autonomy.effective(stage, hold)
    env = seen.env
    if level is Autonomy.ACT:
        # a rollback deploys a tag that ran here before, so the at-most-once rule for deploys
        # doesn't apply to it; started once is still the rule
        if (started := _rolled_back(github, seen, to)) is not None:
            return Rollback(to, IncidentState.ROLLED_BACK, f"rollback to {to.tag} started already ({started})")
        if dry_run:
            return Rollback(to, IncidentState.ROLLED_BACK, f"would roll back: start {env.workflow} with {to.tag}")
        url = github.dispatch(env.workflow, to.tag, to.tag, env.inputs)
        run = f": {url}" if url else ""
        return Rollback(to, IncidentState.ROLLED_BACK, f"rolled back: started {env.workflow} with {to.tag}{run}")
    cause = policy.autonomy.cause(stage, hold)
    if level is Autonomy.PROPOSE:
        how = _approve_rollback_how(env.name, cause)
        return Rollback(to, IncidentState.PROPOSED, f"would roll back to {to.tag}, but {cause}", how)
    return Rollback(to, IncidentState.FAILING, f"would roll back to {to.tag}, but {cause}")


def incident_body(policy: Policy, seen: Observed, rollback: Rollback, now: dt.datetime) -> str:
    assert seen.tag is not None and seen.probe is not None and seen.env.health is not None
    env = seen.env.name
    since = seen.failing_since or now
    lanes = ", ".join(str(lane) for lane in Lane if lane in policy.blocker_lanes)
    lines = [
        incident_line(env, seen.tag, rollback.to, rollback.state),
        f"**{env}** failed {seen.failed_checks} health checks in a row on **{seen.tag.tag}**, since"
        f" {since:%Y-%m-%d %H:%M} UTC.",
        "",
        f"- Health check: `{shown_url(seen.env.health)}`",
        f"- Last check: {seen.probe.detail}",
    ]
    if seen.probe.excerpt:
        lines.append(f"- Body: `{seen.probe.excerpt}`")
    lines += ["", f"**Rollback**: {rollback.text}."]
    if rollback.how:
        lines += ["", *rollback.how]
    lines += [
        "",
        f"While this issue is open, the {lanes} lanes don't release: the `{policy.operate.incident_label}`"
        " label holds them like the blocker label. Close it once resolved, by hand or with a hotfix pull"
        f" request's \"Fixes #N\". shipmill comments here when {env} is healthy again, and doesn't roll back"
        " a second time if the rollback fails its checks too.",
    ]
    return "\n".join(lines) + "\n"


def _incident(
    policy: Policy,
    github: GitHub,
    seen: Observed,
    tags: Sequence[Tag],
    known: Sequence[Incident],
    hold: Hold,
    now: dt.datetime,
    dry_run: bool,
) -> str | None:
    """Open, comment on, or roll back for this environment's incident; what it did"""
    if seen.current is None or seen.probe is None or seen.tag is None:
        return None
    env = seen.env.name
    # an incident closed before the current stretch of failed checks began is history: the
    # person closed that failure, not this one, which opens another
    since = seen.failing_since
    mine = [
        i
        for i in known
        if i.env == env
        and seen.tag in (i.tag, i.to)
        and not (since is not None and i.issue.closed_at is not None and i.issue.closed_at < since)
    ]
    incident = mine[0] if mine else None
    would = "would " if dry_run else ""
    if seen.probe.healthy:
        if incident is None or incident.issue.closed or incident.state is IncidentState.HEALTHY:
            return None
        if not dry_run:
            github.comment_issue(incident.number, f"{env} is healthy again on {seen.tag.tag} ({seen.probe.detail}).")
            healthy = incident.moved(IncidentState.HEALTHY, incident.to)
            github.update_issue(incident.number, incident.issue.title, healthy)
        return f"{would}comment on incident #{incident.number}: healthy again"
    if seen.failed_checks < policy.operate.rollback_after:
        return None
    if incident is not None and incident.issue.closed:
        return (
            f"incident #{incident.number} for {incident.tag.tag} was closed while it failed; shipmill opens"
            " another only if it fails again after recovering"
        )
    if incident is None:
        rollback = _rollback(policy, github, seen, tags, hold, dry_run)
        if dry_run:
            return f"would open an incident; {rollback.text}"
        fix = f"grant `issues: write` to the job that calls operate.yml in {OPERATE_CALLER}"
        try:
            number = github.create_issue(
                incident_title(env, seen.tag),
                incident_body(policy, seen, rollback, now),
                (policy.operate.incident_label,),
            )
        except ReleaseError as exc:
            raise ReleaseError(f"{exc}; if GitHub refused it (403): {fix}") from exc
        return f"opened incident #{number}; {rollback.text}"
    if incident.tag == seen.tag:
        if incident.state is not IncidentState.HEALTHY:
            return f"incident #{incident.number} open ({incident.state})"
        rollback = _rollback(policy, github, seen, tags, hold, dry_run)
        if not dry_run:
            text = [f"{env} fails its health checks again on {seen.tag.tag} ({seen.probe.detail}).", ""]
            text += [f"**Rollback**: {rollback.text}.", *(("", *rollback.how) if rollback.how else ())]
            github.comment_issue(incident.number, "\n".join(text))
            moved = incident.moved(rollback.state, rollback.to)
            github.update_issue(incident.number, incident.issue.title, moved)
        return f"{would}comment on incident #{incident.number}: failing again; {rollback.text}"
    # the environment runs the rollback's tag, and that fails its checks too
    if incident.state is IncidentState.STOPPED:
        return f"incident #{incident.number}: the rollback to {seen.tag.tag} fails too; waiting for a person"
    if not dry_run:
        stop = (
            f"{seen.tag.tag}, the rollback, fails its health checks too ({seen.probe.detail}). shipmill doesn't"
            f" roll {env} back a second time: a second automatic guess is worse than waiting for a person."
        )
        github.comment_issue(incident.number, stop)
        stopped = incident.moved(IncidentState.STOPPED, incident.to)
        github.update_issue(incident.number, incident.issue.title, stopped)
    return (
        f"{would}comment on incident #{incident.number}: the rollback to {seen.tag.tag} fails too; no second rollback"
    )


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
    since: dt.datetime  # a run of the environment's workflow on the tag since then deployed it already


def _minutes(span: dt.timedelta) -> int:
    return int(span.total_seconds() // 60)


def _bake_target(env: Environment, environments: Mapping[str, Environment]) -> int:
    """The longest bake any environment promoted from this one waits for; 0 when none is"""
    return max((e.bake_minutes for e in environments.values() if e.source == env.name), default=0)


def _health_text(seen: Observed, target: int, rollback_after: int, now: dt.datetime) -> str:
    if seen.current is None:
        return "no deployment"
    if not seen.env.health:
        return "no health URL"
    if seen.tag is None or seen.probe is None:
        return "not checked"
    if not seen.probe.healthy:
        since = f", failing for {_minutes(now - seen.failing_since)} min" if seen.failing_since else ""
        count = f", {min(seen.failed_checks, rollback_after)} of {rollback_after} failed checks to roll back"
        return f"unhealthy: {seen.probe.detail}{since}{count}"
    healthy = seen.healthy_for(now) or dt.timedelta()
    if target and _minutes(healthy) < target:
        return f"healthy ({seen.probe.detail}), baking {_minutes(healthy)} of {target} min"
    return f"healthy ({seen.probe.detail})"


def _status(seen: Observed, target: int, rollback_after: int, now: dt.datetime) -> tuple[DeploymentState, str] | None:
    """The status the current deployment should carry, when it differs from its latest. A
    failed check is written each time until rollback_after stand in a row: they count them"""
    if seen.current is None or seen.probe is None:
        return None
    if not seen.probe.healthy:
        if seen.failed_checks > rollback_after:
            return None
        return DeploymentState.FAILURE, f"{STATUS_PREFIX}: {seen.probe.detail}"[:_DESCRIPTION_LIMIT]
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
        why = f"healthy on {env.source} for the {env.bake_minutes} min bake"
        # the source's own run of a shared workflow made its deployment, so it started before it
        return Wanted(source.tag, why, promote=True, since=source.current.created_at)
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
    why = f"the newest {env.lane} release, which its deploy missed"
    return Wanted(newest.version, why, promote=False, since=newest.date)


def _started(github: GitHub, env: Environment, tag: Version, since: dt.datetime) -> WorkflowRun | None:
    """The newest run of the environment's workflow on the tag started since then"""
    runs = [r for r in github.workflow_runs(env.workflow, tag.tag) if r.created_at >= since]
    return runs[0] if runs else None


def deploy_marker(env: str) -> str:
    return f"<!-- shipmill:propose deploy={env} -->"


def _tag_line(tag: Version) -> str:
    return f"<!-- shipmill:tag={tag.tag} -->"


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
            f"shipmill would deploy **{wanted.tag.tag}** to {env.name} now ({wanted.why}), but {cause}.",
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


_TAG_LINE = re.compile(r"<!-- shipmill:tag=(?P<tag>\S+) -->")


def _close_deployed(github: GitHub, seen: Observed, dry_run: bool) -> str | None:
    """Close the environment's open deploy proposal once the environment runs its tag or a
    later one, whoever deployed it; what it did"""
    if seen.current is None or seen.tag is None:
        return None
    issue = find_proposal(github, deploy_marker(seen.env.name))
    if issue is None:
        return None
    found = _TAG_LINE.search(issue.body)
    proposed = Version.of_tag(found["tag"]) if found else None
    if proposed is not None and proposed > seen.tag:
        return None
    if dry_run:
        return f"would close proposal #{issue.number}: {seen.tag.tag} deployed"
    text = f"{seen.env.name} runs {seen.tag.tag} now (deployment {seen.current.id})."
    if proposed != seen.tag:
        text += f" This issue proposed {proposed.tag if proposed else 'another tag'}, so it is closed too."
    github.close_issue(issue.number, text)
    return f"closed proposal #{issue.number}: {seen.tag.tag} deployed"


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
    rollback_after = policy.operate.rollback_after
    checked = any(seen.probe is not None for seen in observed.values())
    known = incidents(policy, github) if checked else []
    reports = []
    for name, seen in observed.items():
        target = _bake_target(seen.env, environments)
        actions = []
        status = _status(seen, target, rollback_after, now)
        # the incident before the status: if opening it fails, the next run still counts
        # this check, and tries again
        incident = _incident(policy, github, seen, tags, known, hold, now, dry_run)
        if status is not None:
            assert seen.current is not None  # a status is only wanted for a current deployment
            if not dry_run:
                github.create_deployment_status(seen.current.id, *status)
            actions.append(f"{'would mark' if dry_run else 'marked'} {status[0]}")
        if incident is not None:
            actions.append(incident)
        wanted = _wanted(seen, observed, tags, now)
        waits = False  # a deploy waits: its proposal is updated, not closed
        if isinstance(wanted, str):
            actions.append(wanted)
        elif (tried := seen.tried(wanted.tag)) is not None:
            states = github.deployment_statuses(tried.id)
            state = states[-1].state if states else DeploymentState.PENDING
            actions.append(f"{wanted.tag.tag} was deployed here already ({state}); shipmill doesn't deploy it again")
        elif (started := _started(github, seen.env, wanted.tag, wanted.since)) is not None:
            # a deploy job makes its deployment only when it starts, after the jobs it needs and
            # a wait for a runner: until then the run is the only sign the tag was dispatched
            actions.append(
                f"{seen.env.workflow} already ran with {wanted.tag.tag} ({started.status}, run {started.id});"
                " shipmill doesn't deploy it again"
            )
        else:
            waits = True
            actions.append(_act(policy, github, seen.env, wanted, hold, dry_run))
        if not waits and (closed := _close_deployed(github, seen, dry_run)) is not None:
            actions.append(closed)
        tag = seen.tag.tag if seen.tag else (seen.current.ref if seen.current else "-")
        health = _health_text(seen, target, rollback_after, now)
        reports.append(Report(name, tag, health, "; ".join(actions)))
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
    issue = find_proposal(github, deploy_marker(name))
    if issue is None:
        raise ReleaseError(f"no open proposal to deploy to {name}; operate opens one when a deploy waits on approval")
    found = _TAG_LINE.search(issue.body)
    if found is None:
        raise ReleaseError(f"proposal #{issue.number} names no tag; the next operate run rewrites it")
    tag = Version.of_tag(found["tag"])
    tried = [d for d in github.deployments(name) if d.ref.removeprefix("refs/tags/") == tag.tag]
    if tried:
        raise ReleaseError(
            f"{tag.tag} was deployed to {name} already (deployment {tried[0].id}); close #{issue.number}"
        )
    running = [r for r in github.workflow_runs(env.workflow, tag.tag) if r.status != "completed"]
    if running:
        raise ReleaseError(
            f"{env.workflow} is running with {tag.tag} already (run {running[0].id}, {running[0].status});"
            f" close #{issue.number} once its deployment shows"
        )
    if dry_run:
        return f"would dispatch {env.workflow} with {tag.tag} to {name} and close #{issue.number}"
    github.dispatch(env.workflow, tag.tag, tag.tag, env.inputs)
    github.close_issue(issue.number, f"Approved: started `{env.workflow}` with {tag.tag} for {name}.")
    return f"dispatched {env.workflow} with {tag.tag} to {name}; closed #{issue.number}"


def approve_rollback(policy: Policy, github: GitHub, name: str, dry_run: bool) -> str:
    """Start the rollback an incident proposes, once; a person acting, so it goes under
    propose and observe, but never under the hold (D-8)"""
    env = policy.environments.get(name)
    if env is None:
        known = ", ".join(policy.environments) or "none"
        raise ReleaseError(f"no environment {name!r} in [environments]; known: {known}")
    hold = Hold.read(github)
    if hold.on:
        raise ReleaseError(f"{hold.reason}; close it to approve a rollback")
    proposed = [
        i
        for i in incidents(policy, github)
        if i.env == name and not i.issue.closed and i.state is IncidentState.PROPOSED
    ]
    if not proposed:
        raise ReleaseError(
            f"no open incident proposes a rollback of {name}; operate proposes one under propose or a hold"
        )
    incident = proposed[0]
    assert incident.to is not None  # a proposed rollback names its tag
    to = incident.to
    deployments = github.deployments(name)
    bad = [d for d in deployments if d.ref.removeprefix("refs/tags/") == incident.tag.tag]
    after = [d for d in deployments if d.ref.removeprefix("refs/tags/") == to.tag and bad and d.id > bad[0].id]
    if after:
        raise ReleaseError(
            f"{to.tag} was deployed to {name} after {incident.tag.tag} already (deployment {after[0].id})"
        )
    running = [r for r in github.workflow_runs(env.workflow, to.tag) if r.status != "completed"]
    if running:
        raise ReleaseError(
            f"{env.workflow} is running with {to.tag} already (run {running[0].id}, {running[0].status})"
        )
    if dry_run:
        return f"would dispatch {env.workflow} with {to.tag} to {name}, rolling back for incident #{incident.number}"
    url = github.dispatch(env.workflow, to.tag, to.tag, env.inputs)
    run = f": {url}" if url else ""
    github.update_issue(incident.number, incident.issue.title, incident.moved(IncidentState.ROLLED_BACK, to))
    github.comment_issue(incident.number, f"Approved: rolled back, started `{env.workflow}` with {to.tag}{run}.")
    return f"dispatched {env.workflow} with {to.tag} to {name}; rolled back for incident #{incident.number}"


def summary(reports: Sequence[Report], dry_run: bool) -> str:
    if not reports:
        return "No [environments] in the config: nothing to operate.\n"
    lines = [
        f"### shipmill operate{' (dry run)' if dry_run else ''}",
        "",
        "| Environment | Tag | Health | Action |",
        "|---|---|---|---|",
    ]
    lines += [
        f"| {r.environment} | {r.tag} | {r.health.replace('|', '/')} | {r.action.replace('|', '/')} |" for r in reports
    ]
    return "\n".join(lines) + "\n"
