#!/usr/bin/env python3
"""Report what a repo's issue-to-release pipeline still owes: the release bot, the
recent releases, the environments shipmill operate runs, and the issue intake.

Usage: watch_state.py <owner/repo> [--repo-dir PATH] [--releases N] [--grace MIN]
                      [--tool SPEC] [--incident-label LABEL] [--json]
                      [--bot-login LOGIN] [--trusted-only]

Release bot (a repo with .github/shipmill.toml):
  BOT_FAILED      the bot's latest finished run failed (cancelled runs are ignored:
                  a newer push cancels the settle wait on purpose)
  BOT_STALLED     a shipmill bot in release mode has a release due now, no run of
                  its workflow is queued or running, and none started in --grace minutes
  BOT_OK          neither; BOT_NONE when the repo has no bot (it releases by "tag X")
  WORK_BRANCH_STALE  for a shipmill bot: a shipmill/v* work branch on origin while no run of
                  the workflow is unfinished (asked by status, however old), so no run owns it: a
                  run cancelled before its cleanup job left it, and each later run stops
                  at it unless its prepare job may list the runs (actions: read)
  WORKTREE_STALE  for a shipmill bot: a worktree `shipmill worktrees` keeps, under
                  .claude/worktrees/ or tmp/wt-*, created over 7 days ago, with why it is
                  kept (reported, never an action: a person finishes, pushes, or removes
                  the work; the main checkout, the --repo-dir checkout, and worktrees that
                  aren't shipmill's are never reported)

Each of the --releases newest version tags (default 3):
  NOT_PUBLISHED   the package is on PyPI but this version isn't, --grace minutes after
                  the tag
  PUBLISHING      the same, inside --grace
  PUBLISHED       PyPI lists it; NO_REGISTRY when the package isn't on PyPI at all
  UNANNOUNCED     closed issues it fixed that have no "Released in <tag>" comment
                  (github-pr-triage's shipped.py, in plan mode)

Hold and postmortems (a repo with a shipmill config):
  HOLD            an open shipmill-hold issue, with who opened it and when (reported, never
                  an action by itself: a person stopped the factory on purpose)
  POSTMORTEM_DUE  an issue labelled [operate] incident_label (default "incident") closed as
                  completed (not as not planned or a duplicate) that no docs/postmortems/*.md
                  on the default branch names in an "Incident: owner/repo#N" line (or the
                  issue's URL, or "#N" for the repo itself), read through the GitHub
                  contents API, not the checkout

Operations (only when the config declares environments; read from the
deployments and issues shipmill operate writes):
  OPERATE_FAILED  the latest finished run of the workflow that calls shipmill's operate.yml
                  failed (cancelled runs are ignored)
  UNHEALTHY       an environment's newest "shipmill health" deployment status is a failure
  PROMOTION_DUE   an open "Ready to promote" proposal issue, with its approve command; or a
                  `from` environment operate would promote (act) or propose promoting
                  (propose, or the hold) now if it ran: its source baked a release
                  (bake_minutes since its success, or since the first health status after
                  a failed check), it is behind that release and never tried it, and
                  deploy.<name> autonomy isn't observe; yet no operate workflow runs on a
                  schedule (no caller, no schedule in it, or no run in the last hour).
                  Unlike operate, it doesn't look for a deploy run started on the tag that
                  hasn't made its deployment yet, so a hand-started deploy can read as due
                  for those few minutes
  INCIDENT_OPEN   an open issue labelled [operate] incident_label (default "incident"),
                  with its age and the pull requests linked to close it

Intake (github-issue-triage's triage_state.py):
  ISSUES          issues needing triage action, counted by state
  ISSUES_OPEN     the other open issues, by state: in progress, blocked, triaged, or
                  postponed, less the ones NEEDS_DECISION and UNTRUSTED list (reported,
                  never an action by itself)
  PRS_OPEN        open non-draft pull requests (reported, never an action by itself),
                  less the ones NEEDS_DECISION and UNTRUSTED list
  NEEDS_DECISION  issues and pull requests labelled needs-decision whose question has no
                  reply, as #N only (triage_state.py's rule, applied to pull requests'
                  comments too): a person owes the answer, so an action, but never an
                  agent's
  UNTRUSTED       with --trusted-only, the issues triage_state.py reads as UNTRUSTED and
                  the pull requests whose head is in a fork, as #N only (reported, never an
                  action: an interactive session takes them up, D-16)

--bot-login and --trusted-only go through to triage_state.py: the login shipmill's
sessions write as (whose comment is the question, and whose issue is trusted), and the
trust filter of an unattended gate.

Repository settings:
  BRANCH_DELETE_OFF  delete_branch_on_merge is off: merged pull request branches stay on
                  GitHub, and a stacked pull request isn't retargeted when the one under it
                  merges. A person turns it on (shipmill-setup's setup_state.py --fix does), so
                  an action but never an agent's; no row while it is on

Workflows:
  RUNS_ACTIVE     a queued or running workflow run of any workflow, one row each, with its
                  status, age, event, branch, and link (reported, never an action by itself)

Agents (all reported, never an action by itself):
  TRIAGE_MODE     the config's [agents] table, what the gate's sessions are told to do: its
                  prompt, whether open pull requests count as work, and the rest of its keys;
                  "no [agents]" without one
  AGENT_SESSION   a Claude Code session on this host working the repo, one row each, from
                  `claude agents --json`: a gate session (named "shipmill <owner/repo>") or one
                  whose folder is in the checkout, with its kind, status, and age
  LOOP            on a Mac, the launchd job running `shipmill gate` for the repo: its
                  interval, whether it runs now, its last exit code, and its log's last
                  decision; "none" without one. A /loop or a /schedule routine lives in a
                  session or the cloud, where no script reads it
  HOST_UNKNOWN    the agent sessions weren't read: claude isn't on PATH (a cloud session)

shipmill on this host (from `claude plugin list --json` and `claude plugin marketplace list --json`):
  SHIPMILL_VERSION   the latest shipmill release, the shipmill@shipmill plugin's installs
                  that apply to the repo (user scope, and local or project scope in the
                  checkout or the gate's launchd working directory, each taken as the project
                  Claude Code loads it as: a linked worktree's main checkout), and the marketplace's
                  source when it isn't shipmill/shipmill (reported, never an action)
  SHIPMILL_OUTDATED  an install of those older than the latest release, with the command
                  that updates it: an action for a person, never an agent's

--incident-label names the label the repo's incidents carry, in place of the config's
[operate] incident_label (fleet.py passes a fleet file's incident_label this way).

--json prints one JSON object per row: state, subject, detail, and agent, true when the
row needs an agent (BOT_FAILED, BOT_STALLED, WORK_BRANCH_STALE, NOT_PUBLISHED, UNANNOUNCED,
ISSUES, OPERATE_FAILED, INCIDENT_OPEN): the rows shipmill gate starts a session for. A waiting
item is in no agent row, so it neither starts a session nor changes the gate's fingerprint.

Holds and incidents lead the report. Exit 0 when nothing needs action, 1 when any
BOT_FAILED, BOT_STALLED, WORK_BRANCH_STALE, NOT_PUBLISHED, UNANNOUNCED, ISSUES,
OPERATE_FAILED, UNHEALTHY, PROMOTION_DUE, INCIDENT_OPEN, POSTMORTEM_DUE, or NEEDS_DECISION
row is present, 2 on bad input or a git,
gh, or uvx failure (a failing `shipmill plan` or `shipmill worktrees`).
Needs git, an authenticated gh, and uvx (for a shipmill bot's plan and worktrees; `shipmill
worktrees` needs claude on PATH too, to see the live sessions). Python 3.10+,
standard library only.
"""

