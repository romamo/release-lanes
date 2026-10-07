"""Spec 009: `shipmill status` says whether the factory works or is stuck"""

import dataclasses
import datetime as dt
import json
import subprocess
import sys
from pathlib import Path

import pytest

from shipmill.agents import AgentsConfig, Mode
from shipmill.app import Identity, default_key
from shipmill.cli import _parser, _status, main
from shipmill.status import (
    Described,
    Facts,
    Gate,
    Issue,
    Job,
    Main,
    Pull,
    Release,
    Row,
    Verdict,
    parse_job,
    parse_waketime,
    read_gate,
    report,
)
from tests import test_app

REPO = "acme/web"
NOW = dt.datetime(2026, 10, 7, 12, 0, tzinfo=dt.UTC)
SHA = "a" * 40
VERSION = Row("SHIPMILL_VERSION", "shipmill", "latest v0.26.0; plugin: user 0.26.0, local 0.26.0 (/x)", False)
AGENTS = AgentsConfig(prompt="/triage {repo}", prs=True, retry_hours=24, app_id=7, mode=Mode.HEADLESS)
APP = Identity(7, "acme-bot", 99, Path("/k.pem"))
JOB = Job(dt.timedelta(minutes=15), True, False, "0", NOW - dt.timedelta(minutes=4), "QUIET: nothing", False)
GATE = Gate(AGENTS, True, True, JOB, None, APP, None, None)


def facts(*rows: Row, **changes: object) -> Facts:
    base = Facts(
        repo=REPO,
        rows=[*rows, VERSION],
        issues=[],
        pulls=[],
        main=Main("main", SHA, SHA, 0, 0),
        release=Release("v0.26.0", NOW - dt.timedelta(days=1), Described("v0.26.0", 0), Described("v0.26.0", 3)),
        gate=GATE,
        cli="0.26.0",
        now=NOW,
    )
    return dataclasses.replace(base, **changes)  # type: ignore[arg-type]


def gate(**changes: object) -> Gate:
    return dataclasses.replace(GATE, **changes)  # type: ignore[arg-type]


def job(**changes: object) -> Job:
    return dataclasses.replace(JOB, **changes)  # type: ignore[arg-type]


def lines(found: Facts) -> list[str]:
    return report(found).text(REPO).splitlines()


def test_s009_1_the_verdict_line_comes_first_with_every_reason() -> None:
    found = facts(Row("BOT_FAILED", REPO, "release.yml failed", True), Row("HOLD", "#3", "freeze", False))
    text = lines(found)
    assert text[0] == f"{REPO}: STUCK"
    assert text[1:3] == [f"  - BOT_FAILED {REPO}: release.yml failed", "  - HOLD #3: freeze"]
    assert lines(facts())[:2] == [f"{REPO}: IDLE", "  - nothing to do"]


@pytest.mark.parametrize(
    "state",
    ["BOT_FAILED", "BOT_STALLED", "WORK_BRANCH_STALE", "NOT_PUBLISHED", "OPERATE_FAILED", "UNHEALTHY", "INCIDENT_OPEN"],
)
def test_s009_2_a_broken_pipeline_row_is_stuck(state: str) -> None:
    assert report(facts(Row(state, "x", "y", False))).verdict is Verdict.STUCK


@pytest.mark.parametrize(
    ("changed", "reason"),
    [
        (gate(job=job(loaded=False)), "installed but not loaded"),
        (gate(job=job(last_run=NOW - dt.timedelta(hours=1))), "the gate last ran 1 h ago, every 15 min"),
        (gate(job=job(last_exit="2")), "the gate's last run exited 2"),
        (gate(job=job(last="shipmill: the gate needs a clean checkout", failed=True)), "last tick failed"),
        (gate(job=job(last="UNCHANGED: same findings; retried after 2026-10-08T09:00")), "retried after"),
    ],
)
def test_s009_3_a_gate_that_isnt_ticking_is_stuck(changed: Gate, reason: str) -> None:
    found = report(facts(gate=changed))
    assert found.verdict is Verdict.STUCK
    assert any(reason in r for r in found.reasons)


def test_s009_3_a_gate_after_sleep_or_before_its_first_run_isnt_stuck() -> None:
    slept = gate(job=job(last_run=NOW - dt.timedelta(hours=8)), woke=NOW - dt.timedelta(minutes=2))
    assert report(facts(gate=slept)).verdict is Verdict.IDLE
    assert report(facts(gate=gate(job=job(last_exit="(never exited)")))).verdict is Verdict.IDLE
    assert report(facts(gate=gate(job=job(last_run=NOW - dt.timedelta(hours=8), running=True)))).verdict is Verdict.IDLE
    off_mac = gate(launchd=False, job=None)
    assert report(facts(gate=off_mac)).verdict is Verdict.IDLE
    assert "  job          no launchd on this host" in lines(facts(gate=off_mac))


