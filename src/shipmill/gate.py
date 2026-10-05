"""Start a Claude Code session for a repo only when its state needs one.

The gate is code: it reads the repo's state through github-ship-watch's watch_state.py,
asks Claude Code which of its own sessions are alive, and launches a background session
(`claude --bg`) only when there is work, no earlier session is still running or waiting on
the user, and the work changed since the last launch. Everything it knows comes from
GitHub and from Claude Code; the one file it writes, under the checkout's git directory,
only keeps an unchanged state from starting a session on every tick.
"""

import datetime as dt
import enum
import hashlib
import json
import re
import subprocess
import sys
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from shipmill.agents import AgentsConfig
from shipmill.autonomy import Hold
from shipmill.errors import ReleaseError
from shipmill.gitrepo import Git

RECORD = "gate.json"
PRS_OPEN = "PRS_OPEN"  # watch_state.py's open pull requests: work only with [agents] prs = true


@dataclass(frozen=True, slots=True)
class Finding:
    """One watch_state.py row; agent is its own verdict that the row needs an agent"""

    state: str
    subject: str
    detail: str
    agent: bool

    def line(self) -> str:
        return f"{self.state} {self.subject}: {self.detail}" if self.detail else f"{self.state} {self.subject}"


@dataclass(frozen=True, slots=True)
class Session:
    """One Claude Code session, as `claude agents --json` lists it"""

    id: str
    name: str
    status: str | None  # busy | idle | waiting; None once the process ended
    state: str | None  # background only: working | blocked | done | failed

    @property
    def running(self) -> bool:
        return self.state == "working" or self.status == "busy"

    @property
    def blocked(self) -> bool:
        return self.state == "blocked"

    @property
    def finished(self) -> bool:
        return self.state in ("done", "failed") and self.status != "busy"


@dataclass(frozen=True, slots=True)
class Launch:
    """The last session the gate started, and the findings it started it for"""

    fingerprint: str
    session: str
    at: dt.datetime


class Action(enum.Enum):
    HELD = "HELD"  # an open shipmill-hold issue stops every launch (D-11)
    QUIET = "QUIET"  # nothing needs an agent
    RUNNING = "RUNNING"  # a session is still working
    WAITING = "WAITING"  # a session waits on the user
    UNCHANGED = "UNCHANGED"  # the same findings already got a session
    LAUNCH = "LAUNCH"


@dataclass(frozen=True, slots=True)
class Decision:
    action: Action
    reason: str
    work: tuple[Finding, ...]
    stop: tuple[str, ...] = ()  # finished sessions to stop before launching


def fingerprint(work: Iterable[Finding]) -> str:
    lines = sorted(f"{f.state}\x00{f.subject}\x00{f.detail}" for f in work)
    return hashlib.sha256("\n".join(lines).encode()).hexdigest()[:16]


def held(hold: Hold, sessions: Sequence[Session]) -> Decision | None:
    """D-11: a hold starts nothing. A session already running finishes; the reason names it
    so a person can stop it"""
    if not hold.on:
        return None
    live = [s.id for s in sessions if s.running or s.blocked]
    still = f"; still open: {', '.join(f'claude stop {i}' for i in live)}" if live else ""
    return Decision(Action.HELD, f"{hold.reason}: no session starts{still}", ())


def busy(sessions: Sequence[Session]) -> Decision | None:
    """A session that is still working or waits on the user holds the repo"""
    for s in sessions:
        if s.blocked:
            return Decision(Action.WAITING, f"session {s.id} waits on you: claude attach {s.id}", ())
    for s in sessions:
        if s.running:
            return Decision(Action.RUNNING, f"session {s.id} is still working: claude attach {s.id}", ())
    return None


def decide(
    findings: Sequence[Finding],
    sessions: Sequence[Session],
    last: Launch | None,
    now: dt.datetime,
    retry: dt.timedelta,
    prs: bool = False,
) -> Decision:
    pending = busy(sessions)
    if pending is not None:
        return pending
    work = tuple(f for f in findings if f.agent or (prs and f.state == PRS_OPEN))
    if not work:
        return Decision(Action.QUIET, "nothing needs an agent", work)
    if last is not None and last.fingerprint == fingerprint(work) and now - last.at < retry:
        again = (last.at + retry).isoformat(timespec="minutes")
        return Decision(Action.UNCHANGED, f"same findings as session {last.session}; retried after {again}", work)
    finished = tuple(s.id for s in sessions if s.finished)
    return Decision(Action.LAUNCH, f"{len(work)} finding(s) need an agent", work, finished)


