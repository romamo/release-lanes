#!/usr/bin/env python3
"""Report many repos' issue-to-release pipelines at once: ship-watch's states across a fleet.

Usage: fleet.py report --fleet FILE [--metrics --json]

A fleet file lists the repos, in TOML:

  [[repos]]
  repo = "romamo/shipyard"

  [[repos]]
  repo = "owner/other"
  incident_label = "sev"   # optional; the label that repo's incidents carry (default "incident")

The file is refused (exit 2, with a message naming the file and the problem) when it is
missing, isn't TOML, has no [[repos]], has an entry without `repo`, has a key other than
`repo` and `incident_label`, names a repo not in owner/name form, or lists a repo twice.
On Python 3.10, where tomllib doesn't exist, the file is read in the plain form above
([[repos]] tables of quoted strings, comments, blank lines) and anything else is refused.

report runs watch_state.py for each repo, on a fresh clone (`gh repo clone`, blobless, in a
temporary folder removed afterwards) with the repo's incident_label passed as
--incident-label, and prints one table: the repo, then each row's state, subject, and
detail as watch_state.py prints them. The rows watch_state.py counts as action come first,
across all repos, then the report-only rows, each group in the fleet file's order.

  --json      the same report as one JSON object: {"repos": [{"repo", "rows": [{"state",
              "subject", "detail"}]}]}, each repo's rows in watch_state.py's order
  --metrics   adds each repo's metrics.py --json measures (the 30 days to the run's
              start, every repo over the same window) as the repo's "metrics"; needs --json

A repo whose check fails (the clone, watch_state.py, or metrics.py exits 2: not found, no
access, a gh error) gets one REPO_ERROR row, its subject the step that failed and its
detail the error's first line; the other repos are still reported.

Exit 0 when no repo has an action row, 1 when any has, 2 after printing everything when a
repo's check failed, and 2 on a malformed fleet file. Reads only: the repairs stay with
each repo's own watch pass. Needs git, an authenticated gh, and uvx, as watch_state.py
does. Python 3.10+, standard library only.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import subprocess
import sys
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10: the plain form is read line by line instead
    tomllib = None

SCRIPTS = Path(__file__).resolve().parent
WATCH_STATE = SCRIPTS / "watch_state.py"
METRICS = SCRIPTS / "metrics.py"
# watch_state.py's ACTION: the states that make it exit 1 (tests/test_fleet.py keeps the two equal)
ACTION = frozenset(
    {
        "BOT_FAILED",
        "BOT_STALLED",
        "NOT_PUBLISHED",
        "UNANNOUNCED",
        "ISSUES",
        "OPERATE_FAILED",
        "UNHEALTHY",
        "PROMOTION_DUE",
        "INCIDENT_OPEN",
        "POSTMORTEM_DUE",
    }
)
REPO_ERROR = "REPO_ERROR"
ROW_KEYS = {"state", "subject", "detail"}  # a watch_state.py --json line
KEYS = ("repo", "incident_label")  # the keys of a [[repos]] entry
REPO = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]*/(?!\.+$)[A-Za-z0-9._-]+$")  # owner/name; a name isn't only dots
# the plain form Python 3.10 reads: a [[repos]] header or key = "string", each with an optional comment
PLAIN_HEADER = re.compile(r"^\[\[\s*repos\s*\]\]\s*(?:#.*)?$")
PLAIN_KEY = re.compile(r"""^([A-Za-z0-9_-]+)\s*=\s*(?:"([^"\\]*)"|'([^']*)')\s*(?:#.*)?$""")


class Refused(SystemExit):
    """Bad input: the message goes to stderr, and the run exits 2"""

    def __init__(self, message: str) -> None:
        super().__init__(2)
        self.message = message

    def __str__(self) -> str:
        return self.message


@dataclass(frozen=True)
class Entry:
    """One repo of the fleet"""

    repo: str
    incident_label: str | None  # None: the repo's own config decides (default "incident")


