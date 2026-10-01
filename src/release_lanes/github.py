"""What the bot asks of GitHub, behind a protocol so tests pass their own"""

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from release_lanes.errors import ReleaseError


@dataclass(frozen=True, slots=True)
class Milestone:
    open_issues: int
    closed_issues: int


class GitHub(Protocol):
    def open_issues(self, label: str) -> list[str]:
        """'#N title' for each open issue with the label"""
        ...

    def milestone(self, title: str) -> Milestone | None: ...

    def merge_commit(self, pr: int) -> str:
        """The commit a merged pull request landed as on the default branch"""
        ...

    def create_release(self, tag: str, title: str, notes: str, prerelease: bool) -> None: ...

    def dispatch(self, workflow: str, ref: str, tag: str) -> None: ...


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

    def dispatch(self, workflow: str, ref: str, tag: str) -> None:
        self._gh("workflow", "run", workflow, "--ref", ref, "-f", f"tag={tag}")