from __future__ import annotations

import argparse
import datetime as dt
import importlib.util
import json
import os
import plistlib
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from types import ModuleType
from typing import Any

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10: the config is read with regexes instead
    tomllib = None

SKILLS = Path(__file__).resolve().parents[2]
SHIPPED = SKILLS / "github-pr-triage" / "scripts" / "shipped.py"
TRIAGE_STATE = SKILLS / "github-issue-triage" / "scripts" / "triage_state.py"
# triage_state.py's ACTION: the issue states that make it exit 1
TRIAGE_ACTION = {
    "NEW",
    "NEEDS_PR",
    "UNBLOCKED",
    "UNFILLED",
    "SPEC_REFUSED",
    "REVISIT",
    "DONE_NOT_CLOSED",
    "SUSPECT_CLOSE",
}
POLICY = Path(".github/shipmill.toml")


def triage_module() -> ModuleType:
    """triage_state.py itself, for its needs-decision rule: one rule for issues and pull requests"""
    spec = importlib.util.spec_from_file_location("shipmill_triage_state", TRIAGE_STATE)
    if spec is None or spec.loader is None:
        sys.stderr.write(f"error: can't load {TRIAGE_STATE}\n")
        raise SystemExit(2)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


TRIAGE = triage_module()
GATE_SESSION = "shipmill "  # shipmill gate names its sessions "shipmill <owner/repo> ..."
LAUNCH_AGENTS = Path.home() / "Library" / "LaunchAgents"
SHIPMILL_REPO = "shipmill/shipmill"  # where shipmill releases
PLUGIN = "shipmill@shipmill"  # the Claude Code plugin, in its marketplace
# the gate log's decision lines: QUIET, LAUNCH, RUNNING, WAITING, UNCHANGED, HELD, or an error
GATE_DECISION = re.compile(r"^(?:[A-Z]+|error): ")
VERSION_TAG = re.compile(r"^v\d+\.\d+")  # skips moving major tags such as v0
ACTION = {
    "BOT_FAILED",
    "BOT_STALLED",
    "WORK_BRANCH_STALE",
    "NOT_PUBLISHED",
    "UNANNOUNCED",
    "ISSUES",
    "OPERATE_FAILED",
    "UNHEALTHY",
    "PROMOTION_DUE",
    "INCIDENT_OPEN",
    "POSTMORTEM_DUE",
    "NEEDS_DECISION",
    "BRANCH_DELETE_OFF",
    "SHIPMILL_OUTDATED",
}
# the states whose row needs an agent: --json marks each row's "agent" from this, and
# shipmill gate starts a session on those rows (SKILL.md's repair table says what it does)
AGENT = {
    "BOT_FAILED",
    "BOT_STALLED",
    "WORK_BRANCH_STALE",
    "NOT_PUBLISHED",
    "UNANNOUNCED",
    "ISSUES",
    "OPERATE_FAILED",
    "INCIDENT_OPEN",
}
STALE = dt.timedelta(days=7)  # a kept shipmill worktree older than this is reported
# the reasons `shipmill worktrees` keeps a worktree that isn't a shipmill worktree at all
NOT_SHIPMILL = {"main checkout", "current checkout", "not a shipmill worktree"}
LEAD = ("INCIDENT_OPEN", "HOLD")  # the report starts with these, in this order
# the statuses of a run that hasn't finished: shipmill's github.ACTIVE_STATUSES, in its order
UNFINISHED = ("queued", "in_progress", "waiting", "pending", "requested")
ACTIVE = set(UNFINISHED)
# a release's work branch: shipmill's land.WORK_PREFIX and a version tag
WORK_BRANCH = re.compile(r"^refs/heads/(shipmill/v\d+\.\d+\S*)$")
HOLD_LABEL = "shipmill-hold"  # shipmill's autonomy.HOLD_LABEL
INCIDENT_LABEL = "incident"  # the default of [operate] incident_label
HEALTH_PREFIX = "shipmill health"  # starts the description of every status shipmill operate writes
OPERATE_SILENT = dt.timedelta(hours=1)  # a scheduled operate runs every 10 minutes
AUTONOMY = ("observe", "propose", "act")
RELEASE_TAG = re.compile(r"v(\d+)\.(\d+)\.(\d+)(?:(a|b|rc)(\d+))?(?:\.dev(\d+))?")  # shipmill's version.PATTERN
PRE_RANK = {"a": 1, "b": 2, "rc": 3}
POSTMORTEMS = "docs/postmortems"  # one file per incident, named in an "Incident: owner/repo#N" line
# "Incident: owner/repo#N", the issue's URL, or "#N" for the repo itself; one per line
POSTMORTEM_NAMES = re.compile(
    r"^Incident:[ \t]*(?:https://github\.com/([\w.-]+/[\w.-]+)/issues/(\d+)|([\w.-]+/[\w.-]+)?#(\d+))[ \t]*$",
    re.MULTILINE,
)
POSTMORTEM_REASONS = {None, "", "COMPLETED"}  # closed as done; "" or None on issues closed before GitHub kept a reason
ISSUE_LIMIT = 1000  # per label or search; no repo has that many holds, incidents, or proposals
PROPOSAL_LABEL = "shipmill-proposal"  # shipmill's github.PROPOSAL_LABEL
PROPOSAL_SEARCH = 'in:title "Ready to"'  # proposals opened before the label; the marker in the body decides
PROPOSAL = re.compile(r"<!-- shipmill:propose deploy=(?P<env>\S+) -->")  # shipmill's operate.deploy_marker
PROPOSED_TAG = re.compile(r"<!-- shipmill:tag=(?P<tag>\S+) -->")
OPERATE_USES = re.compile(
    r"^\s*(?:-\s*)?uses:\s*[\"']?(?:[\w.-]+/[\w.-]+/\.github/workflows/operate\.ya?ml@|\./\.github/workflows/operate\.ya?ml)",
    re.MULTILINE,
)


class Refused(SystemExit):
    """Bad input: the message goes to stderr, and the watch exits 2"""

    def __init__(self, message: str) -> None:
        super().__init__(2)
        self.message = message

    def __str__(self) -> str:
        return self.message


@dataclass(frozen=True)
class Row:
    state: str
    subject: str
    detail: str

    def text(self) -> str:
        return f"{self.state:<14} {self.subject:<16} {self.detail}"

    def json(self) -> dict[str, str | bool]:
        return {"state": self.state, "subject": self.subject, "detail": self.detail, "agent": self.state in AGENT}


@dataclass(frozen=True)
class Run:
    status: str
    conclusion: str
    created: dt.datetime
    url: str


@dataclass(frozen=True)
class ActiveRun:
    """A workflow run of any workflow, as the RUNS_ACTIVE row shows it"""

    workflow: str
    status: str
    event: str
    branch: str
    created: dt.datetime
    url: str


@dataclass(frozen=True)
class Tag:
    name: str
    created: dt.datetime


def run(cmd: list[str], cwd: Path | None = None, env: dict[str, str] | None = None) -> str:
    return checked(cmd, subprocess.run(cmd, cwd=cwd, env=env, capture_output=True, text=True, check=False))


def capture(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, capture_output=True, text=True, check=False)