def parse_plain(text: str, path: Path) -> dict[str, Any]:
    """The plain form of a fleet file, read as tomllib would read it, for Python 3.10"""
    raw: dict[str, Any] = {}
    table = raw
    for number, line in enumerate(text.splitlines(), 1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if PLAIN_HEADER.match(stripped):
            table = {}
            raw.setdefault("repos", []).append(table)
            continue
        found = PLAIN_KEY.match(stripped)
        if found is None:
            raise Refused(
                f"error: {path}: line {number}: can't read {stripped!r} on Python 3.10: "
                'write [[repos]] tables of key = "string" lines, or use Python 3.11+'
            )
        key = found.group(1)
        if key in table:
            raise Refused(f"error: {path}: line {number}: {key} is set twice")
        table[key] = found.group(2) if found.group(2) is not None else found.group(3)
    return raw


def parse(text: str, path: Path) -> dict[str, Any]:
    """The fleet file as TOML: tomllib on Python 3.11+, the plain form on 3.10"""
    if tomllib is None:
        return parse_plain(text, path)
    try:
        return tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise Refused(f"error: {path}: not TOML: {exc}") from None


def entry(table: object, index: int, path: Path) -> Entry:
    """One [[repos]] table, checked"""
    where = f"error: {path}: repos[{index}]"
    if not isinstance(table, dict):
        raise Refused(f"{where} is not a table")
    unknown = sorted(set(table) - set(KEYS))
    if unknown:
        raise Refused(f"{where} has an unknown key {unknown[0]!r}: only repo and incident_label")
    if "repo" not in table:
        raise Refused(f"{where} has no repo")
    repo = table["repo"]
    if not isinstance(repo, str) or not REPO.match(repo):
        raise Refused(f"{where}: repo {repo!r} is not in owner/name form")
    label = table.get("incident_label")
    if label is not None and (not isinstance(label, str) or not label.strip()):
        raise Refused(f"{where}: incident_label must be a non-empty string")
    return Entry(repo, label)


def load(path: Path) -> list[Entry]:
    """The fleet file's repos, in order; Refused when the file is malformed"""
    if not path.is_file():
        raise Refused(f"error: {path}: no such file")
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise Refused(f"error: {path}: not TOML: {exc}") from None
    raw = parse(text, path)
    unknown = sorted(set(raw) - {"repos"})
    if unknown:
        raise Refused(f"error: {path}: unknown key {unknown[0]!r}: a fleet file holds only [[repos]] tables")
    tables = raw.get("repos")
    if not isinstance(tables, list) or not tables:
        raise Refused(f"error: {path}: no [[repos]]")
    entries = [entry(table, i, path) for i, table in enumerate(tables)]
    seen: set[str] = set()
    for e in entries:
        key = e.repo.lower()  # GitHub's owner and repo names ignore case
        if key in seen:
            raise Refused(f"error: {path}: lists {e.repo} twice")
        seen.add(key)
    return entries


def watch_command(e: Entry, checkout: Path) -> list[str]:
    """watch_state.py for one repo, on its checkout, as JSON lines"""
    cmd = [sys.executable, str(WATCH_STATE), e.repo, "--repo-dir", str(checkout), "--json"]
    if e.incident_label is not None:
        cmd += ["--incident-label", e.incident_label]
    return cmd


def clone_command(e: Entry, checkout: Path) -> list[str]:
    """A blobless clone of the repo's default branch, with its tags, as watch_state.py reads it"""
    return ["gh", "repo", "clone", e.repo, str(checkout), "--", "--filter=blob:none", "--quiet"]


def metrics_command(e: Entry, until: str) -> list[str]:
    """metrics.py for one repo over the 30 days to until, as JSON"""
    cmd = [sys.executable, str(METRICS), e.repo, "--json", "--until", until]
    if e.incident_label is not None:
        cmd += ["--incident-label", e.incident_label]
    return cmd


# -- the report ----------------------------------------------------------------------------

Runner = Callable[[Sequence[str]], "subprocess.CompletedProcess[str]"]


def run(cmd: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(list(cmd), capture_output=True, text=True, check=False)


@dataclass(frozen=True)
class Row:
    state: str
    subject: str
    detail: str

    def json(self) -> dict[str, str]:
        return {"state": self.state, "subject": self.subject, "detail": self.detail}


@dataclass(frozen=True)
class RepoReport:
    repo: str
    rows: tuple[Row, ...]
    metrics: dict[str, Any] | None  # metrics.py --json's object; None when not asked or failed

    @property
    def failed(self) -> bool:
        return any(r.state == REPO_ERROR for r in self.rows)

    def json(self, with_metrics: bool) -> dict[str, Any]:
        found: dict[str, Any] = {"repo": self.repo, "rows": [r.json() for r in self.rows]}
        if with_metrics:
            found["metrics"] = self.metrics
        return found


def error_row(step: str, proc: subprocess.CompletedProcess[str]) -> Row:
    """REPO_ERROR for a failed step: the first line of what it wrote to stderr"""
    lines = [line.strip() for line in (proc.stderr or "").splitlines() if line.strip()]
    return Row(REPO_ERROR, step, lines[0] if lines else f"{step} exited {proc.returncode}")


def decoded(text: str, what: str) -> object:
    """A script's JSON output; Refused when it isn't JSON (exit 2: a traceback's 1 reads as an action)"""
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        raise Refused(f"error: {what} printed {text[:200]!r}, not JSON") from None


def watch_rows(repo: str, out: str) -> list[Row]:
    """watch_state.py --json's lines, one row each"""
    rows = []
    for line in out.splitlines():
        found = decoded(line, f"watch_state.py for {repo}")
        if not isinstance(found, dict) or set(found) != ROW_KEYS:
            raise Refused(f"error: watch_state.py for {repo} printed {line!r}, not a row")
        rows.append(Row(str(found["state"]), str(found["subject"]), str(found["detail"])))
    return rows


def check(e: Entry, runner: Runner, workdir: Path, until: str | None) -> RepoReport:
    """One repo's watch rows (and metrics, given until), or its REPO_ERROR row"""
    checkout = workdir / e.repo
    checkout.parent.mkdir(parents=True, exist_ok=True)
    cloned = runner(clone_command(e, checkout))
    if cloned.returncode != 0:
        return RepoReport(e.repo, (error_row("gh repo clone", cloned),), None)
    watched = runner(watch_command(e, checkout))
    if watched.returncode not in (0, 1):
        return RepoReport(e.repo, (error_row("watch_state.py", watched),), None)
    rows = watch_rows(e.repo, watched.stdout)
    if until is None:
        return RepoReport(e.repo, tuple(rows), None)
    measured = runner(metrics_command(e, until))
    if measured.returncode != 0:
        return RepoReport(e.repo, (*rows, error_row("metrics.py", measured)), None)
    found = decoded(measured.stdout, f"metrics.py for {e.repo}")
    if not isinstance(found, dict) or not isinstance(found.get("measures"), list):
        raise Refused(f"error: metrics.py for {e.repo} printed no measures")
    return RepoReport(e.repo, tuple(rows), found)


def report(entries: list[Entry], runner: Runner, workdir: Path, until: str | None) -> list[RepoReport]:
    """Every repo of the fleet, in order; until (the metrics window's end) only with --metrics"""
    return [check(e, runner, workdir, until) for e in entries]


def leads(row: Row) -> bool:
    """A row the table puts first: an action, or a repo whose check failed"""
    return row.state in ACTION or row.state == REPO_ERROR


def table(reports: list[RepoReport]) -> list[str]:
    """One line per row, the repo named on each: actions (and failed checks) first, across all repos"""
    width = max(len(r.repo) for r in reports)
    rows = [(r.repo, row) for r in reports for row in r.rows]
    first = [x for x in rows if leads(x[1])]
    rest = [x for x in rows if not leads(x[1])]
    return [f"{repo:<{width}} {row.state:<14} {row.subject:<16} {row.detail}".rstrip() for repo, row in first + rest]


def exit_code(reports: list[RepoReport]) -> int:
    if any(r.failed for r in reports):
        return 2
    return 1 if any(row.state in ACTION for r in reports for row in r.rows) else 0


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0)  # noqa: UP017 (3.10 has no dt.UTC)


def arguments() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    rep = commands.add_parser("report", help="every repo's watch rows in one table")
    rep.add_argument("--fleet", required=True, type=Path, help="the fleet file")
    rep.add_argument("--metrics", action="store_true", help="add each repo's metrics.py measures (needs --json)")
    rep.add_argument("--json", action="store_true", help="one JSON object instead of a table")
    return parser


def main(argv: Sequence[str] | None = None, runner: Runner = run, clock: Callable[[], dt.datetime] = utc_now) -> int:
    parser = arguments()
    args = parser.parse_args(argv)
    if args.metrics and not args.json:
        parser.error("--metrics needs --json")
    entries = load(args.fleet)
    until = clock().isoformat() if args.metrics else None
    with tempfile.TemporaryDirectory(prefix="fleet-") as workdir:
        reports = report(entries, runner, Path(workdir), until)
    if args.json:
        print(json.dumps({"repos": [r.json(args.metrics) for r in reports]}, indent=2, sort_keys=True))
    else:
        print("\n".join(table(reports)))
    return exit_code(reports)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Refused as refused:
        sys.stderr.write(f"{refused}\n")
        raise
