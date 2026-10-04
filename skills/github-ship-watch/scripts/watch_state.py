#!/usr/bin/env python3
"""Report what a repo's issue-to-release pipeline still owes: the release bot, the
recent releases, the environments shipyard operate runs, and the issue intake.

Usage: watch_state.py <owner/repo> [--repo-dir PATH] [--releases N] [--grace MIN]
                      [--tool SPEC] [--json]

Release bot (a repo with .github/shipyard.toml, or its alias .github/release-policy.toml):
  BOT_FAILED      the bot's latest finished run failed (cancelled runs are ignored:
                  a newer push cancels the settle wait on purpose)
  BOT_STALLED     a shipyard bot in release mode has a release due now, no run of
                  its workflow is queued or running, and none started in --grace minutes
  BOT_OK          neither; BOT_NONE when the repo has no bot (it releases by "tag X")

Each of the --releases newest version tags (default 3):
  NOT_PUBLISHED   the package is on PyPI but this version isn't, --grace minutes after
                  the tag
  PUBLISHING      the same, inside --grace
  PUBLISHED       PyPI lists it; NO_REGISTRY when the package isn't on PyPI at all
  UNANNOUNCED     closed issues it fixed that have no "Released in <tag>" comment
                  (github-pr-triage's shipped.py, in plan mode)

Hold (a repo with a shipyard config):
  HOLD            an open shipyard-hold issue, with who opened it and when (reported, never
                  an action by itself: a person stopped the factory on purpose)

Operations (only when the config has [environments.<name>] tables; read from the
deployments and issues shipyard operate writes):
  OPERATE_FAILED  the latest finished run of the workflow that calls shipyard's operate.yml
                  failed (cancelled runs are ignored)
  UNHEALTHY       an environment's newest "shipyard health" deployment status is a failure
  PROMOTION_DUE   an open "Ready to promote" proposal issue, with its approve command; or a
                  `from` environment whose source has run a release for its bake_minutes
                  while nothing promotes it, as no operate workflow runs on a schedule (no
                  caller, no schedule in it, or no run in the last hour)
  INCIDENT_OPEN   an open issue labelled [operate] incident_label (default "incident"),
                  with its age and the pull requests linked to close it

Intake (github-issue-triage's triage_state.py):
  ISSUES          issues needing triage action, counted by state
  PRS_OPEN        open non-draft pull requests (reported, never an action by itself)

Holds and incidents lead the report. Exit 0 when nothing needs action, 1 when any
BOT_FAILED, BOT_STALLED, NOT_PUBLISHED, UNANNOUNCED, ISSUES, OPERATE_FAILED, UNHEALTHY,
PROMOTION_DUE, or INCIDENT_OPEN row is present, 2 on bad input or a git, gh, or uvx failure.
Needs git, an authenticated gh, and uvx (for a shipyard bot's plan). Python 3.10+,
standard library only.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

SKILLS = Path(__file__).resolve().parents[2]
SHIPPED = SKILLS / "github-pr-triage" / "scripts" / "shipped.py"
TRIAGE_STATE = SKILLS / "github-issue-triage" / "scripts" / "triage_state.py"
POLICIES = (Path(".github/shipyard.toml"), Path(".github/release-policy.toml"))  # the config, then its alias
VERSION_TAG = re.compile(r"^v\d+\.\d+")  # skips moving major tags such as v0
ACTION = {
    "BOT_FAILED",
    "BOT_STALLED",
    "NOT_PUBLISHED",
    "UNANNOUNCED",
    "ISSUES",
    "OPERATE_FAILED",
    "UNHEALTHY",
    "PROMOTION_DUE",
    "INCIDENT_OPEN",
}
LEAD = ("INCIDENT_OPEN", "HOLD")  # the report starts with these, in this order
ACTIVE = {"queued", "in_progress", "waiting", "pending", "requested"}
HOLD_LABEL = "shipyard-hold"  # shipyard's autonomy.HOLD_LABEL
INCIDENT_LABEL = "incident"  # the default of [operate] incident_label
HEALTH_PREFIX = "shipyard health"  # starts the description of every status shipyard operate writes
OPERATE_SILENT = dt.timedelta(hours=1)  # a scheduled operate runs every 10 minutes
ISSUE_LIMIT = 1000
PROPOSAL = re.compile(r"<!-- shipyard:propose deploy=(?P<env>\S+) -->")  # shipyard's operate.deploy_marker
PROPOSED_TAG = re.compile(r"<!-- shipyard:tag=(?P<tag>\S+) -->")
OPERATE_USES = re.compile(
    r"^\s*(?:-\s*)?uses:\s*[\"']?(?:[\w.-]+/[\w.-]+/\.github/workflows/operate\.ya?ml@|\./\.github/workflows/operate\.ya?ml)",
    re.MULTILINE,
)


@dataclass(frozen=True)
class Row:
    state: str
    subject: str
    detail: str

    def text(self) -> str:
        return f"{self.state:<14} {self.subject:<16} {self.detail}"


@dataclass(frozen=True)
class Run:
    status: str
    conclusion: str
    created: dt.datetime
    url: str


@dataclass(frozen=True)
class Tag:
    name: str
    created: dt.datetime


def run(cmd: list[str], cwd: Path | None = None, env: dict[str, str] | None = None) -> str:
    proc = subprocess.run(cmd, cwd=cwd, env=env, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        sys.stderr.write(f"error: {' '.join(cmd[:3])}...: {proc.stderr.strip()}\n")
        raise SystemExit(2)
    return proc.stdout


def parse_time(text: str) -> dt.datetime:
    return dt.datetime.fromisoformat(text.replace("Z", "+00:00"))


# -- release bot ---------------------------------------------------------------------------


def policy_file(repo_dir: Path) -> Path | None:
    """The release policy shipyard reads, or None without one; both names is an error, as in shipyard"""
    found = [p for p in POLICIES if (repo_dir / p).is_file()]
    if len(found) > 1:
        raise SystemExit(f"error: both {found[0]} and {found[1]} exist; shipyard refuses a repo with both")
    return found[0] if found else None


def bot_workflow(repo_dir: Path) -> tuple[str, bool] | None:
    """The bot's workflow file and whether it is a shipyard bot, or None without a bot"""
    policy = policy_file(repo_dir)
    if policy is None:
        return None
    workflows = repo_dir / ".github" / "workflows"
    caller = workflows / "release.yml"
    if caller.is_file() and re.search(r"shipyard|release-lanes|\./\.github/workflows/prepare\.yml", caller.read_text()):
        return "release.yml", True
    if (workflows / "release-bot.yml").is_file():
        return "release-bot.yml", False
    raise SystemExit(f"error: {policy} exists but neither release.yml nor release-bot.yml calls a bot")


