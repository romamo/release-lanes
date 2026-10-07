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
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from shipmill import CLI
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
class Row:
    """One of watch_state.py's JSON lines"""

    state: str
    subject: str
    detail: str
    agent: bool

    def text(self) -> str:
        return f"{self.state} {self.subject}: {self.detail}"


@dataclass(frozen=True, slots=True)
class Issue:
    """One of triage_state.py's JSON lines"""

    number: int
    state: str


@dataclass(frozen=True, slots=True)
class Pull:
    number: int
    draft: bool


@dataclass(frozen=True, slots=True)
class Main:
    """The default branch here and on GitHub; local is None without a local branch"""

    branch: str
    local: str | None
    remote: str
    ahead: int
    behind: int


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
class Job:
    """The gate's launchd job, as its plist, `launchctl print`, and its log show it"""

    interval: dt.timedelta | None
    loaded: bool
    running: bool
    last_exit: str | None
    last_run: dt.datetime | None  # the log's modification time; None without a log
    last: str | None  # the log's last decision line, or the failure after it
    failed: bool  # last is a failed tick's line


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


@dataclass(frozen=True, slots=True)
class Report:
    """The verdict, its reasons (each also a line of the summary, so not printed), and the
    summary's lines (S-009-16)"""

    verdict: Verdict
    reasons: list[str]
    lines: list[str]

    def text(self, repo: str) -> str:
        return "\n".join([f"{repo} ({github(repo)}): {self.verdict.text}", *self.lines]) + "\n"


# -- reading -------------------------------------------------------------------------------


def parse_rows(text: str) -> list[Row]:
    rows = []
    for line in text.splitlines():
        if line.strip():
            raw = json.loads(line)
            rows.append(Row(str(raw["state"]), str(raw["subject"]), str(raw["detail"]), bool(raw["agent"])))
    return rows


def parse_issues(text: str) -> list[Issue]:
    return [Issue(int(r["number"]), str(r["state"])) for r in map(json.loads, text.splitlines()) if r]


def parse_job(plist: dict[str, object], printed: str | None, log: str | None, written: dt.datetime | None) -> Job:
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
    out = _checked(run(["gh", "pr", "list", "-R", repo, "--json", "number,isDraft", "-L", "1000"]), "gh pr list")
    return [Pull(int(p["number"]), bool(p["isDraft"])) for p in json.loads(out)]


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
        return Main(branch, None, remote, 0, 0)
    local = git.sha(f"refs/heads/{branch}")
    ahead, behind = git.run("rev-list", "--left-right", "--count", f"{local}...{remote}").split()
    return Main(branch, local, remote, int(ahead), int(behind))


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
        if log is not None and log.is_file():
            written = dt.datetime.fromtimestamp(log.stat().st_mtime, tz=dt.UTC)
            job = parse_job(plist, printed, log.read_text(errors="replace"), written)
        else:
            job = parse_job(plist, printed, None, None)
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


