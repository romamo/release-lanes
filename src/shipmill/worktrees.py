"""Which worktrees of a repository provably landed, and why the others stay (spec S-002).

Each worktree gets one verdict, REMOVABLE or KEPT; a KEPT one carries the first check it
failed, in the spec's order. Judging reads git, `claude agents --json`, and one `gh pr list`,
and changes nothing but the fetched `origin/<default>`.
"""

import datetime as dt
import enum
import json
import os
import subprocess
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from shipmill.errors import ReleaseError
from shipmill.github import GitHub, PullRequest
from shipmill.gitrepo import REMOTE, Git

FLOOR = dt.timedelta(hours=24)  # a younger worktree may belong to a session that hasn't committed yet
WINDOW = 500  # non-merge commits of origin/<default> searched for a patch id, as landed.py's --depth
EXCLUDE = "CHANGELOG.md"  # its conflicts are often resolved by hand, so a second patch id leaves it out
LOOSE = ("--", ":(top)", f":(top,exclude){EXCLUDE}")  # from the top, as --repo may name a subdirectory
DIFF = ("--no-color", "--no-ext-diff")
HEADS = "refs/heads/"


class Verdict(enum.StrEnum):
    REMOVABLE = "REMOVABLE"
    KEPT = "KEPT"


@dataclass(frozen=True, slots=True)
class Worktree:
    """One record of `git worktree list --porcelain -z`"""

    path: Path
    head: str | None  # None for a bare repository
    branch: str | None  # without refs/heads/; None when detached or bare
    bare: bool = False
    locked: str | None = None  # git's lock reason, "" when it gave none; None when not locked
    prunable: bool = False


@dataclass(frozen=True, slots=True)
class LiveSession:
    """A Claude Code session `claude agents --json` lists"""

    name: str
    cwd: Path


@dataclass(frozen=True, slots=True)
class Judged:
    """A worktree and its verdict; reason is None for a REMOVABLE one"""

    worktree: Worktree
    path: str  # relative to the main checkout
    created: dt.datetime | None  # None for the main checkout, which git keeps no creation record for
    age_hours: int | None
    verdict: Verdict
    reason: str | None

    @property
    def age(self) -> str:
        if self.age_hours is None:
            return "-"
        return f"{self.age_hours}h" if self.age_hours < 48 else f"{self.age_hours // 24}d"

    def record(self) -> dict[str, object]:
        return {
            "path": self.path,
            "branch": self.worktree.branch,
            "head": self.worktree.head,
            "verdict": self.verdict.value,
            "reason": self.reason,
            "created": None if self.created is None else self.created.isoformat(timespec="seconds"),
            "age_hours": self.age_hours,
        }


def parse_worktrees(text: str) -> list[Worktree]:
    """`git worktree list --porcelain -z`: attributes end in NUL, and an empty one ends a record"""
    records: list[list[str]] = [[]]
    for attr in text.split("\0"):
        if attr:
            records[-1].append(attr)
        elif records[-1]:
            records.append([])
    found = [_worktree(r) for r in records if r]
    if not found:
        raise ReleaseError("git worktree list printed no worktree")
    return found


def _worktree(attrs: Sequence[str]) -> Worktree:
    first, *rest = attrs
    key, _, path = first.partition(" ")
    if key != "worktree" or not path:
        raise ReleaseError(f"git worktree list printed {first!r} where a record starts with `worktree <path>`")
    head: str | None = None
    branch: str | None = None
    bare = detached = prunable = False
    locked: str | None = None
    for attr in rest:
        key, _, value = attr.partition(" ")
        if key == "HEAD":
            head = value
        elif key == "branch" and value.startswith(HEADS):
            branch = value[len(HEADS) :]
        elif key == "detached":
            detached = True
        elif key == "bare":
            bare = True
        elif key == "locked":
            locked = value
        elif key == "prunable":
            prunable = True
        else:
            raise ReleaseError(f"git worktree list printed {attr!r} for {path}, which shipmill doesn't know")
    if [branch is not None, detached, bare].count(True) != 1 or (head is None) != bare:
        raise ReleaseError(f"git worktree list printed {list(attrs)!r}: not one of bare, detached, or on a branch")
    return Worktree(Path(path), head, branch, bare, locked, prunable)


def parse_live_sessions(text: str) -> list[LiveSession]:
    """Every session's cwd from `claude agents --json`; anything but an array of rows with a
    string cwd is refused, since a session missed could lose its work"""
    try:
        rows = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ReleaseError(f"claude agents --json printed no JSON: {exc}") from None
    if not isinstance(rows, list):
        raise ReleaseError(f"claude agents --json printed a {type(rows).__name__}, not a JSON array")
    sessions = []
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("cwd"), str) or not row["cwd"]:
            raise ReleaseError(f"claude agents --json printed a row without a string cwd: {str(row)[:200]}")
        sessions.append(LiveSession(_session_name(row), Path(row["cwd"])))
    return sessions