def bot_rows(runs: list[Run], due: str | None, now: dt.datetime, grace: dt.timedelta, name: str) -> list[Row]:
    """Classify the bot from its recent runs, newest first, and a planned due release"""
    rows = []
    finished = [r for r in runs if r.status == "completed" and r.conclusion != "cancelled"]
    if finished and finished[0].conclusion not in {"success", "skipped"}:
        rows.append(Row("BOT_FAILED", name, f"{finished[0].conclusion}: {finished[0].url}"))
    if due is not None:
        active = any(r.status in ACTIVE for r in runs)
        recent = bool(runs) and now - runs[0].created < grace
        if not active and not recent:
            rows.append(Row("BOT_STALLED", name, f"due and not running: {due}"))
    return rows or [Row("BOT_OK", name, "")]


def fetch_runs(repo: str, workflow: str) -> list[Run]:
    out = run(
        ["gh", "run", "list", "-R", repo, "-w", workflow, "-L", "10", "--json", "status,conclusion,createdAt,url"]
    )
    return [Run(r["status"], r["conclusion"] or "", parse_time(r["createdAt"]), r["url"]) for r in json.loads(out)]


def planned_release(repo: str, repo_dir: Path, policy: Path, branch: str, tool: str) -> str | None:
    """What the shipyard planner would release now on the default branch, or None"""
    work = repo_dir / "tmp" / f"ship-watch-{os.getpid()}"
    run(["git", "worktree", "add", "-q", "--detach", str(work), f"origin/{branch}"], cwd=repo_dir)
    try:
        env = {**os.environ, "GITHUB_REPOSITORY": repo}
        out = run(["uvx", "--from", tool, "shipyard", "plan", "--event", "schedule", "--dry-run"], cwd=work, env=env)
    finally:
        run(["git", "worktree", "remove", "--force", str(work)], cwd=repo_dir)
    decision = json.loads(out)
    policy_mode = re.search(r'^mode\s*=\s*"(\w[\w-]*)"', (repo_dir / policy).read_text(), re.MULTILINE)
    if decision["action"] != "release" or policy_mode is None or policy_mode.group(1) != "release":
        return None
    return str(decision["reason"])