def landing_reasons(facts: Facts) -> tuple[list[str], list[str]]:
    """The open pull requests as waits-on-you reasons when no gate lands them, else as work
    for the gate (S-009-26)"""
    found = to_land(facts.rows)
    if not found:
        return [], []
    listed = f"pull request {numbers(found)}"
    agents = facts.gate.agents
    if agents is None:
        return [f"{listed} wait to land: no gate lands pull requests (no [agents] in the config)"], []
    if not agents.prs:
        why = "the gate doesn't land pull requests ([agents] prs = false)"
        return [f"{listed} wait to land: {why}; set [agents] prs = true, or land them by hand"], []
    return [], [f"{listed} to land"]


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
    place of OK (S-009-20)"""

    verdict: Verdict | None
    reason: str
    shown: str


def job_problems(gate: Gate, job: Job, repo: str, now: dt.datetime) -> list[JobProblem]:
    if not job.loaded:
        return [JobProblem(Verdict.STUCK, "the gate's launchd job is installed but not loaded", "not loaded")]
    found = []
    code = re.match(r"^-?\d+", job.last_exit or "")  # launchctl may add a name: "78: EX_CONFIG"
    if code is not None and int(code.group()) != 0:
        reason = f"the gate's last run exited {job.last_exit}"
        found.append(JobProblem(Verdict.STUCK, reason, f"failed: exit {job.last_exit}"))
    if job.interval is not None and job.last_run is not None and not job.running:
        since = max(job.last_run, gate.woke) if gate.woke is not None else job.last_run
        if now - since > 2 * job.interval:
            age = ago(now - job.last_run)
            reason = f"the gate last ran {age} ago, every {ago(job.interval)}"
            found.append(JobProblem(Verdict.STUCK, reason, f"stale: last run {age} ago"))
    last = job.last or ""
    if job.failed:
        found.append(JobProblem(Verdict.STUCK, f"the gate's last tick failed: {last}", f"failed: {last}"))
    elif last.startswith("UNCHANGED"):
        when = re.search(r"retried after (\S+)", last)
        shown = f"cooldown: same findings, retry {when.group(1) if when else last}"
        found.append(JobProblem(Verdict.STUCK, f"the gate found the same work and won't retry yet: {last}", shown))
    elif last.startswith("WAITING"):
        attach = re.search(r"claude attach \S+", last)
        shown = f"waiting on you: {attach.group() if attach else last}"
        found.append(JobProblem(Verdict.WAITS, f"the gate: {last}", shown))
    elif last.startswith("HELD"):  # the HOLD row brings the verdict, and its own line
        held = [item(repo, int(n), False) for n in re.findall(r"#(\d+)", last.partition(": no session")[0])]
        found.append(JobProblem(None, "", f"held by {', '.join(held) or last}"))
    return found


def gate_reasons(gate: Gate, repo: str, now: dt.datetime) -> tuple[list[str], list[str]]:
    """The gate's stuck reasons and its waits-on-you reasons (S-009-3, S-009-4, S-009-5)"""
    stuck: list[str] = []
    yours: list[str] = []
    if gate.agents is None:
        return stuck, yours
    if gate.launchd:
        if gate.job is None:  # another host may run it: the report reads only this one
            yours.append(f"no launchd job runs the gate on this host: {CLI} launchd {repo}")
        else:
            for p in job_problems(gate, gate.job, repo, now):
                if p.verdict is Verdict.STUCK:
                    stuck.append(p.reason)
                elif p.verdict is Verdict.WAITS:
                    yours.append(p.reason)
    if gate.app_error is not None:
        stuck.append(f"the App check fails: {gate.app_error}")
    if gate.agents.app_id is None:
        yours.append("the gate has no App: sessions write as your gh login")
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


def headless_running(gate: Gate) -> str | None:
    """The gate's last decision when it says its session runs: a headless one isn't in
    `claude agents` (D-17)"""
    last = None if gate.job is None else gate.job.last
    return last if last is not None and last.startswith(("RUNNING", "LAUNCH")) else None


def verdict(facts: Facts) -> tuple[Verdict, list[str]]:
    """The worst verdict that applies, and every reason for each, worst first"""
    rows = facts.rows
    stuck = [r.text() for r in rows if r.state in STUCK_ROWS]
    gate_stuck, gate_yours = gate_reasons(facts.gate, facts.repo, facts.now)
    stuck += gate_stuck
    wait = waiting(facts)
    yours = []
    if wait.issues:
        yours.append(f"issue {numbers(wait.issues)} waits on your decision")
    if wait.pulls:
        yours.append(f"pull request {numbers(wait.pulls)} waits on your decision")
    yours += gate_yours
    yours += [r.text() for r in rows if r.state in YOURS_ROWS]
    latest, _ = latest_shipmill(rows)
    if cli_outdated(facts.cli, latest):
        yours.append(f"the shipmill CLI {facts.cli} is older than {latest}")
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
        return Verdict.STUCK, stuck + yours
    if yours:
        return Verdict.WAITS, yours
    if working:
        return Verdict.WORKING, working
    return Verdict.IDLE, ["nothing to do"]


def _line(label_: str, value: str) -> str:
    return f"  {label_:<{WIDTH}} {value}"


def _group(label_: str, values: Sequence[str]) -> list[str]:
    """One value per line, the label on the first; nothing when there are none (S-009-23)"""
    return [_line(label_ if i == 0 else "", v) for i, v in enumerate(values)]


def repo_line(facts: Facts) -> str:
    """S-009-17: the default branch against GitHub's, its version, and the release's state"""
    m = facts.main
    if m.local is None:
        state = f"no local {m.branch}"
    elif m.ahead and m.behind:
        state = f"diverged ({m.ahead} ahead, {m.behind} behind)"
    elif m.behind:
        state = f"{m.behind} behind (git pull)"
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