def _session_name(row: dict[str, Any]) -> str:
    for key in ("name", "id", "sessionId", "pid"):
        value = row.get(key)
        if isinstance(value, (str, int)) and not isinstance(value, bool) and str(value):
            return str(value)
    return "(unnamed)"


class Sessions(Protocol):
    def live(self) -> list[LiveSession]:
        """Every active Claude Code session on this machine, interactive and background"""
        ...


class ClaudeSessions:
    """`claude agents --json`, without --cwd, which would list background sessions only"""

    def __init__(self, root: Path, claude: str = "claude") -> None:
        self.root = root
        self.claude = claude

    def live(self) -> list[LiveSession]:
        cmd = [self.claude, "agents", "--json"]
        try:
            proc = subprocess.run(cmd, cwd=self.root, capture_output=True, text=True, check=False)
        except FileNotFoundError:
            raise ReleaseError(
                f"{self.claude} is not on PATH: shipmill worktrees needs `claude agents --json` to see live sessions"
            ) from None
        if proc.returncode != 0:
            err = (proc.stderr or proc.stdout).strip()[:500]
            raise ReleaseError(f"claude agents --json failed with exit {proc.returncode}: {err}")
        return parse_live_sessions(proc.stdout)


class Landing:
    """Did a branch land on origin/<default>? landed.py's rules (ancestry, patch id, patch id
    without CHANGELOG.md) per commit, then the whole branch diff as one squashed commit"""

    def __init__(self, git: Git, onto: str) -> None:
        self.git = git
        self.onto = onto
        self._window: tuple[frozenset[str], frozenset[str]] | None = None

    def _ids(self, patch: bytes) -> list[str]:
        out = self.git.run("patch-id", "--stable", stdin=patch)
        return [line.split()[0] for line in out.splitlines() if line.strip()]

    def _one(self, patch: bytes) -> str | None:
        ids = self._ids(patch)
        return ids[0] if ids else None

    def window(self) -> tuple[frozenset[str], frozenset[str]]:
        """The patch ids of the newest WINDOW non-merge commits of onto: exact, and without CHANGELOG.md"""
        if self._window is None:
            log = ("log", "-p", *DIFF, "--no-merges", f"-n{WINDOW}", self.onto)
            exact = frozenset(self._ids(self.git.run_bytes(*log)))
            loose = frozenset(self._ids(self.git.run_bytes(*log, *LOOSE)))
            self._window = (exact, loose)
        return self._window

    def _matches(self, exact_patch: bytes, loose_patch: bytes) -> bool:
        """A commit without its CHANGELOG.md changes has no loose id when it changes only CHANGELOG.md"""
        exact, loose = self.window()
        pid = self._one(exact_patch)
        if pid is not None and pid in exact:
            return True
        loose_id = self._one(loose_patch)
        return loose_id is not None and loose_id in loose

    def unlanded(self, branch: str) -> int:
        """How many of the branch's commits are not on onto; 0 when its whole diff landed as one commit"""
        tip = f"{HEADS}{branch}"
        commits = self.git.run("rev-list", "--no-merges", f"{self.onto}..{tip}").split()
        # rev-list leaves out every commit onto holds, so ancestry needs no check of its own
        missing = [
            c
            for c in commits
            if not self._matches(self.git.run_bytes("show", *DIFF, c), self.git.run_bytes("show", *DIFF, c, *LOOSE))
        ]
        if not missing or self._squashed(tip):
            return 0
        return len(missing)

    def _squashed(self, tip: str) -> bool:
        if not self.git.ok("merge-base", self.onto, tip):
            return False
        base = self.git.run("merge-base", self.onto, tip).strip()
        return self._matches(
            self.git.run_bytes("diff", *DIFF, base, tip), self.git.run_bytes("diff", *DIFF, base, tip, *LOOSE)
        )


def created_times(common: Path) -> dict[Path, dt.datetime]:
    """Each linked worktree's creation, by its resolved path: the modification time of the
    commondir file git writes once into <git-common-dir>/worktrees/<id>/"""
    admin = common / "worktrees"
    found: dict[Path, dt.datetime] = {}
    if not admin.is_dir():
        return found
    for entry in sorted(admin.iterdir()):
        gitdir, commondir = entry / "gitdir", entry / "commondir"
        if not gitdir.is_file() or not commondir.is_file():
            continue
        dotgit = Path(gitdir.read_text(encoding="utf-8").strip())
        where = (dotgit if dotgit.is_absolute() else entry / dotgit).resolve().parent
        found[where] = dt.datetime.fromtimestamp(commondir.stat().st_mtime, dt.UTC)
    return found


def upstreams(git: Git) -> dict[str, str]:
    """Each local branch's upstream branch name on origin, for the branches that have one"""
    out = git.run("for-each-ref", "--format=%(refname:short)%00%(upstream:remotename)%00%(upstream:remoteref)", HEADS)
    found = {}
    for line in out.splitlines():
        name, remote, ref = line.split("\0")
        if remote == REMOTE and ref.startswith(HEADS):
            found[name] = ref[len(HEADS) :]
    return found


