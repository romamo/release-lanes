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
UPGRADE_LABEL = "shipmill-upgrade"  # on every issue proposing a config upgrade (spec 014)
# on an upgrade issue whose maintainer answered "not now": asked again only once a newer
# shipmill release changes the upgrade (S-014-14)
UPGRADE_LATER_LABEL = "shipmill-upgrade-later"
OPEN_LIMIT = 100  # open issues read per label: proposals are one per lane and per environment
PULL_LIMIT = 1000  # open pull requests read at once; gh's own default is 30
MERGED_PER_BRANCH = 20  # merged pull requests read per head branch name, the newest
HISTORY_LIMIT = 100  # a merged pull request's commits, and its force-pushes, read: the last ones
_MERGED_FRAGMENT = (
    "fragment merged on PullRequestConnection { nodes { number headRefName baseRefName headRefOid "
    f"commits(last: {HISTORY_LIMIT}) {{ nodes {{ commit {{ oid }} }} }} "
    f"timelineItems(itemTypes: [HEAD_REF_FORCE_PUSHED_EVENT], last: {HISTORY_LIMIT}) "
    "{ nodes { ... on HeadRefForcePushedEvent { beforeCommit { oid } afterCommit { oid } } } } } }"
)
_LABEL_DESCRIPTIONS = {
    PROPOSAL_LABEL: "Opened by shipmill: a release or deploy waiting for a person",
    UPGRADE_LABEL: "Opened by shipmill: a config upgrade waiting for the maintainer's decision",
}
# the statuses of a run not finished yet, each listed with its own query: GitHub filters runs
# by one status at a time
ACTIVE_STATUSES = ("queued", "in_progress", "waiting", "pending", "requested")


class Forbidden(ReleaseError):
    """GitHub refused the call for the token's permissions (HTTP 403, not a rate limit)"""


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
    labels: tuple[str, ...] = ()  # read by labelled_issues; empty where a call doesn't read them

    @property
    def closed(self) -> bool:
        return self.closed_at is not None


@dataclass(frozen=True, slots=True)
class PullRequest:
    number: int
    head: str  # the head branch's name


@dataclass(frozen=True, slots=True)
class MergedPull:
    """A merged pull request and every commit known to have been its head: its head when
    merged, its commits, and each force-push's commit before and after"""

    number: int
    head: str  # the head branch's name
    heads: frozenset[str]  # full commit SHAs
    base: str  # the branch it merged into: only a merge into the default branch landed


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

    def remove_label(self, number: int, label: str) -> None:
        """Take the label off the issue"""
        ...

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

    def open_pull_requests(self) -> list[PullRequest]:
        """The open pull requests, by number (the newest PULL_LIMIT)"""
        ...

    def merged_pull_requests(self, branches: Sequence[str]) -> list[MergedPull]:
        """The merged pull requests whose head branch is named one of branches, by number, with
        their head history (the newest MERGED_PER_BRANCH per name, the last HISTORY_LIMIT
        commits and force-pushes of each); one request for all the names, none for no name"""
        ...

    def active_runs(self, workflow: str) -> list[WorkflowRun]:
        """The workflow's runs on any branch that haven't finished (ACTIVE_STATUSES), newest
        first (the newest 100 of each status); Forbidden when the token can't read Actions"""
        ...


def _time(text: str) -> dt.datetime:
    when = dt.datetime.fromisoformat(text)
    if when.tzinfo is None:
        raise ReleaseError(f"GitHub sent a time without a zone: {text!r}")
    return when


def labelled_command(label: str) -> tuple[str, ...]:
    """gh's arguments that list the issues with the label, open and closed, the newest 100"""
    fields = "number,title,body,state,closedAt,labels"
    return ("issue", "list", "--label", label, "--state", "all", "--json", fields, "--limit", "100")


