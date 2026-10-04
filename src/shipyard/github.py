"""What the bot asks of GitHub, behind a protocol so tests pass their own"""

import datetime as dt
import json
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol

from shipyard.errors import ReleaseError


@dataclass(frozen=True, slots=True)
class Milestone:
    open_issues: int
    closed_issues: int


@dataclass(frozen=True, slots=True)
class Issue:
    number: int
    title: str
    body: str


class DeploymentState(StrEnum):
    """A deployment status's state, as GitHub's REST API names it"""

    ERROR = "error"
    FAILURE = "failure"
    INACTIVE = "inactive"
    IN_PROGRESS = "in_progress"
    QUEUED = "queued"
    PENDING = "pending"
    SUCCESS = "success"
    WAITING = "waiting"  # an Actions job held by the environment's protection rules


@dataclass(frozen=True, slots=True)
class Deployment:
    """A GitHub deployment: what ran where. `ref` is the branch, tag, or sha it was made from"""

    id: int
    ref: str
    sha: str
    created_at: dt.datetime


@dataclass(frozen=True, slots=True)
class DeploymentStatus:
    id: int  # increases with each status, so it orders them where created_at ties
    state: DeploymentState
    created_at: dt.datetime
    description: str


class GitHub(Protocol):
    def open_issues(self, label: str) -> list[str]:
        """'#N title' for each open issue with the label"""
        ...

    def milestone(self, title: str) -> Milestone | None: ...

    def merge_commit(self, pr: int) -> str:
        """The commit a merged pull request landed as on the default branch"""
        ...

    def create_release(self, tag: str, title: str, notes: str, prerelease: bool) -> None: ...

    def dispatch(self, workflow: str, ref: str, tag: str, inputs: Mapping[str, str] | None = None) -> None:
        """Start the workflow on ref with -f tag=<tag>, and -f name=value for each of inputs"""
        ...

    def find_issue(self, marker: str) -> Issue | None:
        """The open issue whose body holds the marker"""
        ...

    def create_issue(self, title: str, body: str) -> int: ...

    def update_issue(self, number: int, title: str, body: str) -> None: ...

    def close_issue(self, number: int, comment: str) -> None: ...

    def deployments(self, environment: str) -> list[Deployment]:
        """The environment's deployments, newest first (the newest 100)"""
        ...

    def deployment_statuses(self, deployment: int) -> list[DeploymentStatus]:
        """The deployment's statuses, oldest first (the newest 100)"""
        ...

    def create_deployment_status(self, deployment: int, state: DeploymentState, description: str) -> None:
        """Add a status, leaving the environment's other deployments as they are"""
        ...


def _time(text: str) -> dt.datetime:
    when = dt.datetime.fromisoformat(text)
    if when.tzinfo is None:
        raise ReleaseError(f"GitHub sent a time without a zone: {text!r}")
    return when


class GhCli:
    """GitHub through the gh CLI, authenticated by GH_TOKEN in Actions"""

    def __init__(self, root: Path) -> None:
        self.root = root

    def _gh(self, *args: str) -> str:
        proc = subprocess.run(["gh", *args], capture_output=True, text=True, cwd=self.root)
        if proc.returncode != 0:
            raise ReleaseError(f"gh {' '.join(args[:3])} failed: {proc.stderr.strip()}")
        return proc.stdout

    def open_issues(self, label: str) -> list[str]:
        found = json.loads(
            self._gh(
                "issue",
                "list",
                "--label",
                label,
                "--state",
                "open",
                "--json",
                "number,title",
                "--limit",
                "50",
            )
        )
        return [f"#{i['number']} {i['title']}" for i in found]

    def milestone(self, title: str) -> Milestone | None:
        found = json.loads(self._gh("api", "repos/{owner}/{repo}/milestones?state=all&per_page=100"))
        for m in found:
            if m["title"] == title:
                return Milestone(int(m["open_issues"]), int(m["closed_issues"]))
        return None

    def merge_commit(self, pr: int) -> str:
        found = json.loads(self._gh("pr", "view", str(pr), "--json", "state,mergeCommit"))
        if found["state"] != "MERGED" or not found.get("mergeCommit"):
            raise ReleaseError(f"#{pr} is not merged ({found['state']}): a hotfix takes merged pull requests")
        return str(found["mergeCommit"]["oid"])

    def create_release(self, tag: str, title: str, notes: str, prerelease: bool) -> None:
        args = ["release", "create", tag, "--verify-tag", "--title", title, "--notes", notes]
        if prerelease:
            args.append("--prerelease")
        self._gh(*args)

    def dispatch(self, workflow: str, ref: str, tag: str, inputs: Mapping[str, str] | None = None) -> None:
        fields = [f"tag={tag}", *(f"{k}={v}" for k, v in (inputs or {}).items())]
        self._gh("workflow", "run", workflow, "--ref", ref, *(a for f in fields for a in ("-f", f)))

    def find_issue(self, marker: str) -> Issue | None:
        found = json.loads(
            self._gh("issue", "list", "--state", "open", "--json", "number,title,body", "--limit", "500")
        )
        hits = [Issue(int(i["number"]), i["title"], i["body"]) for i in found if marker in i["body"]]
        return min(hits, key=lambda i: i.number) if hits else None

    def create_issue(self, title: str, body: str) -> int:
        url = self._gh("issue", "create", "--title", title, "--body", body).strip()
        number = url.rsplit("/", 1)[-1]
        if not number.isdigit():
            raise ReleaseError(f"gh issue create printed {url!r}, not an issue URL")
        return int(number)

    def update_issue(self, number: int, title: str, body: str) -> None:
        self._gh("issue", "edit", str(number), "--title", title, "--body", body)

    def close_issue(self, number: int, comment: str) -> None:
        self._gh("issue", "close", str(number), "--comment", comment)

    def _api(self, *args: str) -> Any:
        return json.loads(self._gh("api", *args))

    def deployments(self, environment: str) -> list[Deployment]:
        found = self._api(
            "-X", "GET", "repos/{owner}/{repo}/deployments", "-f", f"environment={environment}", "-f", "per_page=100"
        )
        deployments = [Deployment(int(d["id"]), str(d["ref"]), str(d["sha"]), _time(d["created_at"])) for d in found]
        return sorted(deployments, key=lambda d: d.id, reverse=True)

    def deployment_statuses(self, deployment: int) -> list[DeploymentStatus]:
        found = self._api(
            "-X", "GET", f"repos/{{owner}}/{{repo}}/deployments/{deployment}/statuses", "-f", "per_page=100"
        )
        statuses = [
            DeploymentStatus(int(s["id"]), DeploymentState(s["state"]), _time(s["created_at"]), s["description"] or "")
            for s in found
        ]
        return sorted(statuses, key=lambda s: s.id)

    def create_deployment_status(self, deployment: int, state: DeploymentState, description: str) -> None:
        self._gh(
            "api",
            "-X",
            "POST",
            f"repos/{{owner}}/{{repo}}/deployments/{deployment}/statuses",
            "-f",
            f"state={state}",
            "-f",
            f"description={description}",
            "-F",
            "auto_inactive=false",
        )
