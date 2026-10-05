import datetime as dt
import subprocess
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path

import pytest

from shipmill.autonomy import HOLD_LABEL
from shipmill.config import CONFIG_PATH, config_path
from shipmill.github import Deployment, DeploymentState, DeploymentStatus, Issue, Milestone, WorkflowRun
from shipmill.gitrepo import Git
from shipmill.policy import Policy

POLICY = """\
name = "demo"
mode = "release"
branch = "main"
version_files = "pyproject"

[changelog]
style = "keep-a-changelog"

[bump]
from = "headings"
major = ["Breaking"]
minor = ["Added", "Changed"]
patch = ["Fixed"]

[lanes.dev]
quiet_minutes = 30

[lanes.rc]
schedule = ["daily 07:00 UTC"]
github_release = true

[lanes.stable]
promote_from = "rc"
min_soak_days = 3
schedule = ["Mon 07:00 UTC"]
github_release = true
dispatch = ["publish.yml"]

[lanes.hotfix]
github_release = true

[[version_lines]]
file = "README.md"
pattern = 'demo \\S+$'
replace = "demo {version}"
"""

CHANGELOG = """\
# Changelog

## [Unreleased]

## [1.0.0] - 2026-09-01

### Added

- The first release

[Unreleased]: https://github.com/o/demo/compare/v1.0.0...HEAD
[1.0.0]: https://github.com/o/demo/releases/tag/v1.0.0
"""

PYPROJECT = """\
[project]
name = "demo"
version = "1.0.0"
"""

UV_LOCK = """\
version = 1

[[package]]
name = "demo"
version = "1.0.0"
source = { editable = "." }
"""

# A fixed timeline: the fixture's 1.0.0 is a month before T0, a Monday
T0 = dt.datetime(2026, 10, 5, tzinfo=dt.UTC)


def at_day(days: float, hour: int = 8) -> dt.datetime:
    """days after T0 at hour:00 UTC"""
    return T0 + dt.timedelta(days=days, hours=hour)


@dataclass
class FakeGitHub:
    blockers: list[str] = field(default_factory=list)
    holds: list[str] = field(default_factory=list)  # open shipmill-hold issues, '#N title'
    issues: dict[int, Issue] = field(default_factory=dict)  # issues shipmill opened, by number
    edits: int = 0  # update_issue calls
    labels: dict[int, tuple[str, ...]] = field(default_factory=dict)  # the labels of issues shipmill opened
    comments: dict[int, list[str]] = field(default_factory=dict)  # by issue, oldest first
    labels_read: list[str] = field(default_factory=list)
    milestones: dict[str, Milestone] = field(default_factory=dict)
    merges: dict[int, str] = field(default_factory=dict)
    releases: list[tuple[str, str, str, bool]] = field(default_factory=list)
    dispatched: list[tuple[str, str, str, dict[str, str]]] = field(default_factory=list)
    closed: dict[int, str] = field(default_factory=dict)  # issues closed, with their comment
    closed_issues: dict[int, Issue] = field(default_factory=dict)
    now: dt.datetime = T0  # when a status written now is created
    envs: dict[str, list[Deployment]] = field(default_factory=dict)  # deployments by environment, newest first
    statuses: dict[int, list[DeploymentStatus]] = field(default_factory=dict)  # by deployment, oldest first
    status_writes: list[tuple[int, DeploymentState, str]] = field(default_factory=list)
    runs: list[tuple[str, str, WorkflowRun]] = field(default_factory=list)  # (workflow, ref, run), as dispatched
    scan_limit: int = 500  # the newest open issues find_issue reads, as gh lists them

    def deploy(self, environment: str, ref: str, at: dt.datetime, *states: DeploymentState) -> int:
        """Record a deployment of ref, with its statuses one minute apart from at; its id"""
        number = 1000 + sum(len(d) for d in self.envs.values())
        self.envs.setdefault(environment, []).insert(0, Deployment(number, ref, "0" * 40, at))
        self.statuses[number] = []
        for i, state in enumerate(states):
            self._status(number, state, "", at + dt.timedelta(minutes=i))
        return number

    def _status(self, deployment: int, state: DeploymentState, description: str, at: dt.datetime) -> None:
        number = 1 + sum(len(s) for s in self.statuses.values())
        self.statuses[deployment].append(DeploymentStatus(number, state, at, description))

    def open_issues(self, label: str) -> list[str]:
        self.labels_read.append(label)
        if label == HOLD_LABEL:
            return list(self.holds)
        if label == "release-blocker":
            return list(self.blockers)
        return [f"#{n} {i.title}" for n, i in sorted(self.issues.items()) if label in self.labels.get(n, ())]

    def milestone(self, title: str) -> Milestone | None:
        return self.milestones.get(title)

    def merge_commit(self, pr: int) -> str:
        return self.merges[pr]

    def create_release(self, tag: str, title: str, notes: str, prerelease: bool) -> None:
        self.releases.append((tag, title, notes, prerelease))

    def dispatch(self, workflow: str, ref: str, tag: str, inputs: Mapping[str, str] | None = None) -> str | None:
        self.dispatched.append((workflow, ref, tag, dict(inputs or {})))
        self.runs.append((workflow, ref, WorkflowRun(len(self.runs) + 1, "queued", self.now)))
        return f"https://github.com/o/demo/actions/runs/{len(self.runs)}"

    def workflow_runs(self, workflow: str, ref: str) -> list[WorkflowRun]:
        return [run for w, r, run in reversed(self.runs) if (w, r) == (workflow, ref)]

    def find_issue(self, marker: str) -> Issue | None:
        newest = sorted(self.issues.values(), key=lambda i: -i.number)[: self.scan_limit]
        hits = [i for i in newest if marker in i.body]
        return min(hits, key=lambda i: i.number) if hits else None

    def create_issue(self, title: str, body: str, labels: Sequence[str] = ()) -> int:
        number = 100 + len(self.issues) + len(self.closed)
        self.issues[number] = Issue(number, title, body)
        self.labels[number] = tuple(labels)
        return number

    def comment_issue(self, number: int, body: str) -> None:
        assert number in self.issues, f"#{number} is not open"
        self.comments.setdefault(number, []).append(body)

    def labelled_issues(self, label: str) -> list[Issue]:
        found = [*self.issues.values(), *self.closed_issues.values()]
        return sorted((i for i in found if label in self.labels.get(i.number, ())), key=lambda i: -i.number)

    def open_labelled_issues(self, label: str) -> list[Issue]:
        return sorted(
            (i for i in self.issues.values() if label in self.labels.get(i.number, ())), key=lambda i: -i.number
        )

    def update_issue(self, number: int, title: str, body: str, labels: Sequence[str] = ()) -> None:
        self.issues[number] = Issue(number, title, body)
        have = self.labels.get(number, ())
        self.labels[number] = (*have, *(label for label in labels if label not in have))
        self.edits += 1

    def close_issue(self, number: int, comment: str) -> None:
        self.closed_issues[number] = replace(self.issues.pop(number), closed_at=self.now)
        self.closed[number] = comment

    def deployments(self, environment: str) -> list[Deployment]:
        return list(self.envs.get(environment, []))

    def deployment_statuses(self, deployment: int) -> list[DeploymentStatus]:
        return list(self.statuses[deployment])

    def create_deployment_status(self, deployment: int, state: DeploymentState, description: str) -> None:
        self.status_writes.append((deployment, state, description))
        self._status(deployment, state, description, self.now)