def prompt(template: str, repo: str, work: Sequence[Finding], now: dt.datetime) -> str:
    found = "\n".join(f"- {f.line()}" for f in work)
    stamp = now.isoformat(timespec="minutes")
    return f"{template.replace('{repo}', repo)}\n\nThe shipmill gate found this at {stamp} (from code):\n{found}"


def session_name(repo: str) -> str:
    return f"shipmill {repo}"


def parse_sessions(text: str, repo: str) -> list[Session]:
    """This repo's gate sessions from `claude agents --json`"""
    try:
        rows = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ReleaseError(f"claude agents --json printed no JSON: {exc}") from None
    if not isinstance(rows, list):
        raise ReleaseError("claude agents --json: expected a JSON array")
    prefix = session_name(repo)
    sessions = []
    for row in rows:
        name = str(row.get("name") or "")
        if row.get("kind") == "background" and (name == prefix or name.startswith(prefix + " ")):
            sessions.append(Session(str(row["id"]), name, row.get("status"), row.get("state")))
    return sessions


def parse_launched(text: str) -> str:
    """The session id `claude --bg` prints, in its `claude attach <id>` hint"""
    found = re.search(r"claude attach (\S+)", text)
    if found is None:
        raise ReleaseError(f"claude --bg printed no session id: {text.strip()[:300]}")
    return found.group(1)


def parse_findings(text: str) -> list[Finding]:
    """watch_state.py --json's rows; a row without a boolean agent is refused, never guessed"""
    findings = []
    for line in text.splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ReleaseError(f"watch_state.py printed {line[:200]!r}, not JSON: {exc}") from None
        if not isinstance(row, dict):
            raise ReleaseError(f"watch_state.py printed {line[:200]!r}, not a row")
        texts = [row.get(key) for key in ("state", "subject", "detail")]
        if not all(isinstance(t, str) for t in texts) or not isinstance(row.get("agent"), bool):
            raise ReleaseError(
                f"watch_state.py printed {line[:200]!r}: a row needs string state, subject, and detail,"
                " and a boolean agent"
            )
        findings.append(Finding(row["state"], row["subject"], row["detail"], row["agent"]))
    return findings


class Claude(Protocol):
    def sessions(self, workspace: Path, repo: str) -> list[Session]: ...
    def launch(self, workspace: Path, name: str, text: str) -> str: ...
    def stop(self, session: str) -> None: ...


class ClaudeCli:
    def __init__(self, args: Sequence[str] = ()) -> None:
        self.args = tuple(args)  # extra flags for the launched session, such as --permission-mode

    def _run(self, cmd: list[str], cwd: Path) -> str:
        proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, check=False)
        if proc.returncode != 0:
            raise ReleaseError(f"{' '.join(cmd[:3])} failed: {(proc.stderr or proc.stdout).strip()[:500]}")
        return proc.stdout

    def sessions(self, workspace: Path, repo: str) -> list[Session]:
        return parse_sessions(self._run(["claude", "agents", "--json", "--cwd", str(workspace)], workspace), repo)

    def launch(self, workspace: Path, name: str, text: str) -> str:
        return parse_launched(self._run(["claude", "--bg", "-n", name, *self.args, text], workspace))

    def stop(self, session: str) -> None:
        self._run(["claude", "stop", session], Path.cwd())


def skills_dir() -> Path:
    """The skills bundled in the wheel, or the checkout's skills folder in development"""
    here = Path(__file__).resolve().parent
    for candidate in (here / "skills", here.parents[1] / "skills"):
        if (candidate / "github-ship-watch").is_dir():
            return candidate
    raise ReleaseError("this shipmill install has no skills folder; reinstall it")