def item_lines(facts: Facts) -> list[str]:
    """The lines shown only when they hold something, between repo and open issues"""
    repo, rows = facts.repo, facts.rows
    pulls = {p.number for p in facts.pulls}

    def of(*states: str) -> list[Row]:
        return [r for r in rows if r.state in states]

    wait = waiting(facts)
    decide = [*(item(repo, n, False) for n in wait.issues), *(item(repo, n, True) for n in wait.pulls)]
    lines = _group("needs decision", decide)
    lines += _group("hold", [_subject_item(repo, r) for r in of("HOLD")])
    lines += _group("incident", [_subject_item(repo, r) for r in of("INCIDENT_OPEN")])
    lines += _group("postmortem", [_subject_item(repo, r) for r in of("POSTMORTEM_DUE")])
    lines += _group("promotion", [_promotion(repo, r) for r in of("PROMOTION_DUE")])
    lines += _group("operate", [f"{r.subject} {r.detail}" for r in of("OPERATE_FAILED", "UNHEALTHY")])
    lines += _group("untrusted", [item(repo, n, n in pulls) for n in _row_numbers(rows, "UNTRUSTED")])
    suspect = sorted((i.number for i in facts.issues if i.state in CLOSED_STATES), reverse=True)
    lines += _group("suspect close", [item(repo, n, False) for n in suspect])
    lines += _group("branches", ["merged branches kept"] if of("BRANCH_DELETE_OFF") else [])
    for name, states in ISSUE_LINES:
        found = sorted((i.number for i in facts.issues if i.state in states), reverse=True)
        lines += _group(name, [item(repo, n, False) for n in found])
        if name == "in progress":  # S-009-24: the pull requests waiting to land follow the work
            lines += _group("to land", [item(repo, n, True) for n in to_land(rows)])
    drafts = sorted((p.number for p in facts.pulls if p.draft), reverse=True)
    lines += _group("drafts", [item(repo, n, True) for n in drafts])
    lines += _group("sessions", session_values(facts))
    runs = of("RUNS_ACTIVE")
    shown = ", ".join(f"{r.subject} {r.detail.rsplit(', ', 1)[-1]}" for r in runs)
    lines += _group("runs", [f"{len(runs)}: {shown}"] if runs else [])
    lines += _group("other", [f"{r.state} {r.subject} {r.detail}" for r in rows if r.state not in PLACED])
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
    if not gate.launchd:
        value = "can't check here: no launchd"
    elif gate.job is None:
        value = "no launchd job on this Mac"
    else:
        job = gate.job
        problems = [p.shown for p in job_problems(gate, job, facts.repo, facts.now)]
        schedule = f"launchd every {ago(job.interval)}" if job.interval else "launchd"
        value = f"{'; '.join(problems) or 'OK'}, {schedule}"
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
    return [_line("gate", value), _line("mode", mode), _line("landing", landing), _line("github app", app)]


def shipmill_line(facts: Facts) -> str:
    """S-009-22: the CLI's version, the plugin's when it differs, and the updates"""
    latest, plugin = latest_shipmill(facts.rows)
    parts = [facts.cli]
    if any(v != facts.cli for v in re.findall(r"\d+\.\d+\.\d+[^\s,;]*", plugin)):
        parts.append(f"plugin {plugin}")
    fixes = [r.detail.partition("; ")[2] for r in facts.rows if r.state == "SHIPMILL_OUTDATED"]
    if cli_outdated(facts.cli, latest):
        fixes.append("uv tool upgrade shipmill")
    if fixes:
        parts.append(f"update available: {'; '.join(fixes)}")
    else:
        parts.append("up to date" if latest is not None else "latest not read")
    return _line("shipmill", ", ".join(parts))


def report(facts: Facts) -> Report:
    found, reasons = verdict(facts)
    open_ = sum(1 for i in facts.issues if i.state not in CLOSED_STATES)
    lines = [
        repo_line(facts),
        *item_lines(facts),
        count_line("open issues", open_, f"{github(facts.repo)}/issues"),
        count_line("pull requests", len(facts.pulls), f"{github(facts.repo)}/pulls"),
        *gate_lines(facts),
        shipmill_line(facts),
    ]
    return Report(found, reasons, lines)