@dataclass
class Repo:
    root: Path
    git: Git
    github: FakeGitHub

    @property
    def policy(self) -> Policy:
        return Policy.load(config_path(self.root))

    @property
    def policy_file(self) -> str:
        """The policy file shipmill reads here, relative to the root"""
        return str(config_path(self.root).relative_to(self.root))

    def at(self, when: dt.datetime) -> None:
        """Make the next commits and tags carry this time"""
        stamp = when.isoformat()
        self.git.env = {"GIT_COMMITTER_DATE": stamp, "GIT_AUTHOR_DATE": stamp}

    def write(self, path: str, text: str) -> None:
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")

    def read(self, path: str) -> str:
        return (self.root / path).read_text(encoding="utf-8")

    def merge(self, pr: int, heading: str, entry: str, path: str = "", text: str = "") -> str:
        """Land a pull request on main: an Unreleased entry plus an optional file change"""
        self.git.run("checkout", "-q", "main")
        self.git.run("reset", "-q", "--hard", "origin/main")
        changelog = self.read("CHANGELOG.md")
        marker = "## [Unreleased]\n\n"
        head, rest = changelog.split(marker, 1)
        block = f"### {heading}\n\n"
        line = f"- {entry} (#{pr})\n"
        rest = block + line + (rest[len(block) :] if rest.startswith(block) else "\n" + rest)
        self.write("CHANGELOG.md", head + marker + rest)
        if path:
            self.write(path, text)
        self.git.run("add", "-A")
        self.git.run("commit", "-q", "-m", f"Merge #{pr}")
        self.git.run("push", "-q", "origin", "main")
        sha = self.git.sha()
        self.github.merges[pr] = sha
        return sha

    def main_text(self, path: str) -> str:
        self.git.fetch("+refs/heads/main:refs/remotes/origin/main")
        text = self.git.show("origin/main", path)
        assert text is not None
        return text


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path: Path) -> Iterator[Repo]:
    origin = tmp_path / "origin.git"
    _git("init", "-q", "--bare", "-b", "main", str(origin), cwd=tmp_path)
    root = tmp_path / "work"
    _git("clone", "-q", str(origin), str(root), cwd=tmp_path)
    git = Git(root, "bot", "bot@example.com")
    r = Repo(root, git, FakeGitHub())
    r.at(T0 - dt.timedelta(days=30))
    r.write(str(CONFIG_PATH), POLICY)
    r.write("CHANGELOG.md", CHANGELOG)
    r.write("pyproject.toml", PYPROJECT)
    r.write("uv.lock", UV_LOCK)
    r.write("README.md", "# demo\n\ndemo 1.0.0\n")
    r.write("src/app.py", "VALUE = 1\n")
    git.run("checkout", "-q", "-b", "main")
    git.run("add", "-A")
    git.run("commit", "-q", "-m", "Release 1.0.0")
    git.run("tag", "-a", "v1.0.0", "-m", "demo 1.0.0")
    git.run("push", "-q", "origin", "main", "v1.0.0")
    yield r
