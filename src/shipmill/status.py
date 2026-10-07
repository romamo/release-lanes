"""Spec 009: `shipmill status` as a picture of the factory. A verdict (STUCK, WAITS ON YOU,
WORKING, IDLE) with every reason, then one block per area. The facts come from
github-ship-watch's rows and a few reads of the checkout, GitHub, and this host's gate;
the verdict and the lines are worked out from them here, with nothing read"""

import datetime as dt
import enum
import json
import plistlib
import re
import subprocess
import sys
import urllib.parse
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from shipmill.agents import AgentsConfig, Mode
from shipmill.app import Identity, default_key
from shipmill.config import CONFIG_PATH, config_path, read
from shipmill.errors import ReleaseError
from shipmill.gate import StateRunner, skills_dir
from shipmill.gitrepo import Git
from shipmill.launchd import label
from shipmill.version import Version

DECISION_LABEL = "needs-decision"  # triage_state.py's DECISION_LABEL
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
TAG_ROWS = ("PUBLISHED", "PUBLISHING", "NOT_PUBLISHED", "NO_REGISTRY", "UNANNOUNCED")
# rows whose facts the blocks show in their own words; any other goes under "other" (S-009-13)
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
    "GATE_NO_APP",  # D-19's row; the app line says the same
}

# triage_state.py's issue states by the line they're listed on (S-009-7)
ISSUE_LINES = (
    ("wait on you", ("NEEDS_DECISION",)),
    ("to triage", ("NEW", "REVISIT", "SPEC_REFUSED", "UNFILLED", "DONE_NOT_CLOSED")),
    ("to build", ("NEEDS_PR", "UNBLOCKED")),
    ("in progress", ("IN_PROGRESS",)),
    ("parked", ("BLOCKED", "POSTPONED", "TRIAGED")),
    ("untrusted", ("UNTRUSTED",)),
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
            return "no version tag"
        return self.tag if not self.past else f"{self.tag} +{self.past} commits"


@dataclass(frozen=True, slots=True)
class Release:
    """GitHub's latest release, and the version each default branch is at"""

    tag: str
    published: dt.datetime
    local: Described | None  # None without a local branch
    remote: Described


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
    release: Release | None
    gate: Gate
    cli: str  # the installed CLI's version
    now: dt.datetime


@dataclass(frozen=True, slots=True)
class Report:
    verdict: Verdict
    reasons: list[str]
    lines: list[str]

    def text(self, repo: str) -> str:
        head = [f"{repo}: {self.verdict.text}", *(f"  - {r}" for r in self.reasons)]
        return "\n".join([*head, "", *self.lines]) + "\n"


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


def read_release(repo: str, git: Git, main: Main, run: StateRunner) -> Release | None:
    proc = run(["gh", "release", "view", "-R", repo, "--json", "tagName,publishedAt"])
    if proc.returncode != 0 and "release not found" in proc.stderr:
        return None
    found = json.loads(_checked(proc, "gh release view"))
    published = dt.datetime.fromisoformat(str(found["publishedAt"]).replace("Z", "+00:00"))
    local = None if main.local is None else describe(git, main.local)
    return Release(str(found["tagName"]), published, local, describe(git, main.remote))


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


def ago(span: dt.timedelta) -> str:
    minutes = int(span.total_seconds() // 60)
    if minutes < 60:
        return f"{max(minutes, 0)} min"
    if minutes < 48 * 60:
        return f"{minutes // 60} h"
    return f"{minutes // (24 * 60)} days"


def numbers(found: Sequence[int]) -> str:
    return " ".join(f"#{n}" for n in found) or "none"


def decision_link(repo: str, kind: str) -> str:
    query = urllib.parse.quote(f"is:open label:{DECISION_LABEL}")
    return f"https://github.com/{repo}/{kind}?q={query}"


def _row_numbers(rows: Sequence[Row], state: str) -> list[int]:
    return [int(n) for r in rows if r.state == state for n in re.findall(r"#(\d+)", r.detail)]


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


def gate_reasons(gate: Gate, repo: str, now: dt.datetime) -> tuple[list[str], list[str]]:
    """The gate's stuck reasons and its waits-on-you reasons (S-009-3, S-009-4, S-009-5)"""
    stuck: list[str] = []
    yours: list[str] = []
    if gate.agents is None:
        return stuck, yours
    if gate.launchd:
        job = gate.job
        if job is None:  # another host may run it: the report reads only this one
            yours.append(f"no launchd job runs the gate on this host: shipmill launchd {repo}")
        elif not job.loaded:
            stuck.append("the gate's launchd job is installed but not loaded")
        else:
            code = re.match(r"^-?\d+", job.last_exit or "")  # launchctl may add a name: "78: EX_CONFIG"
            if code is not None and int(code.group()) != 0:
                stuck.append(f"the gate's last run exited {job.last_exit}")
            if job.interval is not None and job.last_run is not None and not job.running:
                since = max(job.last_run, gate.woke) if gate.woke is not None else job.last_run
                if now - since > 2 * job.interval:
                    stuck.append(f"the gate last ran {ago(now - job.last_run)} ago, every {ago(job.interval)}")
            last = job.last or ""
            if job.failed:
                stuck.append(f"the gate's last tick failed: {last}")
            elif last.startswith("UNCHANGED"):
                stuck.append(f"the gate found the same work and won't retry yet: {last}")
            elif last.startswith("WAITING"):
                yours.append(f"the gate: {last}")
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
    working = [r.text() for r in rows if r.agent and r.state not in STUCK_ROWS]
    working += [
        f"session {r.subject}: {r.detail}" for r in rows if r.state == "AGENT_SESSION" and r.detail.startswith("gate ")
    ]
    job = facts.gate.job
    if job is not None and job.last is not None and job.last.startswith(("RUNNING", "LAUNCH")):
        working.append(f"the gate: {job.last}")  # a headless session isn't in `claude agents` (D-17)
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


def _line(label_: str, value: str, width: int = 12) -> str:
    return f"  {label_:<{width}} {value}"


def repo_lines(facts: Facts) -> list[str]:
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
    local = "none" if m.local is None else m.local[:7]
    lines = ["repo", _line(m.branch, f"local {local}, github {m.remote[:7]}: {state}")]
    rel = facts.release
    if rel is None:
        lines.append(_line("version", "no release yet"))
    else:
        local = "none" if rel.local is None else rel.local.text()
        released = ago(facts.now - rel.published)
        at = f"local {m.branch} at {local}, github {m.branch} at {rel.remote.text()}"
        lines.append(_line("version", f"latest release {rel.tag} ({released} ago); {at}"))
    bot = [f"{r.subject} {r.state.removeprefix('BOT_').lower()}" for r in facts.rows if r.state in RELEASE_ROWS]
    tags: dict[str, list[str]] = {}
    for r in facts.rows:
        if r.state in TAG_ROWS:
            tags.setdefault(r.state.lower().replace("_", " "), []).append(r.subject)
    tagged = [f"{' '.join(names)} {state}" for state, names in tags.items()]
    lines.append(_line("releases", "; ".join(bot + tagged) or "none"))
    return lines


def issue_lines(facts: Facts) -> list[str]:
    open_ = [i for i in facts.issues if i.state not in CLOSED_STATES]
    lines = [f"issues       {len(open_)} open"]
    for name, states in ISSUE_LINES:
        found = sorted((i.number for i in open_ if i.state in states), reverse=True)
        if name == "untrusted" and not found:
            continue
        value = numbers(found)
        if name == "wait on you" and found:
            value += f"  {decision_link(facts.repo, 'issues')}"
        lines.append(_line(name, value))
    suspect = sorted((i.number for i in facts.issues if i.state in CLOSED_STATES), reverse=True)
    if suspect:
        lines.append(_line("closed?", f"{numbers(suspect)} closed by a mention, not a fix (SUSPECT_CLOSE)"))
    return lines


def pull_lines(facts: Facts) -> list[str]:
    wait = waiting(facts)
    value = numbers(wait.pulls)
    if wait.pulls:
        value += f"  {decision_link(facts.repo, 'pulls')}"
    drafts = sorted((p.number for p in facts.pulls if p.draft), reverse=True)
    return [
        f"pull requests  {len(facts.pulls)} open",
        _line("wait on you", value),
        _line("to triage", numbers(_row_numbers(facts.rows, "PRS_OPEN"))),
        _line("drafts", numbers(drafts)),
    ]


def gate_lines(facts: Facts) -> list[str]:
    gate = facts.gate
    if gate.agents is None:
        return ["gate         no [agents]: no gate runs for this repo"]
    lines = ["gate"]
    job = gate.job
    if not gate.launchd:
        lines.append(_line("job", "no launchd on this host"))
    elif job is None:
        lines.append(_line("job", f"none: shipmill launchd {facts.repo}"))
    else:
        parts = [f"launchd every {ago(job.interval)}" if job.interval else "launchd"]
        if not job.loaded:
            parts.append("not loaded")
        else:
            parts.append("running now" if job.running else "idle")
        parts.append("never ran" if job.last_run is None else f"last run {ago(facts.now - job.last_run)} ago")
        if job.last_exit is not None:
            parts.append(f"exit {job.last_exit}")
        lines.append(_line("job", ", ".join(parts)))
        lines.append(_line("last", job.last or "no decision logged yet"))
    mode = str(gate.agents.mode) if gate.mode_set else f"{Mode.INTERACTIVE} (not set, the default)"
    lines.append(_line("mode", mode))
    if gate.agents.app_id is None:
        app = "none: sessions write as your gh login"
    elif gate.app_key is not None:
        app = f"{gate.agents.app_id}, can't check here: no key at {gate.app_key}"
    elif gate.app is not None:
        app = f"{gate.app.slug} ({gate.app.app_id}), connected"
    else:
        app = f"{gate.agents.app_id}, not connected: {gate.app_error}"
    lines.append(_line("app", app))
    sessions = [r for r in facts.rows if r.state == "AGENT_SESSION"]
    unknown = [r for r in facts.rows if r.state == "HOST_UNKNOWN"]
    if unknown:
        lines.append(_line("sessions", f"not read: {unknown[0].detail}"))
    else:
        shown = "; ".join(f"{r.detail.split(',')[0]} ({r.subject})" for r in sessions)
        lines.append(_line("sessions", f"{len(sessions)}: {shown}" if sessions else "none"))
    runs = [r for r in facts.rows if r.state == "RUNS_ACTIVE"]
    shown = "; ".join(f"{r.subject} {r.detail.split(',')[0]}" for r in runs)
    lines.append(_line("runs", f"{len(runs)} active: {shown}" if runs else "none active"))
    return lines


def shipmill_line(facts: Facts) -> str:
    latest, plugin = latest_shipmill(facts.rows)
    fixes = [r.detail.partition("; ")[2] for r in facts.rows if r.state == "SHIPMILL_OUTDATED"]
    if cli_outdated(facts.cli, latest):
        fixes.append("uv tool upgrade shipmill")
    state = f"update available: {'; '.join(fixes)}" if fixes else "up to date"
    return f"shipmill     cli {facts.cli}, plugin {plugin}; latest {latest or 'unknown'}, {state}"


def report(facts: Facts) -> Report:
    found, reasons = verdict(facts)
    lines = [*repo_lines(facts), *issue_lines(facts), *pull_lines(facts), *gate_lines(facts)]
    other = [r for r in facts.rows if r.state not in PLACED]
    if other:
        lines += ["other", *(_line(r.state, f"{r.subject} {r.detail}") for r in other)]
    lines.append(shipmill_line(facts))
    return Report(found, reasons, lines)