def checked(cmd: list[str], proc: subprocess.CompletedProcess[str]) -> str:
    """The command's output; a failed command stops the watch (exit 2)"""
    if proc.returncode != 0:
        sys.stderr.write(f"error: {' '.join(cmd[:3])}...: {proc.stderr.strip()}\n")
        raise SystemExit(2)
    return proc.stdout


def parse_time(text: str) -> dt.datetime:
    return dt.datetime.fromisoformat(text.replace("Z", "+00:00"))


# -- release bot ---------------------------------------------------------------------------


def policy_file(repo_dir: Path) -> Path | None:
    """The release policy shipmill reads, or None without one"""
    return POLICY if (repo_dir / POLICY).is_file() else None


def bot_workflow(repo_dir: Path) -> tuple[str, bool] | None:
    """The bot's workflow file and whether it is a shipmill bot, or None without a bot"""
    policy = policy_file(repo_dir)
    if policy is None:
        return None
    workflows = repo_dir / ".github" / "workflows"
    caller = workflows / "release.yml"
    if caller.is_file() and re.search(r"shipmill|release-lanes|\./\.github/workflows/prepare\.yml", caller.read_text()):
        return "release.yml", True
    if (workflows / "release-bot.yml").is_file():
        return "release-bot.yml", False
    raise Refused(f"error: {policy} exists but neither release.yml nor release-bot.yml calls a bot")


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


def work_branch_rows(branches: dict[str, str], runs: list[Run], workflow: str) -> list[Row]:
    """Each work branch on origin (name to commit) while none of the workflow's unfinished
    runs (fetch_unfinished) is queued or in progress: no run owns it, so it stops every
    later run"""
    if any(r.status in ACTIVE for r in runs):
        return []
    return [
        Row("WORK_BRANCH_STALE", name, f"at {sha[:12]}, and no run of {workflow} is queued or in progress")
        for name, sha in sorted(branches.items())
    ]


def work_branches(repo_dir: Path) -> dict[str, str]:
    """The shipmill/v* work branches on origin, asked of origin itself, with their commits"""
    found = {}
    for line in run(["git", "ls-remote", "--heads", "origin"], cwd=repo_dir).splitlines():
        sha, _, ref = line.partition("\t")
        match = WORK_BRANCH.match(ref)
        if match:
            found[match.group(1)] = sha
    return found


def fetch_runs(repo: str, workflow: str) -> list[Run]:
    out = run(
        ["gh", "run", "list", "-R", repo, "-w", workflow, "-L", "10", "--json", "status,conclusion,createdAt,url"]
    )
    return [Run(r["status"], r["conclusion"] or "", parse_time(r["createdAt"]), r["url"]) for r in json.loads(out)]


def fetch_unfinished(
    repo: str, workflow: str, execute: Callable[[list[str]], subprocess.CompletedProcess[str]] = capture
) -> list[Run]:
    """The workflow's runs that haven't finished, newest first, asked of GitHub by status as
    prepare's active_runs does, however many newer runs have finished. A failed query stops
    the watch: it must never read as no owner, which deletes a branch"""
    found = {}
    # in_progress is asked again last: a run approved after the first in_progress query and
    # before the waiting one would otherwise be in neither list
    for status in (*UNFINISHED, "in_progress"):
        cmd = ["gh", "run", "list", "-R", repo, "-w", workflow, "-s", status, "-L", "100"]
        cmd += ["--json", "status,conclusion,createdAt,url"]
        listed = json.loads(checked(cmd, execute(cmd)))
        if not isinstance(listed, list):
            raise Refused(f"error: gh run list -s {status} printed {type(listed).__name__}, not a JSON array")
        for r in listed:
            found[r["url"]] = Run(r["status"], r["conclusion"] or "", parse_time(r["createdAt"]), r["url"])
    return sorted(found.values(), key=lambda r: r.created, reverse=True)


def planned_release(repo: str, repo_dir: Path, policy: Path, branch: str, tool: str) -> str | None:
    """What the shipmill planner would release now on the default branch, or None"""
    work = repo_dir / "tmp" / f"ship-watch-{os.getpid()}"
    run(["git", "worktree", "add", "-q", "--detach", str(work), f"origin/{branch}"], cwd=repo_dir)
    try:
        env = {**os.environ, "GITHUB_REPOSITORY": repo}
        out = run(["uvx", "--from", tool, "shipmill", "plan", "--event", "schedule", "--dry-run"], cwd=work, env=env)
    finally:
        run(["git", "worktree", "remove", "--force", str(work)], cwd=repo_dir)
    decision = json.loads(out)
    policy_mode = re.search(r'^mode\s*=\s*"(\w[\w-]*)"', (repo_dir / policy).read_text(), re.MULTILINE)
    if decision["action"] != "release" or policy_mode is None or policy_mode.group(1) != "release":
        return None
    return str(decision["reason"])


def stale_rows(text: str) -> list[Row]:
    """`shipmill worktrees --json`'s kept shipmill worktrees created over STALE ago"""
    try:
        report = json.loads(text)
    except json.JSONDecodeError as exc:
        raise Refused(f"error: shipmill worktrees --json printed no JSON: {exc}") from None
    trees = report.get("worktrees") if isinstance(report, dict) else None
    if not isinstance(trees, list):
        raise Refused(f"error: shipmill worktrees --json printed {text[:200]!r}, not a worktrees list")
    rows = []
    for tree in trees:
        if not isinstance(tree, dict) or not all(isinstance(tree.get(k), str) for k in ("path", "verdict")):
            raise Refused(f"error: shipmill worktrees --json printed a row without a path and verdict: {tree!r}")
        if tree["verdict"] != "KEPT" or tree.get("reason") in NOT_SHIPMILL:
            continue
        reason, age = tree.get("reason"), tree.get("age_hours")
        if not isinstance(reason, str) or not isinstance(age, int) or isinstance(age, bool):
            raise Refused(f"error: shipmill worktrees --json printed a kept row without a reason and age: {tree!r}")
        if dt.timedelta(hours=age) > STALE:
            rows.append(Row("WORKTREE_STALE", tree["path"], f"{reason}; created {age // 24}d ago"))
    return rows


def stale_worktrees(repo_dir: Path, tool: str) -> list[Row]:
    """The kept shipmill worktrees of the checkout's repository created over STALE ago"""
    return stale_rows(run(["uvx", "--from", tool, "shipmill", "--repo", str(repo_dir), "worktrees", "--json"]))


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
    deploy: str = "act"  # [autonomy] deploy.<name>: observe, propose, or act (the default, D-7)


@dataclass(frozen=True)
class Deployment:
    id: int
    ref: str  # a release tag, as shipmill dispatches deploys on the tag
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
    closed: dt.datetime | None = None
    state_reason: str | None = None  # a closed issue's: COMPLETED, NOT_PLANNED, or DUPLICATE


@dataclass(frozen=True)
class Config:
    """What the watch reads from the shipmill config"""

    environments: list[Environment]
    incident_label: str