def parse_labelled(text: str) -> list[Issue]:
    """labelled_command's output, newest first"""
    found = json.loads(text)
    issues = [
        Issue(
            int(i["number"]),
            i["title"],
            i["body"],
            None if i["state"] == "OPEN" else _time(i["closedAt"]),
            tuple(str(label["name"]) for label in i["labels"]),
        )
        for i in found
    ]
    return sorted(issues, key=lambda i: i.number, reverse=True)


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
            error = proc.stderr.strip()
            message = f"gh {' '.join(args[:3])} failed: {error}"
            # gh prints "<GitHub's message> (HTTP 403)"; a 403 for a rate limit isn't a permission
            if re.search(r"\bHTTP 403\b", error) and "rate limit" not in error.lower():
                raise Forbidden(message)
            raise ReleaseError(message)
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

    def remove_label(self, number: int, label: str) -> None:
        self._gh("issue", "edit", str(number), "--remove-label", label)

    def labelled_issues(self, label: str) -> list[Issue]:
        return parse_labelled(self._gh(*labelled_command(label)))

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

    def active_runs(self, workflow: str) -> list[WorkflowRun]:
        runs = {}
        # in_progress is asked again last: a run approved after the first in_progress query and
        # before the waiting one would otherwise be in neither list
        for status in (*ACTIVE_STATUSES, "in_progress"):
            found = self._api(
                "-X", "GET", f"repos/{{owner}}/{{repo}}/actions/workflows/{workflow}/runs", "-f", f"status={status}",
                "-f", "per_page=100",
            )  # fmt: skip
            for r in found["workflow_runs"]:
                runs[int(r["id"])] = WorkflowRun(int(r["id"]), str(r["status"]), _time(r["created_at"]))
        return sorted(runs.values(), key=lambda r: r.id, reverse=True)

    def open_pull_requests(self) -> list[PullRequest]:
        found = json.loads(
            self._gh(
                "pr", "list", "--state", "open", "--json", "number,headRefName", "--limit", str(PULL_LIMIT),
            )
        )  # fmt: skip
        if not isinstance(found, list):
            raise ReleaseError(f"gh pr list printed {type(found).__name__}, not a JSON array")
        pulls = []
        for pr in found:
            if (
                not isinstance(pr, dict)
                or not isinstance(pr.get("number"), int)
                or not isinstance(pr.get("headRefName"), str)
            ):
                raise ReleaseError(f"gh pr list printed {pr!r}: a pull request needs a number and a headRefName")
            pulls.append(PullRequest(pr["number"], pr["headRefName"]))
        return sorted(pulls, key=lambda p: p.number)

    def merged_pull_requests(self, branches: Sequence[str]) -> list[MergedPull]:
        names = sorted(set(branches))
        if not names:
            return []
        params = "".join(f", $b{i}: String!" for i in range(len(names)))
        aliases = " ".join(
            f"b{i}: pullRequests(headRefName: $b{i}, states: MERGED, last: {MERGED_PER_BRANCH}) {{ ...merged }}"
            for i in range(len(names))
        )
        query = (
            f"query($owner: String!, $name: String!{params}) "
            f"{{ repository(owner: $owner, name: $name) {{ {aliases} }} }} {_MERGED_FRAGMENT}"
        )
        variables = [a for i, n in enumerate(names) for a in ("-f", f"b{i}={n}")]
        found = json.loads(
            self._gh("api", "graphql", "-F", "owner={owner}", "-F", "name={repo}", "-f", f"query={query}", *variables)
        )
        return parse_merged_pulls(found, len(names))


def parse_merged_pulls(found: Any, aliases: int) -> list[MergedPull]:
    """The `b<i>` connections of merged_pull_requests' GraphQL answer; anything else is refused,
    since a pull request misread could prove a branch landed that didn't"""

    def refuse(what: str) -> ReleaseError:
        return ReleaseError(f"gh api graphql printed {what} for the merged pull requests: {str(found)[:300]}")

    if not isinstance(found, dict) or found.get("errors"):
        raise refuse("errors or no object")
    data = found.get("data")
    repository = data.get("repository") if isinstance(data, dict) else None
    if not isinstance(repository, dict):
        raise refuse("no repository")
    pulls: dict[int, MergedPull] = {}
    for i in range(aliases):
        connection = repository.get(f"b{i}")
        if not isinstance(connection, dict) or not isinstance(connection.get("nodes"), list):
            raise refuse(f"no b{i} connection")
        for node in connection["nodes"]:
            pull = _merged_pull(node)
            if pull is None:
                raise refuse(
                    "a pull request without a number, headRefName, baseRefName, headRefOid, commits, or timeline"
                )
            pulls[pull.number] = pull
    return [pulls[n] for n in sorted(pulls)]


def _oid(holder: Any, key: str) -> str | None:
    """holder[key]["oid"]; None when holder[key] is null (a commit GitHub no longer has)"""
    if not isinstance(holder, dict):
        raise ReleaseError(f"gh api graphql printed {holder!r} where an object holds {key}")
    value = holder.get(key)
    if value is None:
        return None
    if not isinstance(value, dict) or not isinstance(value.get("oid"), str) or not value["oid"]:
        raise ReleaseError(f"gh api graphql printed {holder!r}: {key} needs an oid")
    return str(value["oid"])


def _merged_pull(node: Any) -> MergedPull | None:
    if not isinstance(node, dict):
        return None
    number, head, oid = node.get("number"), node.get("headRefName"), node.get("headRefOid")
    base, commits, timeline = node.get("baseRefName"), node.get("commits"), node.get("timelineItems")
    if (
        not isinstance(number, int)
        or not isinstance(head, str)
        or not isinstance(base, str)
        or not isinstance(oid, str)
        or not isinstance(commits, dict)
        or not isinstance(commits.get("nodes"), list)
        or not isinstance(timeline, dict)
        or not isinstance(timeline.get("nodes"), list)
    ):
        return None
    heads = {oid}
    for c in commits["nodes"]:
        sha = _oid(c, "commit")
        if sha is None:
            raise ReleaseError(f"gh api graphql printed {c!r} for #{number}: a commit needs an oid")
        heads.add(sha)
    for event in timeline["nodes"]:
        if not isinstance(event, dict):
            raise ReleaseError(f"gh api graphql printed {event!r} for #{number}: not a force-push event")
        for key in ("beforeCommit", "afterCommit"):
            sha = _oid(event, key)
            if sha is not None:
                heads.add(sha)
    return MergedPull(number, head, frozenset(heads), base)