def test_s009_3_no_job_on_this_host_waits_on_you_rather_than_stuck() -> None:
    found = report(facts(gate=gate(job=None)))
    assert found.verdict is Verdict.WAITS
    assert found.reasons == [f"no launchd job runs the gate on this host: shipmill launchd {REPO}"]


def test_s009_3_the_log_failure_after_a_decision_is_its_last_line() -> None:
    log = "QUIET: nothing needs an agent\n  pruned x\nInstalled 1 package in 1ms\nshipmill: watch_state.py failed\n"
    plist: dict[str, object] = {"StartInterval": 900}
    found = parse_job(plist, "\tstate = not running\n\tlast exit code = 2\n", log, NOW)
    assert (found.last, found.failed, found.last_exit, found.loaded, found.running) == (
        "shipmill: watch_state.py failed",
        True,
        "2",
        True,
        False,
    )
    assert parse_job(plist, None, None, None).loaded is False
    assert parse_waketime("{ sec = 1791314716, usec = 461743 } Tue Oct  6 22:25:16 2026") == dt.datetime(
        2026, 10, 6, 19, 25, 16, tzinfo=dt.UTC
    )
    assert parse_waketime("{ sec = 0, usec = 0 }") is None


def test_s009_3_a_traceback_shows_its_error_and_a_named_exit_code_is_still_a_number() -> None:
    log = "QUIET: nothing\nTraceback (most recent call last):\n  File \"x\", line 1, in <module>\nKeyError: 'agent'\n"
    found = parse_job({"StartInterval": 900}, "\tstate = not running\n\tlast exit code = 78: EX_CONFIG\n", log, NOW)
    assert (found.last, found.failed, found.last_exit) == ("KeyError: 'agent'", True, "78: EX_CONFIG")
    reasons = report(facts(gate=gate(job=found))).reasons
    assert "the gate's last run exited 78: EX_CONFIG" in reasons
    assert "the gate's last tick failed: KeyError: 'agent'" in reasons
    later = parse_job({}, None, log + "QUIET: nothing needs an agent\n", NOW)
    assert (later.last, later.failed) == ("QUIET: nothing needs an agent", False)


def test_s009_4_a_failed_app_check_is_stuck_and_shown() -> None:
    found = facts(gate=gate(app=None, app_error="the App isn't installed on acme/web"))
    assert report(found).verdict is Verdict.STUCK
    assert "  app          7, not connected: the App isn't installed on acme/web" in lines(found)
    assert "  app          acme-bot (7), connected" in lines(facts())


def test_s009_4_an_app_key_missing_on_this_host_is_not_a_failure(tmp_path: Path) -> None:
    (tmp_path / ".github").mkdir()
    (tmp_path / ".github" / "shipmill.toml").write_text('[agents]\nprompt = "/t"\napp_id = 7\n', encoding="utf-8")

    def app(app_id: int, key: Path) -> Identity:
        raise AssertionError("no key: the check isn't run")

    def run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
        raise AssertionError(cmd)

    found = read_gate(REPO, tmp_path, tmp_path / "home", "linux", run, app, 501)
    assert (found.app, found.app_error, found.mode_set) == (None, None, False)
    assert found.app_key is not None
    text = lines(facts(gate=found))
    assert any("can't check here: no key at" in line for line in text)
    assert "  mode         interactive (not set, the default)" in text
    assert report(facts(gate=found)).verdict is Verdict.IDLE


@pytest.mark.parametrize(
    "found",
    [
        facts(issues=[Issue(6, "NEEDS_DECISION")]),
        facts(Row("NEEDS_DECISION", REPO, "#9", False), pulls=[Pull(9, False)]),
        facts(gate=gate(job=job(last="WAITING: session s waits on you: claude attach s"))),
        *(facts(Row(s, "x", "y", False)) for s in ("HOLD", "PROMOTION_DUE", "POSTMORTEM_DUE", "UNTRUSTED")),
        facts(Row("SHIPMILL_OUTDATED", "plugin user", "0.25.0, latest v0.26.0; claude plugin update x", False)),
        facts(Row("BRANCH_DELETE_OFF", REPO, "turn it on", False)),
        facts(cli="0.25.0"),
        facts(gate=gate(agents=dataclasses.replace(AGENTS, app_id=None), app=None)),
    ],
)
def test_s009_5_what_a_person_owes_waits_on_you(found: Facts) -> None:
    assert report(found).verdict is Verdict.WAITS


