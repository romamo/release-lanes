"""Spec 009: `shipmill status` as a short linked summary of the factory. A heading with the
verdict (STUCK, WAITS ON YOU, WORKING, IDLE), then one line per thing worth knowing, each
reason for the verdict among them, and each item a direct link. The facts come from
github-ship-watch's rows and a few reads of the checkout, GitHub, and this host's gate;
the verdict and the lines are worked out from them here, with nothing read"""

import datetime as dt
import enum
import json
import plistlib
import re
import shlex
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from shipmill.agent_as_person import NAME as AGENT_AS_PERSON
from shipmill.agent_as_person import Flagged
from shipmill.agents import AgentsConfig, Mode
from shipmill.app import Identity, default_key
from shipmill.config import CONFIG_PATH, config_path, read
from shipmill.errors import ReleaseError
from shipmill.gate import StateRunner, skills_dir
from shipmill.gitrepo import Git
from shipmill.launchd import label
from shipmill.version import Version

# the gate log's decision lines (QUIET, LAUNCH, ...), and the lines a failed tick leaves:
# cli.run's "shipmill: <error>", uvx's "error: ...", or a traceback
DECISION = re.compile(r"^[A-Z]+: ")
FAILURE = re.compile(r"^(?:shipmill: |error: |Traceback )")
NEVER_EXITED = "(never exited)"  # launchctl's last exit code before a job's first run

# watch_state.py's rows by the verdict they bring (S-009-2, S-009-5)
STUCK_ROWS = (
    "BOT_FAILED",
    "BOT_STALLED",
    "WORK_BRANCH_STALE",
    "NOT_PUBLISHED",
    "OPERATE_FAILED",
    "UNHEALTHY",
    "INCIDENT_OPEN",
)
# a hold is a person stopping the factory on purpose (D-15): theirs to lift, not a fault
YOURS_ROWS = ("HOLD", "PROMOTION_DUE", "POSTMORTEM_DUE", "UNTRUSTED", "BRANCH_DELETE_OFF", "SHIPMILL_OUTDATED")
RELEASE_ROWS = ("BOT_OK", "BOT_NONE", "BOT_FAILED", "BOT_STALLED", "WORK_BRANCH_STALE")
TAG_ROWS = ("PUBLISHED", "PUBLISHING", "NOT_PUBLISHED", "PREDATES_PUBLISH", "NO_REGISTRY", "UNANNOUNCED")
# rows whose facts the summary shows in their own words; any other goes under "other" (S-009-13)
PLACED = {
    *STUCK_ROWS,
    *YOURS_ROWS,
    *RELEASE_ROWS,
    *TAG_ROWS,
    "ISSUES",
    "ISSUES_OPEN",
    "PRS_OPEN",
    "NEEDS_DECISION",
    "RUNS_ACTIVE",
    "AGENT_SESSION",
    "LOOP",
    "TRIAGE_MODE",
    "SHIPMILL_VERSION",
    "GATE_NO_APP",  # D-19's row; the github app line says the same
    "HOST_UNKNOWN",  # the sessions line says it
}
# the release rows the repo line names in place of "release ok" (S-009-17)
RELEASE_PROBLEMS = ("BOT_FAILED", "BOT_STALLED", "WORK_BRANCH_STALE", "NOT_PUBLISHED", "UNANNOUNCED")

# triage_state.py's issue states by the line they're listed on (S-009-18)
ISSUE_LINES = (
    ("to triage", ("NEW", "REVISIT", "SPEC_REFUSED", "UNFILLED", "DONE_NOT_CLOSED")),
    ("to build", ("NEEDS_PR", "UNBLOCKED")),
    ("in progress", ("IN_PROGRESS",)),
    ("parked", ("BLOCKED", "POSTPONED", "TRIAGED")),
)
CLOSED_STATES = {"SUSPECT_CLOSE"}  # read for recently closed issues, not open ones
# the needs-decision items share one fix, shown once after the group (S-011-12)
DECISION_FIX = "answer the needs-decision question on each item named"


class Verdict(enum.IntEnum):
    """Ordered from best to worst; the report shows the worst that applies"""

    IDLE = 0
    WORKING = 1
    WAITS = 2
    STUCK = 3

    @property
    def text(self) -> str:
        return "WAITS ON YOU" if self is Verdict.WAITS else self.name


@dataclass(frozen=True, slots=True)
class Reason:
    """A STUCK or WAITS ON YOU reason: what is wrong, and the fix that clears it, an exact
    command or the decision to make, built from trusted values only (S-011-12)"""

    text: str
    fix: str

    def __post_init__(self) -> None:
        if not self.fix.strip():
            raise ValueError(f"a reason without a fix: {self.text}")


@dataclass(frozen=True, slots=True)
class Row:
    """One of watch_state.py's JSON lines; fix is None on a row without one, and on every row
    from a watch_state.py that doesn't print the field yet"""

    state: str
    subject: str
    detail: str
    agent: bool
    fix: str | None = None

    def text(self) -> str:
        return f"{self.state} {self.subject}: {self.detail}"


@dataclass(frozen=True, slots=True)
class Issue:
    """One of triage_state.py's JSON lines; note is its note, such as the pull requests that
    cover it (`#409:open`) or what a blocked issue waits on (`owner/repo#5:open`)"""

    number: int
    state: str
    note: str = ""


