#!/usr/bin/env python3
"""Report what a repo's issue-to-release pipeline still owes: the release bot, the
recent releases, and the issue intake.

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

Intake (github-issue-triage's triage_state.py):
  ISSUES          issues needing triage action, counted by state
  PRS_OPEN        open non-draft pull requests (reported, never an action by itself)

Exit 0 when nothing needs action, 1 when any BOT_FAILED, BOT_STALLED, NOT_PUBLISHED,
UNANNOUNCED, or ISSUES row is present, 2 on bad input or a git, gh, or uvx failure.
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
from dataclasses import dataclass
from pathlib import Path

SKILLS = Path(__file__).resolve().parents[2]
SHIPPED = SKILLS / "github-pr-triage" / "scripts" / "shipped.py"
TRIAGE_STATE = SKILLS / "github-issue-triage" / "scripts" / "triage_state.py"
POLICIES = (Path(".github/shipyard.toml"), Path(".github/release-policy.toml"))  # the config, then its alias
VERSION_TAG = re.compile(r"^v\d+\.\d+")  # skips moving major tags such as v0
ACTION = {"BOT_FAILED", "BOT_STALLED", "NOT_PUBLISHED", "UNANNOUNCED", "ISSUES"}
ACTIVE = {"queued", "in_progress", "waiting", "pending", "requested"}


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

    rows += intake_rows(args.repo)
    for row in rows:
        print(json.dumps(row.__dict__, sort_keys=True) if args.json else row.text())
    return 1 if any(r.state in ACTION for r in rows) else 0


if __name__ == "__main__":
    sys.exit(main())
