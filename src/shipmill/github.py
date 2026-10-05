"""What the bot asks of GitHub, behind a protocol so tests pass their own"""

import datetime as dt
import json
import re
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol

from shipmill.errors import ReleaseError

PROPOSAL_LABEL = "shipmill-proposal"  # on every issue proposing a release or a deploy
OPEN_LIMIT = 100  # open issues read per label: proposals are one per lane and per environment
_LABEL_DESCRIPTIONS = {PROPOSAL_LABEL: "Opened by shipmill: a release or deploy waiting for a person"}


@dataclass(frozen=True, slots=True)
class Milestone:
    open_issues: int
    closed_issues: int


@dataclass(frozen=True, slots=True)
class Issue:
    number: int
    title: str
    body: str
    closed_at: dt.datetime | None = None  # None while open

    @property
    def closed(self) -> bool:
        return self.closed_at is not None


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


@dataclass(frozen=True, slots=True)
class WorkflowRun:
    """A run of a workflow: `status` is GitHub's (queued, in_progress, waiting, completed, ...)"""

    id: int
    status: str
    created_at: dt.datetime


class GitHub(Protocol):
    def open_issues(self, label: str) -> list[str]:
        """'#N title' for each open issue with the label"""
        ...

    def milestone(self, title: str) -> Milestone | None: ...

    def merge_commit(self, pr: int) -> str:
        """The commit a merged pull request landed as on the default branch"""
        ...

    def create_release(self, tag: str, title: str, notes: str, prerelease: bool) -> None: ...

    def dispatch(self, workflow: str, ref: str, tag: str, inputs: Mapping[str, str] | None = None) -> str | None:
        """Start the workflow on ref with -f tag=<tag>, and -f name=value for each of inputs;
        the run's URL when GitHub tells it"""
        ...

    def find_issue(self, marker: str) -> Issue | None:
        """The open issue whose body holds the marker, among the newest 500"""
        ...

    def create_issue(self, title: str, body: str, labels: Sequence[str] = ()) -> int:
        """Open an issue; a label the repository lacks is created first"""
        ...

    def update_issue(self, number: int, title: str, body: str, labels: Sequence[str] = ()) -> None:
        """Rewrite an issue and add the labels; a label the repository lacks is created first"""
        ...

    def comment_issue(self, number: int, body: str) -> None: ...

    def labelled_issues(self, label: str) -> list[Issue]:
        """The issues with the label, open and closed, newest first (the newest 100)"""
        ...

    def open_labelled_issues(self, label: str) -> list[Issue]:
        """The open issues with the label, newest first (the newest OPEN_LIMIT)"""
        ...

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

    def workflow_runs(self, workflow: str, ref: str) -> list[WorkflowRun]:
        """The workflow's runs on the branch or tag, newest first (the newest 100)"""
        ...


def _time(text: str) -> dt.datetime:
    when = dt.datetime.fromisoformat(text)
    if when.tzinfo is None:
        raise ReleaseError(f"GitHub sent a time without a zone: {text!r}")
    return when


class GhCli:
    """GitHub through the gh CLI, authenticated by GH_TOKEN in Actions"""

    def __init__(self, root: Path, gh: str = "gh") -> None:
        self.root = root
        self.gh = gh

    def _run(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run([self.gh, *args], capture_output=True, text=True, cwd=self.root)

    def _gh(self, *args: str) -> str:
        proc = self._run(*args)
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

    def dispatch(self, workflow: str, ref: str, tag: str, inputs: Mapping[str, str] | None = None) -> str | None:
        fields = [f"tag={tag}", *(f"{k}={v}" for k, v in (inputs or {}).items())]
        out = self._gh("workflow", "run", workflow, "--ref", ref, *(a for f in fields for a in ("-f", f)))
        found = re.search(r"https://\S+/actions/runs/\d+", out)  # gh prints the run's URL when GitHub returns it
        return found[0] if found else None

    def find_issue(self, marker: str) -> Issue | None:
        found = json.loads(
            self._gh("issue", "list", "--state", "open", "--json", "number,title,body", "--limit", "500")
        )
        hits = [Issue(int(i["number"]), i["title"], i["body"]) for i in found if marker in i["body"]]
        return min(hits, key=lambda i: i.number) if hits else None

    def _create_labels(self, labels: Sequence[str]) -> None:
        """Create each label the repository lacks, as `gh issue create --label` won't. One
        another run created meanwhile (GitHub's 422, "already exists") is there: success"""
        if not labels:
            return
        have = {
            str(label["name"]) for label in json.loads(self._gh("label", "list", "--json", "name", "--limit", "1000"))
        }
        for label in labels:
            if label not in have:
                described = _LABEL_DESCRIPTIONS.get(label, "Opened by shipmill; holds releases while open")
                proc = self._run("label", "create", label, "--description", described)
                if proc.returncode != 0 and "already exists" not in proc.stderr:
                    raise ReleaseError(f"gh label create {label} failed: {proc.stderr.strip()}")

    def create_issue(self, title: str, body: str, labels: Sequence[str] = ()) -> int:
        self._create_labels(labels)
        args = [a for label in labels for a in ("--label", label)]
        url = self._gh("issue", "create", "--title", title, "--body", body, *args).strip()
        number = url.rsplit("/", 1)[-1]
        if not number.isdigit():
            raise ReleaseError(f"gh issue create printed {url!r}, not an issue URL")
        return int(number)

    def update_issue(self, number: int, title: str, body: str, labels: Sequence[str] = ()) -> None:
        self._create_labels(labels)
        args = [a for label in labels for a in ("--add-label", label)]
        self._gh("issue", "edit", str(number), "--title", title, "--body", body, *args)

    def close_issue(self, number: int, comment: str) -> None:
        self._gh("issue", "close", str(number), "--comment", comment)

    def comment_issue(self, number: int, body: str) -> None:
        self._gh("issue", "comment", str(number), "--body", body)

    def labelled_issues(self, label: str) -> list[Issue]:
        found = json.loads(
            self._gh(
                "issue", "list", "--label", label, "--state", "all", "--json", "number,title,body,state,closedAt",
                "--limit", "100",
            )
        )  # fmt: skip
        issues = [
            Issue(int(i["number"]), i["title"], i["body"], None if i["state"] == "OPEN" else _time(i["closedAt"]))
            for i in found
        ]
        return sorted(issues, key=lambda i: i.number, reverse=True)

    def open_labelled_issues(self, label: str) -> list[Issue]:
        found = json.loads(
            self._gh(
                "issue", "list", "--label", label, "--state", "open", "--json", "number,title,body",
                "--limit", str(OPEN_LIMIT),
            )
        )  # fmt: skip
        issues = [Issue(int(i["number"]), i["title"], i["body"]) for i in found]
        return sorted(issues, key=lambda i: i.number, reverse=True)

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

    def workflow_runs(self, workflow: str, ref: str) -> list[WorkflowRun]:
        found = self._api(
            "-X", "GET", f"repos/{{owner}}/{{repo}}/actions/workflows/{workflow}/runs", "-f", f"branch={ref}",
            "-f", "per_page=100",
        )  # fmt: skip
        runs = [WorkflowRun(int(r["id"]), str(r["status"]), _time(r["created_at"])) for r in found["workflow_runs"]]
        return sorted(runs, key=lambda r: r.id, reverse=True)