# -- releases ------------------------------------------------------------------------------


def version_tags(repo_dir: Path) -> list[Tag]:
    out = run(
        [
            "git",
            "for-each-ref",
            "--sort=-creatordate",
            "--format=%(refname:short)%00%(creatordate:iso-strict)",
            "refs/tags",
        ],
        cwd=repo_dir,
    )
    tags = []
    for line in out.splitlines():
        name, _, created = line.partition("\x00")
        if VERSION_TAG.match(name):
            tags.append(Tag(name, parse_time(created)))
    return tags


def package_name(repo_dir: Path) -> str | None:
    pyproject = repo_dir / "pyproject.toml"
    if not pyproject.is_file():
        return None
    project = re.search(r"^\[project\]\s*$(.*?)(?=^\[|\Z)", pyproject.read_text(), re.MULTILINE | re.DOTALL)
    name = re.search(r'^name\s*=\s*"([^"]+)"', project.group(1), re.MULTILINE) if project else None
    return name.group(1) if name else None


def on_pypi(path: str) -> bool:
    try:
        with urllib.request.urlopen(f"https://pypi.org/pypi/{path}/json", timeout=20) as response:
            return bool(response.status == 200)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return False
        sys.stderr.write(f"error: PyPI {path}: HTTP {exc.code}\n")
        raise SystemExit(2) from exc
    except urllib.error.URLError as exc:
        sys.stderr.write(f"error: PyPI {path}: {exc.reason}\n")
        raise SystemExit(2) from exc


def publish_row(tag: Tag, package: str | None, listed: bool | None, now: dt.datetime, grace: dt.timedelta) -> Row:
    if package is None or listed is None:
        return Row("NO_REGISTRY", tag.name, "")
    if listed:
        return Row("PUBLISHED", tag.name, f"{package} {tag.name[1:]} on PyPI")
    if now - tag.created < grace:
        return Row("PUBLISHING", tag.name, f"tagged {int((now - tag.created).total_seconds() // 60)} min ago")
    return Row("NOT_PUBLISHED", tag.name, f"{package} {tag.name[1:]} missing from PyPI")


def unannounced(repo: str, repo_dir: Path, prev: str, tag: str) -> list[str]:
    out = run([sys.executable, str(SHIPPED), repo, prev, tag, "--repo-dir", str(repo_dir)])
    return [line.split()[0] for line in out.splitlines() if "WOULD POST" in line]


# -- holds and operations ------------------------------------------------------------------


@dataclass(frozen=True)
class Environment:
    name: str
    source: str | None  # `from`: promoted from this environment
    bake_minutes: int


@dataclass(frozen=True)
class Deployment:
    id: int
    ref: str  # a release tag, as shipyard dispatches deploys on the tag
    sha: str
    created: dt.datetime