@dataclass(frozen=True, slots=True)
class Pull:
    """An open pull request; merge is GitHub's mergeStateStatus, base its base branch"""

    number: int
    draft: bool
    merge: str
    base: str


@dataclass(frozen=True, slots=True)
class Main:
    """The default branch here and on GitHub; local is None without a local branch, and
    checked_out says whether the checkout is on it"""

    branch: str
    local: str | None
    remote: str
    ahead: int
    behind: int
    checked_out: bool


@dataclass(frozen=True, slots=True)
class Described:
    """The newest version tag a commit descends from, and the commits since; tag None
    without one"""

    tag: str | None
    past: int

    def text(self) -> str:
        if self.tag is None:
            return "no release yet"
        return f"at {self.tag}" if not self.past else f"at {self.tag} +{self.past}"


@dataclass(frozen=True, slots=True)
class JobFiles:
    """Where the gate's launchd job lives, for the fixes that name it: its plist, its log
    (None without a StandardOutPath), and the uid whose gui domain runs it"""

    plist: Path
    log: Path | None
    uid: int


@dataclass(frozen=True, slots=True)
class Job:
    """The gate's launchd job, as its plist, `launchctl print`, and its log show it"""

    interval: dt.timedelta | None
    loaded: bool
    running: bool
    last_exit: str | None
    last_run: dt.datetime | None  # the log's modification time; None without a log
    last: str | None  # the log's last decision line, or the failure after it
    failed: bool  # last is a failed tick's line
    files: JobFiles


@dataclass(frozen=True, slots=True)
class Gate:
    """What drives the agents: [agents], the job on this host, and the App check's answer"""

    agents: AgentsConfig | None  # None: the config has no [agents]
    mode_set: bool  # [agents] names mode (D-20)
    launchd: bool  # this host runs launchd (a Mac)
    job: Job | None  # None: no launchd job for the repo (or no launchd)
    woke: dt.datetime | None  # when this host last woke from sleep; launchd skips the ticks it slept through
    app: Identity | None  # the App check's identity, with [agents] app_id
    app_error: str | None  # the App check's error, when it failed
    app_key: Path | None  # the key the App check would use, when it isn't on this host

    @property
    def login(self) -> str | None:
        """The App bot's login: whose needs-decision questions are the gate's (spec 005)"""
        return None if self.app is None else self.app.login


@dataclass(frozen=True, slots=True)
class Facts:
    repo: str
    rows: Sequence[Row]
    issues: Sequence[Issue]
    pulls: Sequence[Pull]
    main: Main
    version: Described  # GitHub's default branch
    gate: Gate
    cli: str  # the installed CLI's version
    now: dt.datetime
    checkout: Path  # the checkout --repo names, which a git fix runs in (D-23)
    command: str  # how a fix names the CLI: cli_command()'s form
    agent_as_person: Flagged | None = None  # spec 012's check, read only with [agents] app_id


@dataclass(frozen=True, slots=True)
class Report:
    """The verdict, its STUCK or WAITS ON YOU reasons with their fixes (each also a line of
    the summary, with its fix, so not printed), what is WORKING or IDLE otherwise, and the
    summary's lines (S-009-16, S-011-12)"""

    verdict: Verdict
    reasons: list[Reason]
    working: list[str]
    lines: list[str]

    def text(self, repo: str) -> str:
        return "\n".join([f"{repo} ({github(repo)}): {self.verdict.text}", *self.lines]) + "\n"


# -- reading -------------------------------------------------------------------------------


def parse_rows(text: str) -> list[Row]:
    rows = []
    for line in text.splitlines():
        if line.strip():
            raw = json.loads(line)
            fix = raw.get("fix")  # spec 011's field; a watch_state.py before it prints none
            if fix is not None and not isinstance(fix, str):
                raise ReleaseError(f"watch_state.py printed a fix that isn't a string: {line}")
            row = Row(str(raw["state"]), str(raw["subject"]), str(raw["detail"]), bool(raw["agent"]), fix or None)
            rows.append(row)
    return rows


def parse_issues(text: str) -> list[Issue]:
    found = (r for r in map(json.loads, text.splitlines()) if r)
    return [Issue(int(r["number"]), str(r["state"]), str(r["note"])) for r in found]


def parse_job(
    plist: dict[str, object], printed: str | None, log: str | None, written: dt.datetime | None, files: JobFiles
) -> Job:
    """printed is `launchctl print`'s output (None when the job isn't loaded); log is the log's
    text and written its modification time (both None without a log)"""
    interval = plist.get("StartInterval")
    state = re.search(r"^\tstate = (.+)$", printed or "", re.MULTILINE)
    code = re.search(r"^\tlast exit code = (.+)$", printed or "", re.MULTILINE)
    last, traceback = None, False
    for line in (log or "").splitlines():
        if DECISION.match(line) or FAILURE.match(line):
            last, traceback = line, line.startswith("Traceback ")
        elif traceback and line.strip() and not line[0].isspace():
            last = line  # a traceback's last unindented line is its error
    return Job(
        interval=dt.timedelta(seconds=interval) if isinstance(interval, int) else None,
        loaded=printed is not None,
        running=state is not None and state.group(1).strip() == "running",
        last_exit=code.group(1).strip() if code else None,
        last_run=written,
        last=last,
        failed=traceback or (last is not None and FAILURE.match(last) is not None),
        files=files,
    )