def test_s009_6_agent_work_or_a_gate_session_or_a_run_is_working() -> None:
    assert report(facts(Row("ISSUES", REPO, "NEW #4", True))).verdict is Verdict.WORKING
    assert (
        report(facts(Row("RUNS_ACTIVE", "CI", "in_progress 1 min: push on main, u", False))).verdict is Verdict.WORKING
    )
    session = Row("AGENT_SESSION", "s1", "gate busy, started 1 min ago: shipmill acme/web", False)
    assert report(facts(session)).verdict is Verdict.WORKING
    headless = gate(job=job(last="RUNNING: session s is still working: tail -f x"))
    assert report(facts(gate=headless)).verdict is Verdict.WORKING
    mine = Row("AGENT_SESSION", "s2", "interactive busy, started 1 min ago: me", False)
    assert report(facts(mine)).verdict is Verdict.IDLE


def test_s009_7_issues_are_counted_and_listed_with_the_decision_link() -> None:
    issues = [
        Issue(6, "NEEDS_DECISION"),
        Issue(5, "NEW"),
        Issue(4, "NEEDS_PR"),
        Issue(3, "IN_PROGRESS"),
        Issue(2, "TRIAGED"),
        Issue(1, "SUSPECT_CLOSE"),
    ]
    text = lines(facts(issues=issues))
    start = text.index("issues       5 open")
    link = "https://github.com/acme/web/issues?q=is%3Aopen%20label%3Aneeds-decision"
    assert text[start + 1 : start + 6] == [
        f"  wait on you  #6  {link}",
        "  to triage    #5",
        "  to build     #4",
        "  in progress  #3",
        "  parked       #2",
    ]
    assert "  closed?      #1 closed by a mention, not a fix (SUSPECT_CLOSE)" in text


def test_s009_8_pull_requests_are_told_from_issues_waiting() -> None:
    rows = (Row("NEEDS_DECISION", REPO, "#6 #9", False), Row("PRS_OPEN", REPO, "#8", False))
    found = facts(*rows, issues=[Issue(6, "NEEDS_DECISION")], pulls=[Pull(9, False), Pull(8, False), Pull(7, True)])
    text = lines(found)
    start = text.index("pull requests  3 open")
    link = "https://github.com/acme/web/pulls?q=is%3Aopen%20label%3Aneeds-decision"
    assert text[start + 1 : start + 4] == [f"  wait on you  #9  {link}", "  to triage    #8", "  drafts       #7"]
    assert report(found).reasons == ["issue #6 waits on your decision", "pull request #9 waits on your decision"]


@pytest.mark.parametrize(
    ("main_", "shown"),
    [
        (Main("main", SHA, SHA, 0, 0), "in sync"),
        (Main("main", SHA, "b" * 40, 0, 2), "2 behind (git pull)"),
        (Main("main", SHA, "b" * 40, 1, 0), "1 ahead"),
        (Main("main", SHA, "b" * 40, 1, 2), "diverged (1 ahead, 2 behind)"),
        (Main("main", None, SHA, 0, 0), "no local main"),
    ],
)
def test_s009_9_the_main_line_compares_local_with_github(main_: Main, shown: str) -> None:
    assert any(line.startswith("  main ") and line.endswith(shown) for line in lines(facts(main=main_)))


def test_s009_10_the_version_line_names_the_release_and_where_each_main_is() -> None:
    assert (
        "  version      latest release v0.26.0 (1 h ago); local main at v0.26.0, github main at v0.26.0 +3 commits"
        in lines(facts(now=NOW - dt.timedelta(hours=23)))
    )
    assert "  version      no release yet" in lines(facts(release=None))


def test_s009_11_the_gate_block_shows_job_last_mode_and_app() -> None:
    text = lines(facts())
    start = text.index("gate")
    assert text[start + 1 : start + 5] == [
        "  job          launchd every 15 min, idle, last run 4 min ago, exit 0",
        "  last         QUIET: nothing",
        "  mode         headless",
        "  app          acme-bot (7), connected",
    ]
    assert "gate         no [agents]: no gate runs for this repo" in lines(facts(gate=gate(agents=None)))


def test_s009_12_the_shipmill_line_names_both_versions_and_the_updates() -> None:
    assert lines(facts())[-1] == "shipmill     cli 0.26.0, plugin user 0.26.0, local 0.26.0; latest v0.26.0, up to date"
    old = Row(
        "SHIPMILL_OUTDATED", "plugin user", "0.25.0, latest v0.26.0; claude plugin update shipmill@shipmill", False
    )
    assert lines(facts(old, cli="0.25.0"))[-1].endswith(
        "update available: claude plugin update shipmill@shipmill; uv tool upgrade shipmill"
    )


def test_s009_13_a_row_the_report_doesnt_place_is_printed_under_other() -> None:
    text = lines(facts(Row("WORKTREE_STALE", "/w", "kept 9 days", False)))
    assert text[text.index("other") + 1] == "  WORKTREE_STALE /w kept 9 days"