def watch(repo: str, workspace: Path) -> list[Finding]:
    """watch_state.py's rows for the repo; it exits 1 when any needs action"""
    script = skills_dir() / "github-ship-watch" / "scripts" / "watch_state.py"
    cmd = [sys.executable, str(script), repo, "--repo-dir", str(workspace), "--json"]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if proc.returncode not in (0, 1):
        raise ReleaseError(f"watch_state.py failed: {proc.stderr.strip()[:500]}")
    return parse_findings(proc.stdout)


def state_dir(git: Git) -> Path:
    """Shared by every worktree of the checkout, and never committed"""
    common = Path(git.run("rev-parse", "--git-common-dir").strip())
    return (common if common.is_absolute() else git.root / common) / "shipmill"


def load_launch(path: Path) -> Launch | None:
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return Launch(str(data["fingerprint"]), str(data["session"]), dt.datetime.fromisoformat(data["at"]))
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        raise ReleaseError(f"{path} is malformed ({exc}); delete it to start over") from None


def save_launch(path: Path, launch: Launch) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {"fingerprint": launch.fingerprint, "session": launch.session, "at": launch.at.isoformat()}
    path.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")


def check_checkout(git: Git, repo: str) -> None:
    owner, _, name = repo.partition("/")
    if not owner or not name or "/" in name:
        raise ReleaseError(f"repo must be owner/name, got {repo!r}")
    origin = git.run("remote", "get-url", "origin").strip()
    if not re.search(rf"[:/]{re.escape(repo)}(\.git)?/?$", origin, re.IGNORECASE):
        raise ReleaseError(f"{git.root}'s origin is {origin}, not {repo}")


def require_dedicated(git: Git) -> None:
    """A gate checkout is detached and clean: one with a branch or changes is someone's
    working copy, and a session started there would branch and commit in it"""
    hint = "create one with `git worktree add --detach <path> origin/<default>`"
    if git.run("rev-parse", "--abbrev-ref", "HEAD").strip() != "HEAD":
        raise ReleaseError(f"the gate needs a detached checkout; {git.root} is on a branch ({hint})")
    if git.run("status", "--porcelain", "--untracked-files=no").strip():
        raise ReleaseError(f"the gate needs a clean checkout; {git.root} has changes ({hint})")


def refresh(git: Git) -> None:
    """Move a dedicated gate checkout to the head of origin's default branch, so the session
    reads the current [agents] section, CLAUDE.md, and skills"""
    require_dedicated(git)
    head = git.run("ls-remote", "--symref", "origin", "HEAD")
    found = re.search(r"^ref: refs/heads/(\S+)\s+HEAD$", head, re.MULTILINE)
    if found is None:
        raise ReleaseError(f"origin of {git.root} names no default branch")
    git.run("fetch", "-q", "origin", found.group(1))
    git.run("checkout", "-q", "--detach", "FETCH_HEAD")


def gate(
    git: Git,
    repo: str,
    config: Callable[[], AgentsConfig],
    claude: Claude,
    findings: Callable[[], list[Finding]],
    now: dt.datetime,
    hold: Callable[[], Hold],
    refresh_checkout: bool = False,
    dry_run: bool = False,
) -> tuple[Decision, str | None]:
    """Decide, and on LAUNCH stop the finished sessions and start a new one. Returns the
    decision and the launched session's id. A hold, then a busy session, ends the run before
    the checkout moves, the config is read, or the state is read"""
    check_checkout(git, repo)
    record = state_dir(git) / RECORD
    sessions = claude.sessions(git.root, repo)
    stopped = held(hold(), sessions)
    if stopped is not None:
        return stopped, None
    pending = busy(sessions)
    if pending is not None:
        return pending, None
    if refresh_checkout and not dry_run:
        refresh(git)
    agents = config()
    retry = dt.timedelta(hours=agents.retry_hours)
    decision = decide(findings(), sessions, load_launch(record), now, retry, agents.prs)
    if decision.action is not Action.LAUNCH or dry_run:
        return decision, None
    for session in decision.stop:
        claude.stop(session)
    name = f"{session_name(repo)} {now:%Y-%m-%d %H:%M}"
    launched = claude.launch(git.root, name, prompt(agents.prompt, repo, decision.work, now))
    save_launch(record, Launch(fingerprint(decision.work), launched, now))
    return decision, launched