def parse_waketime(text: str) -> dt.datetime | None:
    """`sysctl -n kern.waketime`: "{ sec = N, usec = M } <date>"; sec 0 before any sleep"""
    found = re.match(r"^\{ sec = (\d+), usec = \d+ \}", text.strip())
    if found is None:
        raise ReleaseError(f"sysctl kern.waketime printed {text.strip()!r}")
    sec = int(found.group(1))
    return dt.datetime.fromtimestamp(sec, tz=dt.UTC) if sec else None


def _checked(proc: subprocess.CompletedProcess[str], what: str) -> str:
    if proc.returncode != 0:
        raise ReleaseError(f"{what} failed (exit {proc.returncode}): {proc.stderr.strip()[-500:]}")
    return proc.stdout


def read_issues(repo: str, run: StateRunner, bot_login: str | None) -> list[Issue]:
    """triage_state.py's states, read as watch_state.py read them (the same --bot-login)"""
    script = skills_dir() / "github-issue-triage" / "scripts" / "triage_state.py"
    login = [] if bot_login is None else ["--bot-login", bot_login]
    proc = run([sys.executable, str(script), repo, "--json", *login])
    if proc.returncode not in (0, 1):  # 1: an issue needs triage, an answer like 0
        raise ReleaseError(f"triage_state.py failed (exit {proc.returncode}): {proc.stderr.strip()[-500:]}")
    return parse_issues(proc.stdout)


def read_pulls(repo: str, run: StateRunner) -> list[Pull]:
    fields = "number,isDraft,mergeStateStatus,baseRefName"
    out = _checked(run(["gh", "pr", "list", "-R", repo, "--json", fields, "-L", "1000"]), "gh pr list")
    found = json.loads(out)
    return [
        Pull(int(p["number"]), bool(p["isDraft"]), str(p["mergeStateStatus"]), str(p["baseRefName"])) for p in found
    ]


def read_branch(repo: str, run: StateRunner) -> str:
    cmd = ["gh", "repo", "view", repo, "--json", "defaultBranchRef", "-q", ".defaultBranchRef.name"]
    branch = _checked(run(cmd), "gh repo view").strip()
    if not branch:
        raise ReleaseError(f"gh repo view {repo} named no default branch")
    return branch


def read_main(git: Git, branch: str) -> Main:
    """After watch_state.py's fetch, so origin's branch is GitHub's"""
    remote = git.run("rev-parse", "--verify", "-q", f"refs/remotes/origin/{branch}^{{commit}}").strip()
    if not git.ok("rev-parse", "--verify", "-q", f"refs/heads/{branch}"):
        return Main(branch, None, remote, 0, 0, False)
    local = git.sha(f"refs/heads/{branch}")
    ahead, behind = git.run("rev-list", "--left-right", "--count", f"{local}...{remote}").split()
    on = (
        git.ok("symbolic-ref", "-q", "HEAD") and git.run("symbolic-ref", "-q", "HEAD").strip() == f"refs/heads/{branch}"
    )
    return Main(branch, local, remote, int(ahead), int(behind), on)


def describe(git: Git, rev: str) -> Described:
    """After watch_state.py's fetch of the tags; `git describe --long` prints <tag>-<n>-g<sha>"""
    if not git.ok("describe", "--tags", "--match", "v[0-9]*", rev):
        return Described(None, int(git.run("rev-list", "--count", rev).strip()))
    tag, past, _ = git.run("describe", "--tags", "--long", "--match", "v[0-9]*", rev).strip().rsplit("-", 2)
    return Described(tag, int(past))


def read_gate(
    repo: str,
    root: Path,
    home: Path,
    platform: str,
    run: StateRunner,
    app: Callable[[int, Path], Identity],
    uid: int,
) -> Gate:
    """[agents] from the checkout's config; on a Mac, the gate's launchd job, its log, and the
    last wake; with an app_id, the App check, with the key the job passes the gate
    (`--app-key`), else the App's default key, unless that key isn't on this host"""
    raw = read(config_path(root)) if (root / CONFIG_PATH).is_file() else {}
    agents = AgentsConfig.load(root) if "agents" in raw else None
    table = raw.get("agents")
    mode_set = isinstance(table, dict) and "mode" in table
    launchd = platform == "darwin"
    job, key, woke = None, None, None
    plist_path = home / "Library" / "LaunchAgents" / f"{label(repo)}.plist"
    if agents is not None and launchd and plist_path.is_file():
        with plist_path.open("rb") as handle:
            plist = plistlib.load(handle)
        if not isinstance(plist, dict):
            raise ReleaseError(f"{plist_path} is not a launchd job")
        args = plist.get("ProgramArguments")
        if isinstance(args, list) and "--app-key" in args[:-1]:
            key = Path(str(args[args.index("--app-key") + 1]))
        proc = run(["launchctl", "print", f"gui/{uid}/{label(repo)}"])
        printed = proc.stdout if proc.returncode == 0 else None
        log_path = plist.get("StandardOutPath")
        log = Path(log_path) if isinstance(log_path, str) else None
        files = JobFiles(plist_path, log, uid)
        if log is not None and log.is_file():
            written = dt.datetime.fromtimestamp(log.stat().st_mtime, tz=dt.UTC)
            job = parse_job(plist, printed, log.read_text(errors="replace"), written, files)
        else:
            job = parse_job(plist, printed, None, None, files)
        woke = parse_waketime(_checked(run(["sysctl", "-n", "kern.waketime"]), "sysctl kern.waketime"))
    identity, error, absent = None, None, None
    if agents is not None and agents.app_id is not None:
        path = key if key is not None else default_key(agents.app_id, home)
        if not path.is_file():
            absent = path
        else:
            try:
                identity = app(agents.app_id, path)
            except ReleaseError as exc:  # the check's failure is the report's finding (S-009-4)
                error = str(exc)
    return Gate(agents, mode_set, launchd, job, woke, identity, error, absent)