def shipmill_worktree(path: Path, others: Iterable[Path]) -> bool:
    """<W>/.claude/worktrees/<name> or <W>/tmp/wt-<name>, for W another worktree of the repository"""
    roots = set(others)
    parent = path.parent
    if parent.name == "worktrees" and parent.parent.name == ".claude" and parent.parent.parent in roots:
        return True
    named = path.name.startswith("wt-") and len(path.name) > len("wt-")
    return named and parent.name == "tmp" and parent.parent in roots


def _inside(cwd: Path, path: Path) -> bool:
    return cwd == path or path in cwd.parents


@dataclass(frozen=True, slots=True)
class _Context:
    default: str
    main: Path
    current: Path
    roots: tuple[Path, ...]
    live: tuple[LiveSession, ...]
    pulls: tuple[PullRequest, ...]
    upstream: dict[str, str]
    landing: Landing


def _reason(ctx: _Context, tree: Worktree, path: Path, age: dt.timedelta | None) -> str | None:
    """The first check the worktree fails, in the spec's order; None when it passes them all"""
    if path == ctx.main:
        return "main checkout"
    if path == ctx.current:
        return "current checkout"
    if not shipmill_worktree(path, (r for r in ctx.roots if r != path)):
        return "not a shipmill worktree"
    if tree.prunable or not path.is_dir():
        return "directory missing"
    if tree.locked is not None:
        return f"locked: {tree.locked}" if tree.locked else "locked"
    if tree.branch is None:
        return "detached HEAD"
    if tree.branch == ctx.default:
        return "holds the default branch"
    if age is None:
        raise ReleaseError(f"git keeps no creation record for {path} under its common dir")
    if age < FLOOR:
        return f"created {_hours(age)}h ago"
    for session in ctx.live:
        if _inside(session.cwd, path):
            return f"live session {session.name}"
    for other in ctx.roots:
        if path in other.parents:  # git status here can't see it, and git worktree remove deletes it
            return f"holds worktree {os.path.relpath(other, ctx.main)}"
    if Git(path).run("status", "--porcelain", "--untracked-files=all").strip():
        return "uncommitted changes"
    unlanded = ctx.landing.unlanded(tree.branch)
    if unlanded:
        return f"{unlanded} commit(s) not landed"
    heads = {tree.branch, ctx.upstream.get(tree.branch, tree.branch)}
    for pr in ctx.pulls:
        if pr.head in heads:
            return f"open PR #{pr.number}"
    return None


def _hours(age: dt.timedelta) -> int:
    return max(0, int(age.total_seconds() // 3600))


def judge(git: Git, github: GitHub, sessions: Sessions, now: dt.datetime) -> list[Judged]:
    """Every worktree of the repository whose checkout git names, with its verdict, in git's
    order (the main checkout first). Reads the live sessions and the open pull requests
    before judging anything, so a failure of either judges nothing"""
    default = git.default_branch()
    onto = f"refs/remotes/{REMOTE}/{default}"
    git.run("fetch", "-q", REMOTE, f"+{HEADS}{default}:{onto}")
    live = tuple(sessions.live())
    pulls = tuple(github.open_pull_requests())
    trees = parse_worktrees(git.run("worktree", "list", "--porcelain", "-z"))
    resolved = [t.path.resolve() for t in trees]
    common = Path(git.run("rev-parse", "--path-format=absolute", "--git-common-dir").strip())
    created = created_times(common)
    main = resolved[0]
    ctx = _Context(
        default=default,
        main=main,
        current=Path(git.run("rev-parse", "--show-toplevel").strip()).resolve(),
        roots=tuple(resolved),
        live=tuple(LiveSession(s.name, s.cwd.resolve()) for s in live),
        pulls=pulls,
        upstream=upstreams(git),
        landing=Landing(git, onto),
    )
    judged = []
    for tree, path in zip(trees, resolved, strict=True):
        when = None if path == main else created.get(path)
        age = None if when is None else now - when
        reason = _reason(ctx, tree, path, age)
        judged.append(
            Judged(
                worktree=tree,
                path=os.path.relpath(path, main),
                created=when,
                age_hours=None if age is None else _hours(age),
                verdict=Verdict.KEPT if reason is not None else Verdict.REMOVABLE,
                reason=reason,
            )
        )
    return judged


COLUMNS = ("verdict", "path", "branch", "age", "reason")


def table(judged: Sequence[Judged], label: Callable[[Judged], str] = lambda j: j.verdict.value) -> str:
    """One row per worktree under a header; label names a row's verdict, so a prune can print REMOVED"""
    rows = [COLUMNS] + [(label(j), j.path, j.worktree.branch or "-", j.age, j.reason or "") for j in judged]
    widths = [max(len(r[i]) for r in rows) for i in range(len(COLUMNS) - 1)]
    lines = ["  ".join([*(c.ljust(w) for c, w in zip(r, widths, strict=False)), r[-1]]).rstrip() for r in rows]
    return "\n".join(lines) + "\n"


def report(judged: Sequence[Judged]) -> dict[str, list[dict[str, object]]]:
    return {"worktrees": [j.record() for j in judged]}