def test_s009_13_a_row_the_sessions_line_shows_is_not_under_other_too() -> None:
    text = lines(facts(Row("HOST_UNKNOWN", "claude", "not on PATH", False)))
    assert "  sessions     not read: not on PATH" in text
    assert "other" not in text


VERSION_JSON = {"state": VERSION.state, "subject": VERSION.subject, "detail": VERSION.detail, "agent": False}


class Fake:
    """watch_state.py, triage_state.py, and gh as the picture reads them"""

    def __init__(self, code: int) -> None:
        self.code = code
        self.seen: list[list[str]] = []

    def __call__(self, cmd: list[str]) -> subprocess.CompletedProcess[str]:
        self.seen.append(cmd)
        if cmd[1].endswith("watch_state.py"):
            rows = [{"state": "BOT_FAILED", "subject": REPO, "detail": "failed", "agent": True}, VERSION_JSON]
            out = "".join(json.dumps(r) + "\n" for r in rows) if "--json" in cmd else "BOT_FAILED table\n"
            return subprocess.CompletedProcess(cmd, self.code, out, "")
        if cmd[1].endswith("triage_state.py"):
            return subprocess.CompletedProcess(cmd, 0, '{"number": 4, "state": "NEW", "title": "t", "note": ""}\n', "")
        answers = {
            ("gh", "pr"): "[]",
            ("gh", "repo"): "main\n",
            ("gh", "release"): '{"tagName": "v1.0.0", "publishedAt": "2026-10-06T12:00:00Z"}',
        }
        return subprocess.CompletedProcess(cmd, 0, answers[(cmd[0], cmd[1])], "")


def repo_with_release(root: Path, repo: str = REPO) -> Path:
    def git(*args: str) -> None:
        subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)

    root.mkdir()
    git("init", "-q", "-b", "main")
    git("remote", "add", "origin", f"git@github.com:{repo}.git")
    git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "one")
    git("tag", "v1.0.0")
    git("update-ref", "refs/remotes/origin/main", "HEAD")
    return root.resolve()


@pytest.mark.parametrize("code", [0, 1])
def test_s009_14_rows_json_and_the_exit_code_stay_spec_008s(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], code: int
) -> None:
    root = repo_with_release(tmp_path / "repo")
    fake = Fake(code)
    assert main(["--repo", str(root), "status"], state=fake) == code
    out = capsys.readouterr().out
    assert out.startswith(f"{REPO}: STUCK\n  - BOT_FAILED {REPO}: failed\n")
    assert "  version      latest release v1.0.0 (" in out
    assert "  to triage    #4" in out
    assert main(["--repo", str(root), "status", "--rows"], state=Fake(code)) == code
    assert capsys.readouterr().out == "BOT_FAILED table\n"
    rows = Fake(code)
    assert main(["--repo", str(root), "status", "--json"], state=rows) == code
    assert capsys.readouterr().out.startswith('{"state": "BOT_FAILED"')
    assert len(rows.seen) == 1 and rows.seen[0][1].endswith("watch_state.py")
    assert any(c[0] == sys.executable and c[1].endswith("triage_state.py") for c in fake.seen)


def test_s009_15_with_the_app_the_reads_pass_its_bot_login(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    repo = test_app.REPO
    root = repo_with_release(tmp_path / "repo", repo)
    (root / ".github").mkdir()
    config = f'[agents]\nprompt = "/t"\napp_id = {test_app.APP_ID}\nmode = "headless"\n'
    (root / ".github" / "shipmill.toml").write_text(config, encoding="utf-8")
    home = tmp_path / "home"
    key = default_key(test_app.APP_ID, home)
    key.parent.mkdir(parents=True)
    key.write_text("not a real key\n", encoding="utf-8")
    key.chmod(0o600)
    fake = Fake(0)
    args = _parser().parse_args(["--repo", str(root), "status"])
    api, signer = test_app.FakeApi(), test_app.FakeSigner()
    assert _status(root, args, fake, api, signer, home=home, platform="linux", now=NOW) == 0
    reads = [c for c in fake.seen if c[1].endswith(("watch_state.py", "triage_state.py"))]
    assert len(reads) == 2
    for cmd in reads:
        assert cmd[-2:] == ["--bot-login", test_app.BOT]
        assert "--trusted-only" not in cmd
    assert f"  app          demo-agent ({test_app.APP_ID}), connected" in capsys.readouterr().out
    rows = Fake(0)
    assert _status(root, _parser().parse_args(["--repo", str(root), "status", "--rows"]), rows, api, signer) == 0
    assert "--bot-login" not in rows.seen[0]