def ago(span: dt.timedelta) -> str:
    minutes = int(span.total_seconds() // 60)
    if minutes < 120:
        return f"{minutes} min"
    if minutes < 48 * 60:
        return f"{minutes // 60} h"
    return f"{minutes // (24 * 60)} d"


def config(text: str, policy: Path, incident_label: str | None = None) -> Config:
    """The environments and the incident label of the config, read as shipmill reads it: with
    tomllib on Python 3.11+, with the regex fallback on 3.10. A given incident_label (the
    --incident-label flag) takes the place of the config's, which is still checked"""
    if tomllib is None:
        envs, levels = environments_310(text, policy), deploy_autonomy_310(text, policy)
        label = incident_label_310(text, policy)
    else:
        try:
            raw = tomllib.loads(text)
        except tomllib.TOMLDecodeError as exc:
            raise Refused(f"error: {policy}: {exc}") from None
        envs, levels = environments_toml(raw, policy), deploy_autonomy_toml(raw, policy)
        label = incident_label_toml(raw, policy)
    return Config([replace(e, deploy=levels.get(e.name, "act")) for e in envs], incident_label or label)


def autonomy_level(value: object, name: str, policy: Path) -> str:
    if not isinstance(value, str) or value not in AUTONOMY:
        raise Refused(f"error: {policy}: [autonomy] deploy.{name} must be one of {list(AUTONOMY)}, got {value!r}")
    return value


def deploy_autonomy_toml(raw: dict[str, object], policy: Path) -> dict[str, str]:
    """[autonomy] deploy.<name> of the parsed config, per environment that sets it"""
    table = raw.get("autonomy", {})
    deploy = table.get("deploy", {}) if isinstance(table, dict) else None
    if not isinstance(deploy, dict):
        raise Refused(f"error: {policy}: [autonomy] deploy is a table of environments")
    return {name: autonomy_level(value, name, policy) for name, value in deploy.items()}


def environments_toml(raw: dict[str, object], policy: Path) -> list[Environment]:
    """The [environments] tables of the parsed config, in the order the config declares them"""
    tables = raw.get("environments", {})
    if not isinstance(tables, dict):
        raise Refused(f"error: {policy}: environments is not a table")
    found = []
    for name, table in tables.items():
        if not isinstance(table, dict):
            raise Refused(f"error: {policy}: environments.{name} is not a table")
        source = table.get("from")
        bake = table.get("bake_minutes", 0)
        if not (source is None or isinstance(source, str)) or isinstance(bake, bool) or not isinstance(bake, int):
            raise Refused(f"error: {policy}: environments.{name} has a malformed from or bake_minutes")
        found.append(Environment(name, source or None, bake))
    return found


def incident_label_toml(raw: dict[str, object], policy: Path) -> str:
    """[operate] incident_label of the parsed config, or the default"""
    table = raw.get("operate", {})
    label = table.get("incident_label", INCIDENT_LABEL) if isinstance(table, dict) else None
    if not isinstance(label, str) or not label:
        raise Refused(f"error: {policy}: [operate] incident_label must be a non-empty string")
    return label


# -- the Python 3.10 fallback: no tomllib, so the plain forms are read with regexes, and any
# other form is refused rather than misread


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
    found = re.search(rf"^\s*[\"']?{re.escape(key)}[\"']?\s*=\s*(?:\"([^\"]*)\"|'([^']*)')", body, re.MULTILINE)
    return None if found is None else found.group(1) if found.group(1) is not None else found.group(2)


def blank(body: str) -> bool:
    """Only whitespace and comments"""
    return all(not line.strip() or line.strip().startswith("#") for line in body.splitlines())


def environments_310(text: str, policy: Path) -> list[Environment]:
    """The [environments.<name>] tables of the config, in order; an empty list without any"""
    unreadable = Refused(
        f"error: {policy}: can't read [environments] on Python 3.10: use 3.11+ or plain [environments.<name>] tables"
    )
    tables = toml_tables(text)
    if not blank(tables.get("environments", "")) or re.search(
        r"^\s*[\"']?environments[\"']?\s*[.=]", tables.get("", ""), re.MULTILINE
    ):
        raise unreadable
    found = []
    for name, body in tables.items():
        parts = name.split(".")
        if parts[0] != "environments" or len(parts) == 1:
            continue
        if len(parts) != 2:
            raise unreadable
        bake = re.search(r"^\s*[\"']?bake_minutes[\"']?\s*=\s*(\d+)\s*(?:#.*)?$", body, re.MULTILINE)
        if bake is None and re.search(r"^\s*[\"']?bake_minutes[\"']?\s*=", body, re.MULTILINE):
            raise unreadable
        found.append(Environment(parts[1], toml_string(body, "from") or None, int(bake.group(1)) if bake else 0))
    return found


def deploy_autonomy_310(text: str, policy: Path) -> dict[str, str]:
    """[autonomy] deploy.<name>, written as dotted keys in a plain [autonomy] table or as keys
    of a plain [autonomy.deploy] table"""
    unreadable = Refused(
        f"error: {policy}: can't read [autonomy] deploy on Python 3.10: use 3.11+, deploy.<name> keys in a plain"
        " [autonomy] table, or a plain [autonomy.deploy] table"
    )
    tables = toml_tables(text)
    if re.search(r"^\s*[\"']?autonomy[\"']?\s*[.=]", tables.get("", ""), re.MULTILINE):
        raise unreadable
    if any(name.startswith("autonomy.") and name != "autonomy.deploy" for name in tables):
        raise unreadable
    level = r"\s*=\s*(?:\"(?P<level>[^\"]*)\"|'(?P<plain>[^']*)')\s*(?:#.*)?$"
    found: list[tuple[str, str]] = []
    for line in tables.get("autonomy", "").splitlines():
        if not re.match(r"^\s*[\"']?deploy\b", line):
            continue
        key = re.match(rf"^\s*deploy\s*\.\s*[\"']?(?P<name>[A-Za-z0-9][A-Za-z0-9_.-]*?)[\"']?{level}", line)
        if key is None:
            raise unreadable
        found.append((key["name"], key["level"] if key["level"] is not None else key["plain"]))
    for line in tables.get("autonomy.deploy", "").splitlines():
        if blank(line):
            continue
        key = re.match(rf"^\s*[\"']?(?P<name>[A-Za-z0-9][A-Za-z0-9_.-]*?)[\"']?{level}", line)
        if key is None:
            raise unreadable
        found.append((key["name"], key["level"] if key["level"] is not None else key["plain"]))
    return {name: autonomy_level(value, name, policy) for name, value in found}


def incident_label_310(text: str, policy: Path) -> str:
    """[operate] incident_label, or the default"""
    tables = toml_tables(text)
    if re.search(r"^\s*[\"']?operate[\"']?\s*[.=]", tables.get("", ""), re.MULTILINE):
        raise Refused(f"error: {policy}: can't read [operate] on Python 3.10: use 3.11+ or a plain [operate] table")
    body = tables.get("operate", "")
    label = toml_string(body, "incident_label")
    if label is None and re.search(r"^\s*[\"']?incident_label[\"']?\s*=", body, re.MULTILINE):
        raise Refused(f"error: {policy}: can't read [operate] incident_label on Python 3.10: use 3.11+")
    if label == "":
        raise Refused(f"error: {policy}: [operate] incident_label must be a non-empty string")
    return label or INCIDENT_LABEL


def operate_caller(repo_dir: Path) -> tuple[str, bool] | None:
    """The workflow that calls shipmill's operate.yml and whether it runs on a schedule, or None"""
    workflows = repo_dir / ".github" / "workflows"
    if not workflows.is_dir():
        return None
    for path in sorted([*workflows.glob("*.yml"), *workflows.glob("*.yaml")]):
        text = path.read_text()
        if OPERATE_USES.search(text):
            return path.name, re.search(r"^\s*schedule:", text, re.MULTILINE) is not None
    return None


def current_deployment(deployments: list[Deployment], statuses_of: Callable[[int], list[Status]]) -> Current | None:
    """The newest of the deployments, newest first, that reached success, as shipmill operate reads it"""
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
    """Why no shipmill operate runs on a schedule, or None while one does"""
    if caller is None:
        return "no workflow calls shipmill's operate.yml"
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


def proposal_issues(labelled: list[Issue], search: Callable[[], list[Issue]]) -> list[Issue]:
    """The open proposals: by their label, and always by the title search too, for one opened
    before the label (shipmill labels each on its next update), one issue once by number"""
    seen = {i.number for i in labelled}
    return labelled + [i for i in search() if i.number not in seen]


def proposal_rows(issues: list[Issue], caller: str, held: bool) -> list[Row]:
    """The open proposal issues shipmill operate opens for a deploy that waits on approval"""
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


def version_key(tag: str) -> tuple[int, int, int, int, int, int] | None:
    """A release tag's order, as shipmill's Version sorts it (PEP 440: X.Y.Z.devN < X.Y.ZaN <
    X.Y.ZbN < X.Y.ZrcN < X.Y.Z); None when the tag names no release"""
    found = RELEASE_TAG.fullmatch(tag)
    if found is None:
        return None
    major, minor, patch, pre, pre_n, dev = found.groups()
    if pre is None:
        phase, n = (0, 0) if dev is not None else (4, 0)
    else:
        phase, n = PRE_RANK[pre], int(pre_n)
    return int(major), int(minor), int(patch), phase, n, int(dev) if dev is not None else 2**62


def bake_start(current: Current) -> dt.datetime | None:
    """When the current healthy stretch began, as shipmill operate counts it: from the first
    success, but after a failed health check from the first health status that followed it;
    None while the newest check failed"""
    ours = [s for s in current.statuses if s.description.startswith(HEALTH_PREFIX)]
    failures = [s for s in ours if s.state == "failure"]
    if not failures:
        return min(s.created for s in current.statuses if s.state == "success")
    recovered = [s for s in ours if s.id > failures[-1].id]
    return recovered[0].created if recovered else None


def unpromoted_row(
    env: Environment,
    source: Current | None,
    target: Current | None,
    deployments: list[Deployment],
    idle: str | None,
    held: bool,
    command: str,
    now: dt.datetime,
) -> Row | None:
    """A `from` environment that operate would promote, or propose promoting, now if it ran:
    its source baked a release, the environment is behind it and never tried it, and its
    deploy autonomy isn't observe. With operate running, its proposal issue or its deploy is
    the signal instead; with it idle, no proposal issue ever opens, so this row stands in"""
    if env.source is None or idle is None or source is None or env.deploy == "observe":
        return None  # observe never promotes, nor proposes
    wanted = version_key(source.tag)
    if wanted is None:
        return None  # operate promotes only a deployment whose ref names a release tag
    on = version_key(target.tag) if target else None
    if on is not None and on >= wanted:
        return None
    if any(d.ref.removeprefix("refs/tags/") == source.tag for d in deployments):
        return None  # tried there already, in any state: operate deploys a release once
    since = bake_start(source)
    if since is None or now - since < dt.timedelta(minutes=env.bake_minutes):
        return None
    healthy = f"{source.tag} healthy on {env.source} for {ago(now - since)}"
    if env.deploy == "act" and not held:
        would = f"operate would promote {source.tag} to {env.name} ({healthy})"
    else:  # propose, or act under the hold: operate opens a proposal issue
        after = f" after the {HOLD_LABEL} issues close" if held else ""
        approve = f"approve with `shipmill operate --approve {env.name}` once it runs{after}"
        would = f"operate would propose promoting {source.tag} to {env.name} ({approve}; {healthy})"
    return Row("PROMOTION_DUE", env.name, f"{would}, but {idle}; {command}")


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


def postmortem_named(texts: list[str], repo: str) -> set[int]:
    """The incidents of the repo that the postmortems name, by number"""
    named = set()
    for text in texts:
        for url_repo, url_number, short_repo, number in POSTMORTEM_NAMES.findall(text):
            owner_repo = url_repo or short_repo or repo  # a plain #N is the repo's own
            if owner_repo.lower() == repo.lower():
                named.add(int(url_number or number))
    return named


def postmortem_rows(issues: list[Issue], label: str, texts: list[str], repo: str, now: dt.datetime) -> list[Row]:
    """A closed incident that no postmortem names; an open one isn't due yet, and one closed
    as not planned or as a duplicate was no incident"""
    named = postmortem_named(texts, repo)
    rows = []
    for i in issues:
        if label not in i.labels or i.closed is None or i.state_reason not in POSTMORTEM_REASONS or i.number in named:
            continue
        missing = f"no {POSTMORTEMS}/*.md names it (Incident: {repo}#{i.number})"
        rows.append(Row("POSTMORTEM_DUE", f"#{i.number}", f"{i.title}; closed {ago(now - i.closed)} ago, {missing}"))
    return rows


def postmortem_texts(repo: str, branch: str) -> list[str]:
    """Every docs/postmortems/*.md on the default branch, through the contents API; none
    without the folder"""
    listing = ["gh", "api", "-X", "GET", f"repos/{repo}/contents/{POSTMORTEMS}", "-f", f"ref={branch}"]
    proc = subprocess.run(listing, capture_output=True, text=True, check=False)
    raw = ["gh", "api", "-X", "GET", "-H", "Accept: application/vnd.github.raw+json"]
    return [
        run([*raw, f"repos/{repo}/contents/{path}", "-f", f"ref={branch}"])
        for path in postmortem_paths(proc, repo, branch)
    ]


def postmortem_paths(proc: subprocess.CompletedProcess[str], repo: str, branch: str) -> list[str]:
    """The *.md files of the folder listing; none when the folder is missing. Any other
    failure, a missing ref included, stops the watch"""
    if proc.returncode != 0 and "Not Found (HTTP 404)" in proc.stderr:
        return []
    if proc.returncode != 0:
        sys.stderr.write(f"error: gh api {POSTMORTEMS}: {proc.stderr.strip()}\n")
        raise SystemExit(2)
    entries = json.loads(proc.stdout)
    if not isinstance(entries, list):
        raise Refused(f"error: {repo}: {POSTMORTEMS} on {branch} is not a folder")
    return [e["path"] for e in entries if e["type"] == "file" and e["name"].endswith(".md")]


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


def fetch_issues(repo: str, *filters: str, state: str = "open") -> list[Issue]:
    """The issues a filter picks: a label, or a title search, so no repo has too many"""
    fields = "number,title,body,createdAt,closedAt,stateReason,author,labels,closedByPullRequestsReferences"
    cmd = ["gh", "issue", "list", "-R", repo, "--state", state, *filters, "-L", str(ISSUE_LIMIT), "--json", fields]
    found = json.loads(run(cmd))
    if len(found) >= ISSUE_LIMIT:
        raise Refused(f"error: {repo} has {ISSUE_LIMIT}+ {state} issues for {' '.join(filters)}")
    return [
        Issue(
            int(i["number"]),
            i["title"],
            i["body"] or "",
            parse_time(i["createdAt"]),
            (i["author"] or {}).get("login", "ghost"),
            tuple(label["name"] for label in i["labels"]),
            tuple(int(p["number"]) for p in i["closedByPullRequestsReferences"]),
            parse_time(i["closedAt"]) if i["closedAt"] else None,
            i["stateReason"],
        )
        for i in found
    ]


def fetch_deployments(repo: str, environment: str) -> list[Deployment]:
    query = ["-f", f"environment={environment}", "-f", "per_page=100"]  # as shipmill operate reads them
    out = run(["gh", "api", "-X", "GET", f"repos/{repo}/deployments", *query])
    found = [Deployment(int(d["id"]), d["ref"], d["sha"], parse_time(d["created_at"])) for d in json.loads(out)]
    return sorted(found, key=lambda d: d.id, reverse=True)


def fetch_statuses(repo: str, deployment: int) -> list[Status]:
    out = run(["gh", "api", "-X", "GET", f"repos/{repo}/deployments/{deployment}/statuses", "-f", "per_page=100"])
    found = [
        Status(int(s["id"]), s["state"], s["description"] or "", parse_time(s["created_at"])) for s in json.loads(out)
    ]
    return sorted(found, key=lambda s: s.id)


def operations_rows(
    repo: str, repo_dir: Path, envs: list[Environment], held: bool, label: str, now: dt.datetime
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
    issues = proposal_issues(
        fetch_issues(repo, "--label", PROPOSAL_LABEL), lambda: fetch_issues(repo, "--search", PROPOSAL_SEARCH)
    )
    proposals = proposal_rows(issues, caller[0] if caller else "operate.yml", held)
    rows += proposals
    idle = operate_idle(caller, runs, now)
    if caller:
        command = f"run it once: gh workflow run {caller[0]} -f dry-run=false"
    else:
        command = "write one with `shipmill init --operate`, then run it once"
    proposed = {r.subject for r in proposals}
    for env in envs:
        if env.name in proposed or env.source is None:
            continue
        source = current.get(env.source)
        row = unpromoted_row(env, source, current[env.name], deployments[env.name], idle, held, command, now)
        if row is not None:
            rows.append(row)
    return rows + incident_rows(fetch_issues(repo, "--label", label), label, now)


# -- intake --------------------------------------------------------------------------------


Comments = list[tuple[str, str, str]]  # (login, author association, body), oldest first


def pr_comments(repo: str, number: int) -> Comments:
    """A pull request's conversation comments, every page, through the REST API (a Bot's
    login carries "[bot]" there, as --bot-login does)"""
    jq = '.[] | [(.user.login // "ghost"), .author_association, (.body // "")]'
    out = run(["gh", "api", "--paginate", f"repos/{repo}/issues/{number}/comments", "--jq", jq])
    return [(str(c[0]), str(c[1]), str(c[2])) for c in map(json.loads, out.splitlines())]


def intake(
    repo: str,
    code: int,
    triage: str,
    prs: list[dict[str, Any]],
    comments_of: Callable[[int], Comments],
    bot_login: str | None = None,
    trusted_only: bool = False,
) -> list[Row]:
    """The intake rows from triage_state.py's exit code and --json lines, and the open pull
    requests (number, isDraft, isCrossRepository, labels). Each pull request is UNTRUSTED
    (with trusted_only, a head in a fork), else waiting on a decision (labelled, no reply;
    its comments are read only then), else in PRS_OPEN unless a draft"""
    counts: dict[str, list[int]] = {}
    for line in triage.splitlines():
        row = json.loads(line)
        counts.setdefault(row["state"], []).append(int(row["number"]))
    rows = []
    if code == 1:
        detail = "; ".join(
            f"{s} {' '.join(f'#{n}' for n in numbers)}" for s, numbers in sorted(counts.items()) if s in TRIAGE_ACTION
        )
        rows.append(Row("ISSUES", repo, detail))
    elsewhere = TRIAGE_ACTION | {"NEEDS_DECISION", "UNTRUSTED"}  # rows of their own
    rest = "; ".join(
        f"{s} {' '.join(f'#{n}' for n in numbers)}" for s, numbers in sorted(counts.items()) if s not in elsewhere
    )
    if rest:
        rows.append(Row("ISSUES_OPEN", repo, rest))
    waiting, untrusted = list(counts.get("NEEDS_DECISION", [])), list(counts.get("UNTRUSTED", []))
    ready = []
    for pr in prs:
        number = int(pr["number"])
        labels = {label["name"] for label in pr["labels"]}
        if trusted_only and pr["isCrossRepository"]:
            untrusted.append(number)
        elif TRIAGE.DECISION_LABEL in labels and TRIAGE.waits_on_decision(comments_of(number), bot_login):
            waiting.append(number)
        elif not pr["isDraft"]:
            ready.append(f"#{number}")
    if ready:
        rows.append(Row("PRS_OPEN", repo, " ".join(ready)))
    if waiting:
        rows.append(Row("NEEDS_DECISION", repo, " ".join(f"#{n}" for n in sorted(set(waiting)))))
    if untrusted:
        rows.append(Row("UNTRUSTED", repo, " ".join(f"#{n}" for n in sorted(set(untrusted)))))
    return rows


def intake_rows(repo: str, bot_login: str | None = None, trusted_only: bool = False) -> list[Row]:
    cmd = [sys.executable, str(TRIAGE_STATE), repo, "--json"]
    cmd += ["--bot-login", bot_login] if bot_login is not None else []
    cmd += ["--trusted-only"] if trusted_only else []
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if proc.returncode not in (0, 1):
        sys.stderr.write(f"error: triage_state.py: {proc.stderr.strip()}\n")
        raise SystemExit(2)
    fields = "number,isDraft,isCrossRepository,labels"
    prs = json.loads(run(["gh", "pr", "list", "-R", repo, "--json", fields, "-L", "100"]))
    return intake(repo, proc.returncode, proc.stdout, prs, lambda n: pr_comments(repo, n), bot_login, trusted_only)


# -- agents --------------------------------------------------------------------------------


def triage_mode_row(text: str, policy: Path) -> Row:
    """The config's [agents] table, each key as the config sets it, the prompt first"""
    table = agents_table(text, policy)
    if table is None:
        return Row("TRIAGE_MODE", "[agents]", "no [agents]: no gate session runs for this repo")
    keys = sorted(table, key=lambda k: (k != "prompt", k))
    return Row("TRIAGE_MODE", "[agents]", "; ".join(f"{k} = {table[k]}" for k in keys))


def agents_table(text: str, policy: Path) -> dict[str, str] | None:
    """[agents] of the config, each value as TOML writes it; None without the table"""
    if tomllib is None:
        return agents_table_310(text, policy)
    try:
        raw = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise Refused(f"error: {policy}: {exc}") from None
    return agents_table_toml(raw, policy)


def agents_table_toml(raw: dict[str, object], policy: Path) -> dict[str, str] | None:
    table = raw.get("agents")
    if table is None:
        return None
    if not isinstance(table, dict):
        raise Refused(f"error: {policy}: agents is not a table")
    return {k: json.dumps(v) if isinstance(v, (str, bool)) else str(v) for k, v in table.items()}


def agents_table_310(text: str, policy: Path) -> dict[str, str] | None:
    """[agents] as a plain table of one-line keys; any other form is refused"""
    tables = toml_tables(text)
    if "agents" not in tables:
        return None
    if any(name.startswith("agents.") for name in tables):
        raise Refused(f"error: {policy}: can't read [agents] on Python 3.10: use 3.11+ or a plain [agents] table")
    found = {}
    for line in tables["agents"].splitlines():
        if blank(line):
            continue
        key = re.match(r"^\s*(\w+)\s*=\s*(\"[^\"]*\"|'[^']*'|[^#\s]+)\s*(?:#.*)?$", line)
        if key is None:
            raise Refused(f"error: {policy}: can't read [agents] on Python 3.10: use 3.11+ ({line.strip()!r})")
        value = key.group(2)
        found[key.group(1)] = json.dumps(value[1:-1]) if value[0] in "\"'" else value
    return found


def session_rows(text: str, repo: str, repo_dir: Path, now: dt.datetime) -> list[Row]:
    """`claude agents --json`'s sessions working the repo: the gate's, by name, and any whose
    folder is in the checkout"""
    try:
        sessions = json.loads(text)
    except json.JSONDecodeError as exc:
        raise Refused(f"error: claude agents --json printed no JSON: {exc}") from None
    if not isinstance(sessions, list):
        raise Refused("error: claude agents --json: expected a JSON array")
    rows = []
    for s in sessions:
        if not isinstance(s, dict) or not isinstance(s.get("startedAt"), int):
            raise Refused(f"error: claude agents --json printed a session without startedAt: {s!r}")
        name, cwd = str(s.get("name") or ""), Path(str(s.get("cwd") or "/"))
        gate = name == GATE_SESSION + repo or name.startswith(f"{GATE_SESSION}{repo} ")
        if not gate and cwd != repo_dir and repo_dir not in cwd.parents:
            continue
        started = dt.datetime.fromtimestamp(s["startedAt"] / 1000, tz=dt.timezone.utc)  # noqa: UP017
        status = "/".join(str(s[k]) for k in ("status", "state") if s.get(k))
        who = "gate" if gate else str(s.get("kind") or "session")
        detail = f"{who} {status}, started {ago(now - started)} ago: {name or s.get('sessionId', '')}"
        rows.append(Row("AGENT_SESSION", str(s.get("id") or s.get("pid") or ""), detail))
    return rows


def gate_label(repo: str) -> str:
    """The gate's launchd label, as shipmill's launchd.label builds it"""
    owner, _, name = repo.partition("/")
    return "dev.shipmill.gate." + re.sub(r"[^a-z0-9.-]", "-", f"{owner}.{name}".lower())


def loop_row(
    label: str, plist: dict[str, object] | None, printed: str | None, log: str | None, now: dt.datetime
) -> Row:
    """The gate's launchd job: from its plist, `launchctl print` (None when not loaded), and
    its log's text (None without a log)"""
    if plist is None:
        return Row("LOOP", "none", "no launchd job runs shipmill gate for this repo (shipmill launchd)")
    interval = plist.get("StartInterval")
    parts = [f"launchd every {ago(dt.timedelta(seconds=interval))}" if isinstance(interval, int) else "launchd"]
    if printed is None:
        parts.append("installed but not loaded")
    else:
        state = re.search(r"^\tstate = (.+)$", printed, re.MULTILINE)
        code = re.search(r"^\tlast exit code = (.+)$", printed, re.MULTILINE)
        parts.append(state.group(1) if state else "state unknown")
        if code:
            parts.append(f"last exit {code.group(1)}")
    if log is not None:
        decisions = [line for line in log.splitlines() if GATE_DECISION.match(line)]
        if decisions:
            parts.append(f"last: {decisions[-1]}")
    return Row("LOOP", label, "; ".join(parts))


def gate_plist(repo: str) -> dict[str, object] | None:
    """The gate's launchd job for the repo, on a Mac; None without one"""
    path = LAUNCH_AGENTS / f"{gate_label(repo)}.plist"
    if sys.platform != "darwin" or not path.is_file():
        return None
    with path.open("rb") as handle:
        found = plistlib.load(handle)
    if not isinstance(found, dict):
        raise Refused(f"error: {path} is not a launchd job")
    return found


def agent_rows(repo: str, repo_dir: Path, now: dt.datetime) -> list[Row]:
    """The sessions and, on a Mac, the gate's launchd job; what this host can see"""
    claude = shutil.which("claude")
    if claude is None:
        rows = [Row("HOST_UNKNOWN", "claude", "not on PATH: the agent sessions weren't read")]
    else:
        rows = session_rows(run([claude, "agents", "--json"]), repo, repo_dir, now)
    if sys.platform != "darwin":
        return rows
    label = gate_label(repo)
    plist = gate_plist(repo)
    if plist is None:
        return [*rows, loop_row(label, None, None, None, now)]
    proc = subprocess.run(
        ["launchctl", "print", f"gui/{os.getuid()}/{label}"], capture_output=True, text=True, check=False
    )
    printed = proc.stdout if proc.returncode == 0 else None
    log_path = plist.get("StandardOutPath")
    log = Path(log_path).read_text(errors="replace") if isinstance(log_path, str) and Path(log_path).is_file() else None
    return [*rows, loop_row(label, plist, printed, log, now)]


def plugin_rows(plugins: object, marketplaces: object, latest: str, folders: list[Path]) -> list[Row]:
    """The plugin's installs that apply to the repo, against the latest release: from `claude
    plugin list --json` and `claude plugin marketplace list --json`, parsed (None when claude
    isn't on PATH). An install applies at user scope, or at local or project scope in one of
    the folders"""
    newest = version_key(latest)
    if newest is None:
        raise Refused(f"error: {SHIPMILL_REPO}'s latest release {latest!r} is not a version tag")
    if plugins is None:
        return [Row("SHIPMILL_VERSION", "shipmill", f"latest {latest}; plugin: claude isn't on PATH, not read")]
    if not isinstance(plugins, list):
        raise Refused("error: claude plugin list --json: expected a JSON array")
    installs = []
    for e in plugins:
        if not isinstance(e, dict) or e.get("id") != PLUGIN:
            continue
        if not isinstance(e.get("scope"), str) or not isinstance(e.get("version"), str):
            raise Refused(f"error: claude plugin list --json: {PLUGIN} install {e!r}")
        where = e.get("projectPath")
        if e["scope"] == "user" or (isinstance(where, str) and Path(where) in folders):
            installs.append((e["scope"], e["version"], where if isinstance(where, str) else None))
    rows = []
    for scope, version, where in installs:
        key = version_key(f"v{version}")
        if key is None:
            raise Refused(f"error: {PLUGIN} {scope} install has version {version!r}")
        if key < newest:
            update = f"claude plugin update {PLUGIN} --scope {scope}"
            fix = f"in {where}: {update}" if where else update
            rows.append(Row("SHIPMILL_OUTDATED", f"plugin {scope}", f"{version}, latest {latest}; {fix}"))
    found = ", ".join(f"{scope} {version}" + (f" ({where})" if where else "") for scope, version, where in installs)
    detail = f"latest {latest}; plugin: {found or 'not installed for this repo on this host'}"
    market = PLUGIN.partition("@")[2]
    for m in marketplaces if isinstance(marketplaces, list) else []:
        origin = m.get("repo") if isinstance(m, dict) and m.get("name") == market else None
        if isinstance(origin, str) and origin.lower() != SHIPMILL_REPO:
            detail += f"; marketplace source {origin}, now {SHIPMILL_REPO}"
    return [Row("SHIPMILL_VERSION", "shipmill", detail), *rows]


def claude_json(claude: str, *args: str) -> object:
    out = run([claude, *args, "--json"])
    try:
        return json.loads(out)
    except json.JSONDecodeError as exc:
        raise Refused(f"error: claude {' '.join(args)} --json printed no JSON: {exc}") from None


def main_checkout(folder: Path, common_dir: str) -> Path:
    """The project Claude Code keys a folder's local and project installs on: a linked git
    worktree's main checkout, given `git rev-parse --git-common-dir` (#198); the folder itself
    otherwise"""
    common = Path(common_dir.strip())
    return common.parent if common.name == ".git" else folder


def project_folder(folder: Path) -> Path:
    """main_checkout() of a folder on disk; a missing folder, or one outside git, is its own"""
    if not folder.is_dir():
        return folder
    cmd = ["git", "-C", str(folder), "rev-parse", "--path-format=absolute", "--git-common-dir"]
    # LC_ALL=C: the "not a git repository" check reads git's message, which a localized git translates
    env = {**os.environ, "LC_ALL": "C"}
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False, env=env)
    if proc.returncode != 0 and "not a git repository" in proc.stderr:
        return folder
    if proc.returncode != 0:
        sys.stderr.write(f"error: git rev-parse in {folder}: {proc.stderr.strip()}\n")
        raise SystemExit(2)
    return main_checkout(folder, proc.stdout)


def shipmill_rows(repo: str, repo_dir: Path) -> list[Row]:
    """The host's shipmill plugin against shipmill's latest release"""
    latest = run(["gh", "release", "view", "-R", SHIPMILL_REPO, "--json", "tagName", "-q", ".tagName"]).strip()
    folders = [project_folder(repo_dir)]
    plist = gate_plist(repo)
    workdir = plist.get("WorkingDirectory") if plist else None
    if isinstance(workdir, str):
        folders.append(project_folder(Path(workdir)))
    claude = shutil.which("claude")
    if claude is None:
        return plugin_rows(None, None, latest, folders)
    plugins = claude_json(claude, "plugin", "list")
    return plugin_rows(plugins, claude_json(claude, "plugin", "marketplace", "list"), latest, folders)


# -- repository settings -------------------------------------------------------------------


def settings_rows(repo: str, delete_on_merge: object) -> list[Row]:
    """The repo settings the pipeline relies on, from `gh repo view`; a value that isn't a
    boolean is refused, never guessed"""
    if not isinstance(delete_on_merge, bool):
        raise Refused(f"error: gh repo view {repo}: deleteBranchOnMerge is {delete_on_merge!r}, not a boolean")
    if delete_on_merge:
        return []
    fix = f"gh repo edit {repo} --delete-branch-on-merge"
    return [Row("BRANCH_DELETE_OFF", repo, f"merged PR branches stay on GitHub; turn it on: {fix}")]


# -- workflows -----------------------------------------------------------------------------


def active_rows(runs: list[ActiveRun], now: dt.datetime) -> list[Row]:
    """The queued and running workflow runs, oldest first"""
    return [
        Row("RUNS_ACTIVE", r.workflow, f"{r.status} {ago(now - r.created)}: {r.event} on {r.branch}, {r.url}")
        for r in sorted(runs, key=lambda r: r.created)
        if r.status in ACTIVE
    ]


def fetch_active(repo: str) -> list[ActiveRun]:
    """The repo's newest runs of every workflow; a run still active is among them"""
    fields = "workflowName,status,event,headBranch,createdAt,url"
    out = run(["gh", "run", "list", "-R", repo, "-L", "100", "--json", fields])
    return [
        ActiveRun(r["workflowName"], r["status"], r["event"], r["headBranch"], parse_time(r["createdAt"]), r["url"])
        for r in json.loads(out)
    ]


def arguments() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("repo", help="owner/name")
    parser.add_argument("--repo-dir", default=".", help="local checkout (default: cwd)")
    parser.add_argument("--releases", type=int, default=3, help="newest version tags to check")
    parser.add_argument("--grace", type=int, default=20, help="minutes a run or an upload may take")
    parser.add_argument("--tool", default="git+https://github.com/shipmill/shipmill@v0", help="where uvx gets shipmill")
    parser.add_argument("--incident-label", help="the label incidents carry (default: the config's)")
    parser.add_argument("--json", action="store_true", help="JSON lines instead of a table")
    parser.add_argument("--bot-login", help="the login shipmill's sessions write as, such as <slug>[bot]")
    parser.add_argument(
        "--trusted-only", action="store_true", help="leave untrusted issues and fork pull requests to UNTRUSTED (D-16)"
    )
    return parser


def main() -> int:
    parser = arguments()
    args = parser.parse_args()
    if args.incident_label is not None and not args.incident_label.strip():
        parser.error("--incident-label must not be empty")
    if args.bot_login is not None and not args.bot_login.strip():
        parser.error("--bot-login must not be empty")
    if "/" not in args.repo:
        parser.error("repo must be owner/name")
    if not 1 <= args.releases <= 20:
        parser.error("--releases must be 1..20")
    repo_dir = Path(args.repo_dir).resolve()
    now = dt.datetime.now(dt.timezone.utc)  # noqa: UP017 (dt.UTC needs 3.11)
    grace = dt.timedelta(minutes=args.grace)

    view = json.loads(run(["gh", "repo", "view", args.repo, "--json", "defaultBranchRef,deleteBranchOnMerge"]))
    branch = view["defaultBranchRef"]["name"]
    run(["git", "fetch", "-q", "--force", "--tags", "origin", branch], cwd=repo_dir)

    rows: list[Row] = settings_rows(args.repo, view["deleteBranchOnMerge"])
    bot = bot_workflow(repo_dir)
    policy = policy_file(repo_dir)
    if bot is None or policy is None:
        rows.append(Row("BOT_NONE", args.repo, 'no release policy: releases by "tag X"'))
    else:
        workflow, shipmill = bot
        due = planned_release(args.repo, repo_dir, policy, branch, args.tool) if shipmill else None
        # the branches before the runs: a run that ends between the two reads deleted its branch
        branches = work_branches(repo_dir) if shipmill else {}
        runs = fetch_runs(args.repo, workflow)
        rows += bot_rows(runs, due, now, grace, workflow)
        # an older run can still own the branch behind newer finished ones, so the owners
        # are asked by status; only with a branch, so a normal pass makes no extra call
        owners = fetch_unfinished(args.repo, workflow) if branches else []
        rows += work_branch_rows(branches, owners, workflow)
        if shipmill:  # after the plan, whose own worktree is gone by then
            rows += stale_worktrees(repo_dir, args.tool)

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
        rows.append(triage_mode_row((repo_dir / policy).read_text(encoding="utf-8"), policy))
        read = config((repo_dir / policy).read_text(encoding="utf-8"), policy, args.incident_label)
        holds = hold_rows(fetch_issues(args.repo, "--label", HOLD_LABEL), now)
        rows += holds
        closed = fetch_issues(args.repo, "--label", read.incident_label, state="closed")
        if closed:  # the postmortems are read only once an incident has closed
            texts = postmortem_texts(args.repo, branch)
            rows += postmortem_rows(closed, read.incident_label, texts, args.repo, now)
        if read.environments:
            rows += operations_rows(args.repo, repo_dir, read.environments, bool(holds), read.incident_label, now)

    rows += intake_rows(args.repo, args.bot_login, args.trusted_only)
    rows += active_rows(fetch_active(args.repo), now)
    rows += agent_rows(args.repo, repo_dir, now)
    rows += shipmill_rows(args.repo, repo_dir)
    for row in ordered(rows):
        print(json.dumps(row.json(), sort_keys=True) if args.json else row.text())
    return 1 if any(r.state in ACTION for r in rows) else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Refused as refused:
        sys.stderr.write(f"{refused}\n")
        raise