# -- the report ----------------------------------------------------------------------------

WIDTH = 14  # the widest label, "needs decision"


def ago(span: dt.timedelta) -> str:
    minutes = int(span.total_seconds() // 60)
    if minutes < 60:
        return f"{max(minutes, 0)} min"
    if minutes < 48 * 60:
        return f"{minutes // 60} h"
    return f"{minutes // (24 * 60)} days"


def numbers(found: Sequence[int]) -> str:
    return " ".join(f"#{n}" for n in found) or "none"


def github(repo: str) -> str:
    return f"https://github.com/{repo}"


def item(repo: str, number: int, pull: bool) -> str:
    """An issue or pull request as its number and its own link, never a search (S-009-18)"""
    return f"#{number} {github(repo)}/{'pull' if pull else 'issues'}/{number}"


def _row_numbers(rows: Sequence[Row], state: str) -> list[int]:
    return [int(n) for r in rows if r.state == state for n in re.findall(r"#(\d+)", r.detail)]


def to_land(rows: Sequence[Row]) -> list[int]:
    """The pull requests in the PRS_OPEN row, newest first; its detail is only `#N` tokens"""
    for r in rows:
        if r.state == "PRS_OPEN" and re.fullmatch(r"#\d+(?: #\d+)*", r.detail) is None:
            raise ReleaseError(f"watch_state.py's PRS_OPEN row is unreadable: {r.detail!r}")
    return sorted(set(_row_numbers(rows, "PRS_OPEN")), reverse=True)


def landing_fix(facts: Facts) -> str | None:
    """S-011-13: with pull requests waiting and no gate landing them, how to land them; None
    with [agents] prs = true or none waiting"""
    agents = facts.gate.agents
    if not to_land(facts.rows) or (agents is not None and agents.prs):
        return None
    land = f"/shipmill:github-pr-triage {facts.repo}"
    return f"set prs = true under [agents] in {CONFIG_PATH}, or land them by hand: {land}"


def landing_reasons(facts: Facts) -> tuple[list[Reason], list[str]]:
    """The open pull requests as waits-on-you reasons when no gate lands them, else as work
    for the gate (S-009-26)"""
    found = to_land(facts.rows)
    fix = landing_fix(facts)
    if not found:
        return [], []
    listed = f"pull request {numbers(found)}"
    agents = facts.gate.agents
    if fix is None:
        return [], [f"{listed} to land"]
    if agents is None:
        return [Reason(f"{listed} wait to land: no gate lands pull requests (no [agents] in the config)", fix)], []
    why = "the gate doesn't land pull requests ([agents] prs = false)"
    return [Reason(f"{listed} wait to land: {why}; set [agents] prs = true, or land them by hand", fix)], []


@dataclass(frozen=True, slots=True)
class Waiting:
    """The items waiting on a decision, told apart: the NEEDS_DECISION row's pull requests
    are those in the open pull request list; triage_state.py names the issues"""

    issues: list[int]
    pulls: list[int]


def waiting(facts: Facts) -> Waiting:
    pulls = {p.number for p in facts.pulls}
    named = _row_numbers(facts.rows, "NEEDS_DECISION")
    issues = sorted({i.number for i in facts.issues if i.state == "NEEDS_DECISION"}, reverse=True)
    return Waiting(issues, sorted({n for n in named if n in pulls}, reverse=True))


@dataclass(frozen=True, slots=True)
class JobProblem:
    """Something wrong with, or waiting at, the gate's launchd job: the verdict it brings
    (None: the row behind it brings its own), its reason, and how the gate line shows it in
    place of OK (S-009-20), with the fix for it (S-011-14), None on a HELD line"""

    verdict: Verdict | None
    reason: str
    shown: str
    fix: str | None


def _log_fix(job: Job, repo: str) -> str:
    """Where a failed run or tick left its error: the log, else launchd's own record"""
    if job.files.log is None:
        return f"launchctl print gui/{job.files.uid}/{label(repo)}"
    return f"tail -n 50 {shlex.quote(str(job.files.log))}"


def job_problems(gate: Gate, job: Job, repo: str, now: dt.datetime) -> list[JobProblem]:
    if not job.loaded:
        load = f"launchctl bootstrap gui/{job.files.uid} {shlex.quote(str(job.files.plist))}"
        return [JobProblem(Verdict.STUCK, "the gate's launchd job is installed but not loaded", "not loaded", load)]
    found = []
    code = re.match(r"^-?\d+", job.last_exit or "")  # launchctl may add a name: "78: EX_CONFIG"
    if code is not None and int(code.group()) != 0:
        reason = f"the gate's last run exited {job.last_exit}"
        found.append(JobProblem(Verdict.STUCK, reason, f"failed: exit {job.last_exit}", _log_fix(job, repo)))
    if job.interval is not None and job.last_run is not None and not job.running:
        since = max(job.last_run, gate.woke) if gate.woke is not None else job.last_run
        if now - since > 2 * job.interval:
            age = ago(now - job.last_run)
            reason = f"the gate last ran {age} ago, every {ago(job.interval)}"
            kick = f"launchctl kickstart gui/{job.files.uid}/{label(repo)}"
            found.append(JobProblem(Verdict.STUCK, reason, f"stale: last run {age} ago", kick))
    last = job.last or ""
    if job.failed:
        found.append(
            JobProblem(Verdict.STUCK, f"the gate's last tick failed: {last}", f"failed: {last}", _log_fix(job, repo))
        )
    elif last.startswith("UNCHANGED"):
        when = re.search(r"retried after (\S+)", last)
        retry = when.group(1) if when else last
        shown = f"cooldown: same findings, retry {retry}"
        session = re.search(r"session ([\w-]+)", last)
        read = f"claude --resume {session.group(1)}" if session else _log_fix(job, repo)
        after = f"after session {session.group(1)}" if session else "after the last session"
        fix = f"the same findings came back {after}: read it ({read}), or wait for the retry"
        reason = f"the gate found the same work and won't retry yet: {last}"
        found.append(JobProblem(Verdict.STUCK, reason, shown, fix))
    elif last.startswith("WAITING"):
        attach = re.search(r"claude attach [\w-]+", last)
        shown = f"waiting on you: {attach.group() if attach else last}"
        found.append(
            JobProblem(Verdict.WAITS, f"the gate: {last}", shown, attach.group() if attach else _log_fix(job, repo))
        )
    elif last.startswith("HELD"):  # the HOLD row brings the verdict, its own line, and the fix
        held = [item(repo, int(n), False) for n in re.findall(r"#(\d+)", last.partition(": no session")[0])]
        found.append(JobProblem(None, "", f"held by {', '.join(held) or last}", None))
    return found


def app_fix(gate: Gate, repo: str, command: str) -> str | None:
    """S-011-14: a failed App check's fix checks the App's install; with no App, D-19's
    setup step; None when the App is fine, or can't be checked here"""
    if gate.agents is None:
        return None
    if gate.agents.app_id is None:
        return f"{command} app-create, then set app_id under [agents] in {CONFIG_PATH} (shipmill-setup's App step)"
    if gate.app_error is not None:
        return f"{command} app-install {repo}"
    return None


def no_job_fix(repo: str, command: str) -> str:
    return f"{command} launchd {repo}"


def gate_reasons(gate: Gate, repo: str, now: dt.datetime, command: str) -> tuple[list[Reason], list[Reason]]:
    """The gate's stuck reasons and its waits-on-you reasons (S-009-3, S-009-4, S-009-5)"""
    stuck: list[Reason] = []
    yours: list[Reason] = []
    if gate.agents is None:
        return stuck, yours
    if gate.launchd:
        if gate.job is None:  # another host may run it: the report reads only this one
            launch = no_job_fix(repo, command)
            yours.append(Reason(f"no launchd job runs the gate on this host: {launch}", launch))
        else:
            for p in job_problems(gate, gate.job, repo, now):
                if p.verdict is None:
                    continue
                if p.fix is None:
                    raise ValueError(f"the gate problem {p.reason!r} has no fix")
                (stuck if p.verdict is Verdict.STUCK else yours).append(Reason(p.reason, p.fix))
    app = app_fix(gate, repo, command)
    if gate.app_error is not None and app is not None:
        stuck.append(Reason(f"the App check fails: {gate.app_error}", app))
    if gate.agents.app_id is None and app is not None:
        yours.append(Reason("the gate has no App: sessions write as your gh login", app))
    return stuck, yours


def latest_shipmill(rows: Sequence[Row]) -> tuple[str | None, str]:
    """shipmill's latest release and the plugin installs, from the SHIPMILL_VERSION row"""
    for r in rows:
        if r.state == "SHIPMILL_VERSION":
            found = re.match(r"^latest (\S+); plugin: (.*)$", r.detail)
            if found is None:
                raise ReleaseError(f"watch_state.py's SHIPMILL_VERSION row is unreadable: {r.detail!r}")
            plugin = re.sub(r" \([^)]*\)", "", found.group(2))  # the project folders, for one line
            return found.group(1), plugin
    return None, "not read"


def cli_outdated(cli: str, latest: str | None) -> bool:
    return latest is not None and Version.parse(cli) < Version.of_tag(latest)


def cli_update(command: str) -> str:
    """S-011-15: the update for the form installed (#238): a `uv tool install` puts a bare
    `shipmill` on PATH (cli_command()); without one, the uvx form runs, so install it"""
    return "uv tool upgrade shipmill" if command == "shipmill" else "uv tool install shipmill"


def row_fix(repo: str, r: Row) -> str:
    """The fix of a row that is a reason: watch_state.py's own (spec 011's `fix`), or from a
    watch_state.py without the field, the fix a SHIPMILL_OUTDATED detail carries, else the
    watch, whose report gives each row's repair"""
    if r.fix is not None:
        return r.fix
    if r.state == "SHIPMILL_OUTDATED" and "; " in r.detail:
        return r.detail.partition("; ")[2]
    return f"/shipmill:github-ship-watch {repo}"


def headless_running(gate: Gate) -> str | None:
    """The gate's last decision when it says its session runs: a headless one isn't in
    `claude agents` (D-17)"""
    last = None if gate.job is None else gate.job.last
    return last if last is not None and last.startswith(("RUNNING", "LAUNCH")) else None


def verdict(facts: Facts) -> tuple[Verdict, list[Reason], list[str]]:
    """The worst verdict that applies with every reason for it, worst first, each with its
    fix (S-011-12); for WORKING or IDLE, what is going on instead"""
    rows, repo = facts.rows, facts.repo
    stuck = [Reason(r.text(), row_fix(repo, r)) for r in rows if r.state in STUCK_ROWS]
    gate_stuck, gate_yours = gate_reasons(facts.gate, repo, facts.now, facts.command)
    stuck += gate_stuck
    wait = waiting(facts)
    yours = []
    if wait.issues:
        yours.append(Reason(f"issue {numbers(wait.issues)} waits on your decision", DECISION_FIX))
    if wait.pulls:
        yours.append(Reason(f"pull request {numbers(wait.pulls)} waits on your decision", DECISION_FIX))
    yours += gate_yours
    yours += [Reason(r.text(), row_fix(repo, r)) for r in rows if r.state in YOURS_ROWS]
    latest, _ = latest_shipmill(rows)
    if cli_outdated(facts.cli, latest):
        yours.append(Reason(f"the shipmill CLI {facts.cli} is older than {latest}", cli_update(facts.command)))
    land_yours, land_working = landing_reasons(facts)
    yours += land_yours
    working = [r.text() for r in rows if r.agent and r.state not in STUCK_ROWS]
    working += land_working
    working += [
        f"session {r.subject}: {r.detail}" for r in rows if r.state == "AGENT_SESSION" and r.detail.startswith("gate ")
    ]
    running = headless_running(facts.gate)
    if running is not None:
        working.append(f"the gate: {running}")
    active = [r for r in rows if r.state == "RUNS_ACTIVE"]
    if active:
        working.append(f"{len(active)} workflow run(s) active")
    if stuck:
        return Verdict.STUCK, stuck + yours, []
    if yours:
        return Verdict.WAITS, yours, []
    if working:
        return Verdict.WORKING, [], working
    return Verdict.IDLE, [], ["nothing to do"]


def _line(label_: str, value: str) -> str:
    return f"  {label_:<{WIDTH}} {value}"


def _group(label_: str, values: Sequence[str]) -> list[str]:
    """One value per line, the label on the first; nothing when there are none (S-009-23)"""
    return [_line(label_ if i == 0 else "", v) for i, v in enumerate(values)]


def _fixed(label_: str, values: Sequence[tuple[str, str | None]]) -> list[str]:
    """A group whose values may carry a fix: each fix on an indented `fix:` line aligned with
    the values, after the last of the run of values sharing it, and not when the line before
    already says it (S-011-12)"""
    lines = []
    for i, (value, fix) in enumerate(values):
        lines.append(_line(label_ if i == 0 else "", value))
        shared = i + 1 < len(values) and values[i + 1][1] == fix
        if fix is not None and not shared and fix not in lines[-1]:
            lines.append(_line("", f"fix: {fix}"))
    return lines


def _after(line: str, fixes: Sequence[str]) -> list[str]:
    """A line and the fixes of what it clears, each once, but none the line already says"""
    return [line, *(_line("", f"fix: {f}") for f in dict.fromkeys(fixes) if f not in line)]


def reason_fix(repo: str, r: Row) -> str | None:
    """The fix a summary line shows for a row it places: a row that is a reason always has
    one; any other only shows its own, under `other`"""
    return row_fix(repo, r) if r.state in STUCK_ROWS or r.state in YOURS_ROWS else None


def repo_lines(facts: Facts) -> list[str]:
    """S-009-17: the default branch against GitHub's, its version, and the release's state,
    then the fix of each release problem that is a reason"""
    fixes = [f for r in facts.rows if r.state in RELEASE_PROBLEMS if (f := reason_fix(facts.repo, r)) is not None]
    return _after(repo_line(facts), fixes)


def repo_line(facts: Facts) -> str:
    m = facts.main
    if m.local is None:
        state = f"no local {m.branch}"
    elif m.ahead and m.behind:
        state = f"diverged ({m.ahead} ahead, {m.behind} behind)"
    elif m.behind:  # S-011-16: the pull names the checkout, so it works from any folder (D-23)
        at = shlex.quote(str(facts.checkout))
        # off the branch, a pull would pull the branch checked out: fast-forward the default one
        catch_up = "pull --ff-only" if m.checked_out else f"fetch origin {shlex.quote(f'{m.branch}:{m.branch}')}"
        state = f"{m.behind} behind: git -C {at} {catch_up}"
    elif m.ahead:
        state = f"{m.ahead} ahead"
    else:
        state = "in sync"
    # BOT_NONE: no release workflow or policy, so no release to call ok; no verdict reason
    problems = ["no release workflow"] if any(r.state == "BOT_NONE" for r in facts.rows) else []
    for r in facts.rows:
        if r.state in RELEASE_PROBLEMS:
            run = re.search(r"https://\S+", r.detail) if r.state == "BOT_FAILED" else None
            problems.append(f"{r.state} {run.group() if run else r.subject}")
    at = f" {facts.version.text()}" if facts.version.tag is not None else f", {facts.version.text()}"
    return _line("repo", f"{state}{at}, {'; '.join(problems) or 'release ok'}")


def _subject_item(repo: str, r: Row) -> str:
    """A row whose subject is an issue, "#N", as its link"""
    found = re.fullmatch(r"#(\d+)", r.subject)
    return item(repo, int(found.group(1)), False) if found else f"{r.subject} {r.detail}"


def _promotion(repo: str, r: Row) -> str:
    """The environment, and the proposal issue's link when operate opened one"""
    proposal = re.match(r"#(\d+) ", r.detail)
    return f"{r.subject} {item(repo, int(proposal.group(1)), False)}" if proposal else f"{r.subject}: {r.detail}"


def session_values(facts: Facts) -> list[str]:
    """The gate's sessions, counted: those `claude agents` lists, else the headless one the
    gate's log says runs; or why the sessions weren't read"""
    unknown = [f"not read: {r.detail}" for r in facts.rows if r.state == "HOST_UNKNOWN"]
    gates = [r for r in facts.rows if r.state == "AGENT_SESSION" and r.detail.startswith("gate ")]
    shown = [f"{r.detail.split(',')[0]} ({r.subject})" for r in gates]
    running = headless_running(facts.gate)
    if not gates and running is not None:
        shown.append(f"gate log {running}")
    return [*unknown, *([f"{len(shown)}: {'; '.join(shown)}"] if shown else [])]


def issue_value(repo: str, issue: Issue) -> str:
    """An issue on its line with what acting on it needs, from triage_state.py's note, never
    its title (S-011-18): an in-progress issue's open pull requests, and why a parked one is
    parked"""
    shown = item(repo, issue.number, False)
    if issue.state == "IN_PROGRESS":
        opened = [int(n) for n in re.findall(r"(?<![\w/])#(\d+):open\b", issue.note)]
        return f"{shown} → {', '.join(item(repo, n, True) for n in opened)}" if opened else shown
    if issue.state == "BLOCKED":
        waits = re.findall(r"([\w.-]+/[\w.-]+#\d+):(\S+)", issue.note)
        if waits:
            named = ", ".join(f"{ref} ({state})" for ref, state in waits)
            when = "it closes or merges" if len(waits) == 1 else "they close or merge"
            return f"{shown} waits on {named}; unblocks when {when}"
        if issue.note == "labelled blocked":
            remove = f"gh issue edit {issue.number} -R {repo} --remove-label blocked"
            return f"{shown} labelled blocked: {remove} once it can go on"
        return shown
    if issue.state in ("POSTPONED", "TRIAGED"):
        return f"{shown} {issue.state.lower()}"
    return shown


def pull_value(facts: Facts, number: int) -> str:
    """A pull request to land, with what GitHub says keeps it from merging (S-011-17)"""
    shown = item(facts.repo, number, True)
    for p in facts.pulls:
        if p.number == number and p.merge == "BEHIND":
            return f"{shown} behind {p.base}: gh pr update-branch {number} -R {facts.repo}"
        if p.number == number and p.merge == "DIRTY":
            return f"{shown} conflicts with {p.base}: needs a rebase"
    return shown


def item_lines(facts: Facts) -> list[str]:
    """The lines shown only when they hold something, between repo and open issues"""
    repo, rows = facts.repo, facts.rows
    pulls = {p.number for p in facts.pulls}

    def of(*states: str) -> list[Row]:
        return [r for r in rows if r.state in states]

    def fixed(r: Row) -> str | None:
        return reason_fix(repo, r)

    wait = waiting(facts)
    decide = [*(item(repo, n, False) for n in wait.issues), *(item(repo, n, True) for n in wait.pulls)]
    lines = _fixed("needs decision", [(v, DECISION_FIX) for v in decide])
    lines += _fixed("hold", [(_subject_item(repo, r), fixed(r)) for r in of("HOLD")])
    lines += _fixed("incident", [(_subject_item(repo, r), fixed(r)) for r in of("INCIDENT_OPEN")])
    lines += _fixed("postmortem", [(_subject_item(repo, r), fixed(r)) for r in of("POSTMORTEM_DUE")])
    lines += _fixed("promotion", [(_promotion(repo, r), fixed(r)) for r in of("PROMOTION_DUE")])
    lines += _fixed("operate", [(f"{r.subject} {r.detail}", fixed(r)) for r in of("OPERATE_FAILED", "UNHEALTHY")])
    untrusted = [(r, int(n)) for r in of("UNTRUSTED") for n in re.findall(r"#(\d+)", r.detail)]
    lines += _fixed("untrusted", [(item(repo, n, n in pulls), fixed(r)) for r, n in untrusted])
    suspect = sorted((i.number for i in facts.issues if i.state in CLOSED_STATES), reverse=True)
    lines += _group("suspect close", [item(repo, n, False) for n in suspect])
    lines += _fixed("branches", [("merged branches kept", fixed(r)) for r in of("BRANCH_DELETE_OFF")][:1])
    for name, states in ISSUE_LINES:
        found = sorted((i for i in facts.issues if i.state in states), key=lambda i: i.number, reverse=True)
        lines += _group(name, [issue_value(repo, i) for i in found])
        if name == "in progress":  # S-009-24: the pull requests waiting to land follow the work
            land = [pull_value(facts, n) for n in to_land(rows)]
            # without [agents] there is no landing line, so the fix follows the pull requests
            fix = landing_fix(facts) if facts.gate.agents is None else None
            lines += _fixed("to land", [(v, fix) for v in land])
    drafts = sorted((p.number for p in facts.pulls if p.draft), reverse=True)
    ready = [f"{item(repo, n, True)}: gh pr ready {n} -R {repo} once it is ready" for n in drafts]
    lines += _group("drafts", ready)  # S-011-19
    lines += _group("sessions", session_values(facts))
    runs = of("RUNS_ACTIVE")
    shown = ", ".join(f"{r.subject} {r.detail.rsplit(', ', 1)[-1]}" for r in runs)
    lines += _group("runs", [f"{len(runs)}: {shown}"] if runs else [])
    other = [(f"{r.state} {r.subject} {r.detail}", r.fix) for r in rows if r.state not in PLACED]
    lines += _fixed("other", other)
    return lines


def count_line(label_: str, count: int, link: str) -> str:
    """S-009-19: the count and the list's link, or none"""
    return _line(label_, f"{count} {link}" if count else "none")


def gate_lines(facts: Facts) -> list[str]:
    """S-009-20, S-009-21, S-009-25: the gate, then with [agents] its mode, whether it lands
    pull requests, and the App"""
    gate = facts.gate
    if gate.agents is None:
        return [_line("gate", "none: no [agents] in the config")]
    fixes: list[str] = []
    if not gate.launchd:
        value = "can't check here: no launchd"
    elif gate.job is None:
        value = "no launchd job on this Mac"
        fixes.append(no_job_fix(facts.repo, facts.command))
    else:
        job = gate.job
        problems = job_problems(gate, job, facts.repo, facts.now)
        fixes += [p.fix for p in problems if p.fix is not None]
        schedule = f"launchd every {ago(job.interval)}" if job.interval else "launchd"
        value = f"{'; '.join(p.shown for p in problems) or 'OK'}, {schedule}"
    mode = str(gate.agents.mode) if gate.mode_set else f"{Mode.INTERACTIVE} (not set)"
    if gate.agents.app_id is None:
        app = "not active"
    elif gate.app_key is not None:
        app = f"can't check here: no key at {gate.app_key}"
    elif gate.app is not None:
        app = "active"
    else:
        app = f"not connected: {gate.app_error}"
    landing = "on" if gate.agents.prs else "off: [agents] prs = false (the gate opens PRs but never lands them)"
    land = landing_fix(facts)
    app_fixes = [] if (fix := app_fix(gate, facts.repo, facts.command)) is None else [fix]
    return [
        *_after(_line("gate", value), fixes),
        _line("mode", mode),
        *_after(_line("landing", landing), [] if land is None else [land]),
        *_after(_line("github app", app), app_fixes),
    ]


def shipmill_lines(facts: Facts) -> list[str]:
    """S-009-22: the CLI's version, the plugin's when it differs, and the updates, each the
    fix for its install (S-011-15)"""
    latest, plugin = latest_shipmill(facts.rows)
    parts = [facts.cli]
    if any(v != facts.cli for v in re.findall(r"\d+\.\d+\.\d+[^\s,;]*", plugin)):
        parts.append(f"plugin {plugin}")
    outdated = [r for r in facts.rows if r.state == "SHIPMILL_OUTDATED"]
    fixes = [r.detail.partition("; ")[2] for r in outdated]
    if cli_outdated(facts.cli, latest):
        fixes.append(cli_update(facts.command))
    if fixes:
        parts.append(f"update available: {'; '.join(fixes)}")
    else:
        parts.append("up to date" if latest is not None else "latest not read")
    return _after(_line("shipmill", ", ".join(parts)), [row_fix(facts.repo, r) for r in outdated])


def agent_as_person_lines(facts: Facts) -> list[str]:
    """S-012-7: a WARN row when agent-marked items went out under a person's login, its fix on
    an indented `fix:` line (S-011-12); a warning beside the verdict, not a reason for it"""
    found = facts.agent_as_person
    if found is None or not found.urls:
        return []
    return _after(_line("WARN", f"{AGENT_AS_PERSON}: {found.summary()}"), [found.fix(facts.command)])


def report(facts: Facts) -> Report:
    found, reasons, working = verdict(facts)
    open_ = sum(1 for i in facts.issues if i.state not in CLOSED_STATES)
    lines = [
        *repo_lines(facts),
        *item_lines(facts),
        count_line("open issues", open_, f"{github(facts.repo)}/issues"),
        count_line("pull requests", len(facts.pulls), f"{github(facts.repo)}/pulls"),
        *gate_lines(facts),
        *agent_as_person_lines(facts),
        *shipmill_lines(facts),
    ]
    return Report(found, reasons, working, lines)