@dataclass(frozen=True)
class Status:
    id: int
    state: str
    description: str
    created: dt.datetime


@dataclass(frozen=True)
class Current:
    """An environment's newest deployment that reached success, with its statuses oldest first"""

    deployment: Deployment
    statuses: tuple[Status, ...]

    @property
    def tag(self) -> str:
        return self.deployment.ref.removeprefix("refs/tags/")


@dataclass(frozen=True)
class Issue:
    number: int
    title: str
    body: str
    created: dt.datetime
    author: str
    labels: tuple[str, ...]
    closing_prs: tuple[int, ...]  # pull requests linked to close it


def ago(span: dt.timedelta) -> str:
    minutes = int(span.total_seconds() // 60)
    if minutes < 120:
        return f"{minutes} min"
    if minutes < 48 * 60:
        return f"{minutes // 60} h"
    return f"{minutes // (24 * 60)} d"


def toml_tables(text: str) -> dict[str, str]:
    """Each [table] header of a TOML file, its dotted parts unquoted, mapped to its body; the
    keys before the first header are under "". Arrays of tables ([[x]]) are skipped"""
    tables: dict[str, str] = {}
    name: str | None = ""
    body: list[str] = []
    for line in [*text.splitlines(), "[[end]]"]:
        header = re.match(r"^\s*(\[\[?)\s*([^\[\]]+?)\s*\]\]?\s*(?:#.*)?$", line)
        if header is None:
            body.append(line)
            continue
        if name is not None:
            tables[name] = "\n".join(body)
        body = []
        parts = [p.strip().strip("\"'") for p in header.group(2).split(".")]
        name = None if header.group(1) == "[[" else ".".join(parts)
    return tables


def toml_string(body: str, key: str) -> str | None:
    found = re.search(rf"^\s*{re.escape(key)}\s*=\s*(?:\"([^\"]*)\"|'([^']*)')", body, re.MULTILINE)
    return None if found is None else found.group(1) if found.group(1) is not None else found.group(2)


def environments(text: str, policy: Path) -> list[Environment]:
    """The [environments.<name>] tables of the config, in order; an empty list without any"""
    tables = toml_tables(text)
    if "environments" in tables or re.search(r"^\s*environments\s*[.=]", tables.get("", ""), re.MULTILINE):
        raise SystemExit(f"error: {policy}: write each environment as an [environments.<name>] table to watch it")
    found = []
    for name, body in tables.items():
        parts = name.split(".")
        if parts[0] != "environments":
            continue
        if len(parts) != 2:
            raise SystemExit(f"error: {policy}: [{name}] is not an [environments.<name>] table")
        bake = re.search(r"^\s*bake_minutes\s*=\s*(\d+)\s*(?:#.*)?$", body, re.MULTILINE)
        found.append(Environment(parts[1], toml_string(body, "from"), int(bake.group(1)) if bake else 0))
    return found


def incident_label(text: str) -> str:
    """[operate] incident_label, or the default"""
    label = toml_string(toml_tables(text).get("operate", ""), "incident_label")
    if label == "":
        raise SystemExit("error: [operate] incident_label is empty")
    return label or INCIDENT_LABEL


def operate_caller(repo_dir: Path) -> tuple[str, bool] | None:
    """The workflow that calls shipyard's operate.yml and whether it runs on a schedule, or None"""
    workflows = repo_dir / ".github" / "workflows"
    if not workflows.is_dir():
        return None
    for path in sorted([*workflows.glob("*.yml"), *workflows.glob("*.yaml")]):
        text = path.read_text()
        if OPERATE_USES.search(text):
            return path.name, re.search(r"^\s*schedule:", text, re.MULTILINE) is not None
    return None


def current_deployment(deployments: list[Deployment], statuses_of: Callable[[int], list[Status]]) -> Current | None:
    """The newest of the deployments, newest first, that reached success, as shipyard operate reads it"""
    for deployment in deployments:
        statuses = tuple(statuses_of(deployment.id))
        if any(s.state == "success" for s in statuses):
            return Current(deployment, statuses)
    return None


def operate_rows(runs: list[Run], caller: str) -> list[Row]:
    finished = [r for r in runs if r.status == "completed" and r.conclusion != "cancelled"]
    if finished and finished[0].conclusion not in {"success", "skipped"}:
        return [Row("OPERATE_FAILED", caller, f"{finished[0].conclusion}: {finished[0].url}")]
    return []


def operate_idle(caller: tuple[str, bool] | None, runs: list[Run], now: dt.datetime) -> str | None:
    """Why no shipyard operate runs on a schedule, or None while one does"""
    if caller is None:
        return "no workflow calls shipyard's operate.yml"
    name, scheduled = caller
    if not scheduled:
        return f"{name} has no schedule"
    if not runs:
        return f"{name} has never run"
    if now - runs[0].created >= OPERATE_SILENT:
        return f"{name} last ran {ago(now - runs[0].created)} ago"
    return None


def unhealthy_row(env: str, current: Current | None, now: dt.datetime) -> Row | None:
    ours = [s for s in current.statuses if s.description.startswith(HEALTH_PREFIX)] if current else []
    if current is None or not ours or ours[-1].state != "failure":
        return None
    why = ours[-1].description[len(HEALTH_PREFIX) :].lstrip(": ")
    return Row("UNHEALTHY", env, f"{current.tag}: {why}, since {ago(now - ours[-1].created)} ago")


def proposal_rows(issues: list[Issue], caller: str, held: bool) -> list[Row]:
    """The open proposal issues shipyard operate opens for a deploy that waits on approval"""
    rows = []
    for issue in issues:
        found = PROPOSAL.search(issue.body)
        if found is None:
            continue
        env = found.group("env")
        tag = PROPOSED_TAG.search(issue.body)
        approve = f"gh workflow run {caller} -f approve={env} -f dry-run=false"
        first = f"close the {HOLD_LABEL} issues, then " if held else ""
        detail = f"#{issue.number} {tag.group('tag') if tag else issue.title}: {first}{approve}"
        rows.append(Row("PROMOTION_DUE", env, detail))
    return rows


def unpromoted_row(
    env: Environment,
    source: Current | None,
    deployments: list[Deployment],
    idle: str | None,
    command: str,
    now: dt.datetime,
) -> Row | None:
    """A `from` environment whose source baked a release that nothing promotes, because no
    operate runs; operate itself counts the bake from health checks, so with it running the
    proposal issue or the deploy is the signal"""
    if env.source is None or idle is None or source is None:
        return None
    ours = [s for s in source.statuses if s.description.startswith(HEALTH_PREFIX)]
    if ours and ours[-1].state == "failure":
        return None
    succeeded = min(s.created for s in source.statuses if s.state == "success")
    if now - succeeded < dt.timedelta(minutes=env.bake_minutes):
        return None
    if any(d.sha == source.deployment.sha for d in deployments):
        return None  # tried there already, in any state: operate deploys a release once
    detail = f"{source.tag} on {env.source} for {ago(now - succeeded)}, unpromoted: {idle}; {command}"
    return Row("PROMOTION_DUE", env.name, detail)


def incident_rows(issues: list[Issue], label: str, now: dt.datetime) -> list[Row]:
    rows = []
    for issue in issues:
        if label not in issue.labels:
            continue
        prs = " ".join(f"#{n}" for n in issue.closing_prs)
        linked = f"PR {prs} links it" if prs else "no PR links it"
        detail = f"{issue.title}; open {ago(now - issue.created)}, {linked}"
        rows.append(Row("INCIDENT_OPEN", f"#{issue.number}", detail))
    return rows


def hold_rows(issues: list[Issue], now: dt.datetime) -> list[Row]:
    return [
        Row(
            "HOLD",
            f"#{i.number}",
            f"{i.title}; opened by @{i.author} {ago(now - i.created)} ago ({i.created:%Y-%m-%d %H:%M} UTC)",
        )
        for i in issues
        if HOLD_LABEL in i.labels
    ]


def ordered(rows: list[Row]) -> list[Row]:
    """Incidents and holds first, the rest in the order found"""
    return sorted(rows, key=lambda r: LEAD.index(r.state) if r.state in LEAD else len(LEAD))


def fetch_issues(repo: str) -> list[Issue]:
    fields = "number,title,body,createdAt,author,labels,closedByPullRequestsReferences"
    out = run(["gh", "issue", "list", "-R", repo, "--state", "open", "-L", str(ISSUE_LIMIT), "--json", fields])
    found = json.loads(out)
    if len(found) >= ISSUE_LIMIT:
        raise SystemExit(f"error: {repo} has {ISSUE_LIMIT}+ open issues; the watch reads at most {ISSUE_LIMIT - 1}")
    return [
        Issue(
            int(i["number"]),
            i["title"],
            i["body"] or "",
            parse_time(i["createdAt"]),
            (i["author"] or {}).get("login", "ghost"),
            tuple(label["name"] for label in i["labels"]),
            tuple(int(p["number"]) for p in i["closedByPullRequestsReferences"]),
        )
        for i in found
    ]


def fetch_deployments(repo: str, environment: str) -> list[Deployment]:
    out = run(
        ["gh", "api", "-X", "GET", f"repos/{repo}/deployments", "-f", f"environment={environment}", "-f", "per_page=30"]
    )
    found = [Deployment(int(d["id"]), d["ref"], d["sha"], parse_time(d["created_at"])) for d in json.loads(out)]
    return sorted(found, key=lambda d: d.id, reverse=True)


def fetch_statuses(repo: str, deployment: int) -> list[Status]:
    out = run(["gh", "api", "-X", "GET", f"repos/{repo}/deployments/{deployment}/statuses", "-f", "per_page=100"])
    found = [
        Status(int(s["id"]), s["state"], s["description"] or "", parse_time(s["created_at"])) for s in json.loads(out)
    ]
    return sorted(found, key=lambda s: s.id)


def operations_rows(
    repo: str, repo_dir: Path, envs: list[Environment], issues: list[Issue], label: str, now: dt.datetime
) -> list[Row]:
    caller = operate_caller(repo_dir)
    runs = fetch_runs(repo, caller[0]) if caller else []
    rows = operate_rows(runs, caller[0]) if caller else []
    deployments = {e.name: fetch_deployments(repo, e.name) for e in envs}
    current = {e.name: current_deployment(deployments[e.name], lambda i: fetch_statuses(repo, i)) for e in envs}
    for env in envs:
        row = unhealthy_row(env.name, current[env.name], now)
        if row is not None:
            rows.append(row)
    held = any(HOLD_LABEL in i.labels for i in issues)
    proposals = proposal_rows(issues, caller[0] if caller else "operate.yml", held)
    rows += proposals
    idle = operate_idle(caller, runs, now)
    if caller:
        command = f"run it once: gh workflow run {caller[0]} -f dry-run=false"
    else:
        command = "write one with `shipyard init --operate`, then run it once"
    proposed = {r.subject for r in proposals}
    for env in envs:
        if env.name in proposed or env.source is None:
            continue
        row = unpromoted_row(env, current.get(env.source), deployments[env.name], idle, command, now)
        if row is not None:
            rows.append(row)
    return rows + incident_rows(issues, label, now)


# -- intake --------------------------------------------------------------------------------


def intake_rows(repo: str) -> list[Row]:
    proc = subprocess.run(
        [sys.executable, str(TRIAGE_STATE), repo, "--json"], capture_output=True, text=True, check=False
    )
    if proc.returncode not in (0, 1):
        sys.stderr.write(f"error: triage_state.py: {proc.stderr.strip()}\n")
        raise SystemExit(2)
    counts: dict[str, list[str]] = {}
    for line in proc.stdout.splitlines():
        row = json.loads(line)
        counts.setdefault(row["state"], []).append(f"#{row['number']}")
    rows = []
    if proc.returncode == 1:
        acted = {"NEW", "NEEDS_PR", "UNBLOCKED", "REVISIT", "DONE_NOT_CLOSED", "SUSPECT_CLOSE"}
        detail = "; ".join(f"{s} {' '.join(n)}" for s, n in sorted(counts.items()) if s in acted)
        rows.append(Row("ISSUES", repo, detail))
    prs = json.loads(run(["gh", "pr", "list", "-R", repo, "--json", "number,isDraft", "-L", "100"]))
    ready = [f"#{p['number']}" for p in prs if not p["isDraft"]]
    if ready:
        rows.append(Row("PRS_OPEN", repo, " ".join(ready)))
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("repo", help="owner/name")
    parser.add_argument("--repo-dir", default=".", help="local checkout (default: cwd)")
    parser.add_argument("--releases", type=int, default=3, help="newest version tags to check")
    parser.add_argument("--grace", type=int, default=20, help="minutes a run or an upload may take")
    parser.add_argument("--tool", default="git+https://github.com/romamo/shipyard@v0", help="where uvx gets shipyard")
    parser.add_argument("--json", action="store_true", help="JSON lines instead of a table")
    args = parser.parse_args()
    if "/" not in args.repo:
        parser.error("repo must be owner/name")
    if not 1 <= args.releases <= 20:
        parser.error("--releases must be 1..20")
    repo_dir = Path(args.repo_dir).resolve()
    now = dt.datetime.now(dt.timezone.utc)  # noqa: UP017 (dt.UTC needs 3.11)
    grace = dt.timedelta(minutes=args.grace)

    branch = run(
        ["gh", "repo", "view", args.repo, "--json", "defaultBranchRef", "-q", ".defaultBranchRef.name"]
    ).strip()
    run(["git", "fetch", "-q", "--force", "--tags", "origin", branch], cwd=repo_dir)

    rows: list[Row] = []
    bot = bot_workflow(repo_dir)
    policy = policy_file(repo_dir)
    if bot is None or policy is None:
        rows.append(Row("BOT_NONE", args.repo, 'no release policy: releases by "tag X"'))
    else:
        workflow, shipyard = bot
        due = planned_release(args.repo, repo_dir, policy, branch, args.tool) if shipyard else None
        rows += bot_rows(fetch_runs(args.repo, workflow), due, now, grace, workflow)

    tags = version_tags(repo_dir)
    package = package_name(repo_dir)
    registered = package is not None and on_pypi(package)
    for i, tag in enumerate(tags[: args.releases]):
        listed = on_pypi(f"{package}/{tag.name[1:]}") if registered else None
        row = publish_row(tag, package if registered else None, listed, now, grace)
        rows.append(row)
        if row.state in {"PUBLISHED", "NO_REGISTRY"} and i + 1 < len(tags):
            issues = unannounced(args.repo, repo_dir, tags[i + 1].name, tag.name)
            if issues:
                rows.append(Row("UNANNOUNCED", tag.name, f"{' '.join(issues)} (since {tags[i + 1].name})"))

    if policy is not None:
        text = (repo_dir / policy).read_text()
        envs = environments(text, policy)
        issues = fetch_issues(args.repo)
        rows += hold_rows(issues, now)
        if envs:
            rows += operations_rows(args.repo, repo_dir, envs, issues, incident_label(text), now)

    rows += intake_rows(args.repo)
    for row in ordered(rows):
        print(json.dumps(row.__dict__, sort_keys=True) if args.json else row.text())
    return 1 if any(r.state in ACTION for r in rows) else 0


if __name__ == "__main__":
    sys.exit(main())
