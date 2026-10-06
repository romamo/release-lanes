"""The github-ship-watch skill's classification, without git, gh, or the network"""

import datetime as dt
import importlib.util
import json
import subprocess
import sys
import tomllib
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "skills" / "github-ship-watch" / "scripts" / "watch_state.py"
NOW = dt.datetime(2026, 10, 3, 12, 0, tzinfo=dt.timezone.utc)  # noqa: UP017 (runs under 3.10 too, as the script does)
GRACE = dt.timedelta(minutes=20)


@pytest.fixture(scope="module")
def ws() -> ModuleType:
    spec = importlib.util.spec_from_file_location("watch_state", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses look their module up by name
    spec.loader.exec_module(module)
    return module


def run(ws: ModuleType, status: str, conclusion: str, minutes_ago: int) -> object:
    return ws.Run(status, conclusion, NOW - dt.timedelta(minutes=minutes_ago), f"run-{minutes_ago}")


def states(rows: list[object]) -> list[str]:
    return [r.state for r in rows]  # type: ignore[attr-defined]


def test_a_failed_latest_run_is_reported(ws: ModuleType) -> None:
    runs = [run(ws, "completed", "failure", 5), run(ws, "completed", "success", 60)]
    assert states(ws.bot_rows(runs, None, NOW, GRACE, "release.yml")) == ["BOT_FAILED"]


def test_a_cancelled_settle_run_is_not_a_failure(ws: ModuleType) -> None:
    runs = [run(ws, "completed", "cancelled", 5), run(ws, "completed", "success", 60)]
    assert states(ws.bot_rows(runs, None, NOW, GRACE, "release.yml")) == ["BOT_OK"]


def test_a_success_after_a_failure_clears_it(ws: ModuleType) -> None:
    runs = [run(ws, "completed", "success", 5), run(ws, "completed", "failure", 60)]
    assert states(ws.bot_rows(runs, None, NOW, GRACE, "release.yml")) == ["BOT_OK"]


def test_a_due_release_with_no_run_is_stalled(ws: ModuleType) -> None:
    # the #6 case: a hand-started policy run cancelled the push run's wait, then skipped
    runs = [run(ws, "completed", "success", 40), run(ws, "completed", "cancelled", 41)]
    rows = ws.bot_rows(runs, "stable 0.3.0: main quiet for 40 min", NOW, GRACE, "release.yml")
    assert states(rows) == ["BOT_STALLED"]


@pytest.mark.parametrize(("status", "minutes_ago"), [("in_progress", 40), ("queued", 40), ("completed", 5)])
def test_a_due_release_with_a_live_or_recent_run_waits(ws: ModuleType, status: str, minutes_ago: int) -> None:
    runs = [run(ws, status, "success" if status == "completed" else "", minutes_ago)]
    assert states(ws.bot_rows(runs, "stable 0.3.0", NOW, GRACE, "release.yml")) == ["BOT_OK"]


def test_a_work_branch_no_run_owns_is_stale(ws: ModuleType) -> None:
    # #175: the run that pushed shipmill/v0.17.0 was cancelled before its cleanup got a runner
    runs = [run(ws, "completed", "success", 5), run(ws, "completed", "cancelled", 900)]
    rows = ws.work_branch_rows({"shipmill/v0.17.0": "a" * 40}, runs, "release.yml")
    assert [(r.state, r.subject, r.detail) for r in rows] == [
        ("WORK_BRANCH_STALE", "shipmill/v0.17.0", f"at {'a' * 12}, and no run of release.yml is queued or in progress")
    ]
    assert rows[0].json()["agent"] is True and "WORK_BRANCH_STALE" in ws.ACTION


@pytest.mark.parametrize("status", ["queued", "in_progress", "waiting"])
def test_a_work_branch_an_active_run_may_own_is_not_stale(ws: ModuleType, status: str) -> None:
    runs = [run(ws, status, "", 40), run(ws, "completed", "success", 60)]
    assert ws.work_branch_rows({"shipmill/v0.17.0": "a" * 40}, runs, "release.yml") == []


def test_the_stale_work_branch_repair_rechecks_every_unfinished_status(ws: ModuleType) -> None:
    # the agent deletes the branch: a re-check filtered to in_progress and queued misses a
    # run waiting on an environment, whose CI still uses the branch (#175); one unfiltered
    # listing misses an owner behind newer finished runs (#185)
    skill = (SCRIPT.parents[1] / "SKILL.md").read_text(encoding="utf-8")
    row = next(line for line in skill.splitlines() if line.startswith("| WORK_BRANCH_STALE |"))
    assert "gh run list -w <workflow> -s <status> --json url --jq length" in row
    assert all(f"`{status}`" in row for status in ws.UNFINISHED) and "prints `0`" in row


def gh_proc(code: int, out: str = "", err: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess([], code, out, err)


def listing(*runs: tuple[str, int]) -> str:
    return json.dumps(
        [
            {"status": s, "conclusion": "", "createdAt": (NOW - dt.timedelta(minutes=m)).isoformat(), "url": f"run-{m}"}
            for s, m in runs
        ]
    )


def test_an_owner_older_than_the_newest_runs_keeps_its_work_branch(ws: ModuleType) -> None:
    # #185: eleven newer finished runs pushed the owner out of the newest ten; asked by
    # status, the owner, waiting on an environment, is still found
    asked: list[list[str]] = []

    def gh(cmd: list[str]) -> Any:
        asked.append(cmd)
        status = cmd[cmd.index("-s") + 1]
        return gh_proc(0, listing(("waiting", 600)) if status == "waiting" else "[]")

    owners = ws.fetch_unfinished("o/r", "release.yml", gh)
    assert [r.url for r in owners] == ["run-600"]
    assert [c[c.index("-s") + 1] for c in asked] == [*ws.UNFINISHED, "in_progress"]
    assert all(c[:7] == ["gh", "run", "list", "-R", "o/r", "-w", "release.yml"] for c in asked)
    assert ws.work_branch_rows({"shipmill/v0.17.0": "a" * 40}, owners, "release.yml") == []


def test_a_run_seen_under_two_statuses_is_one_owner(ws: ModuleType) -> None:
    def gh(cmd: list[str]) -> Any:
        return gh_proc(0, listing(("in_progress", 5)) if "in_progress" in cmd else "[]")

    assert [r.url for r in ws.fetch_unfinished("o/r", "release.yml", gh)] == ["run-5"]


def test_a_failed_owner_query_stops_the_watch(ws: ModuleType, capsys: pytest.CaptureFixture[str]) -> None:
    # a failed query is not "no owner": that reading deletes a live run's branch
    def gh(cmd: list[str]) -> Any:
        return gh_proc(1, err="HTTP 502: Server Error") if "waiting" in cmd else gh_proc(0, "[]")

    with pytest.raises(SystemExit) as stopped:
        ws.fetch_unfinished("o/r", "release.yml", gh)
    assert stopped.value.code == 2
    assert "Server Error" in capsys.readouterr().err
    with pytest.raises(ws.Refused):
        ws.fetch_unfinished("o/r", "release.yml", lambda cmd: gh_proc(0, "{}"))


def test_only_release_work_branches_are_read(ws: ModuleType) -> None:
    assert ws.WORK_BRANCH.match("refs/heads/shipmill/v1.2.0rc1")[1] == "shipmill/v1.2.0rc1"
    for ref in ("refs/heads/shipmill", "refs/heads/shipmill/other", "refs/heads/main", "refs/tags/shipmill/v1.0.0"):
        assert ws.WORK_BRANCH.match(ref) is None


def test_publish_states(ws: ModuleType) -> None:
    old = ws.Tag("v1.2.0", NOW - dt.timedelta(hours=1))
    new = ws.Tag("v1.2.1", NOW - dt.timedelta(minutes=5))
    assert ws.publish_row(old, "pkg", True, NOW, GRACE).state == "PUBLISHED"
    assert ws.publish_row(old, "pkg", False, NOW, GRACE).state == "NOT_PUBLISHED"
    assert ws.publish_row(new, "pkg", False, NOW, GRACE).state == "PUBLISHING"
    assert ws.publish_row(old, None, None, NOW, GRACE).state == "NO_REGISTRY"


def test_bot_detection(ws: ModuleType, tmp_path: Path) -> None:
    assert ws.bot_workflow(tmp_path) is None
    (tmp_path / ".github" / "workflows").mkdir(parents=True)
    (tmp_path / ".github" / "shipmill.toml").write_text('mode = "release"\n')
    assert ws.policy_file(tmp_path) == Path(".github") / "shipmill.toml"
    with pytest.raises(SystemExit):
        ws.bot_workflow(tmp_path)
    (tmp_path / ".github" / "workflows" / "release-bot.yml").write_text("name: Release bot\n")
    assert ws.bot_workflow(tmp_path) == ("release-bot.yml", False)
    caller = "jobs:\n  prepare:\n    uses: romamo/shipmill/.github/workflows/prepare.yml@v0\n"
    (tmp_path / ".github" / "workflows" / "release.yml").write_text(caller)
    assert ws.bot_workflow(tmp_path) == ("release.yml", True)


def test_package_name_reads_the_project_table(ws: ModuleType, tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text('[tool.x]\nname = "not-it"\n\n[project]\nname = "pkg"\nversion = "1"\n')
    assert ws.package_name(tmp_path) == "pkg"


# -- holds and operations -------------------------------------------------------------------


def issue(ws: ModuleType, number: int, labels: tuple[str, ...] = (), body: str = "", **kw: object) -> object:
    fields: dict[str, object] = {
        "title": f"issue {number}",
        "created": NOW - dt.timedelta(hours=3),
        "author": "alice",
        "closing_prs": (),
        "closed": None,
        "state_reason": None,
    }
    fields.update(kw)
    return ws.Issue(
        number,
        fields["title"],
        body,
        fields["created"],
        fields["author"],
        labels,
        fields["closing_prs"],
        fields["closed"],
        fields["state_reason"],
    )


def deployment(ws: ModuleType, id_: int, tag: str, minutes_ago: int = 120) -> object:
    return ws.Deployment(id_, tag, f"sha-{tag}", NOW - dt.timedelta(minutes=minutes_ago))


def status(ws: ModuleType, id_: int, state: str, description: str, minutes_ago: int) -> object:
    return ws.Status(id_, state, description, NOW - dt.timedelta(minutes=minutes_ago))


def test_a_hold_names_who_opened_it_and_when(ws: ModuleType) -> None:
    issues = [issue(ws, 7, ("shipmill-hold",), title="Stop: bad migration", author="bob"), issue(ws, 8)]
    rows = ws.hold_rows(issues, NOW)
    assert [(r.state, r.subject) for r in rows] == [("HOLD", "#7")]
    assert rows[0].detail == "Stop: bad migration; opened by @bob 3 h ago (2026-10-03 09:00 UTC)"
    assert "HOLD" not in ws.ACTION  # a person stopped the factory on purpose


def test_an_incident_reports_its_age_and_linking_prs(ws: ModuleType) -> None:
    issues = [
        issue(ws, 3, ("incident",), created=NOW - dt.timedelta(days=2), closing_prs=(12,)),
        issue(ws, 4, ("sev1",), created=NOW - dt.timedelta(minutes=30)),
        issue(ws, 5, ("bug",)),
    ]
    assert [r.detail for r in ws.incident_rows(issues, "incident", NOW)] == ["issue 3; open 2 d, PR #12 links it"]
    rows = ws.incident_rows(issues, "sev1", NOW)
    assert [(r.state, r.subject, r.detail) for r in rows] == [
        ("INCIDENT_OPEN", "#4", "issue 4; open 30 min, no PR links it")
    ]


POSTMORTEM = """# 2026-10-02: staging served HTTP 503

Incident: romamo/shipmill#3
Incident: other/repo#4
"""


def test_a_closed_incident_without_a_postmortem_is_due(ws: ModuleType) -> None:
    closed = issue(ws, 3, ("incident",), title="staging 503", closed=NOW - dt.timedelta(hours=5))
    rows = ws.postmortem_rows([closed], "incident", [], "romamo/shipmill", NOW)
    assert [(r.state, r.subject) for r in rows] == [("POSTMORTEM_DUE", "#3")]
    assert rows[0].detail == (
        "staging 503; closed 5 h ago, no docs/postmortems/*.md names it (Incident: romamo/shipmill#3)"
    )
    assert "POSTMORTEM_DUE" in ws.ACTION


def test_a_postmortem_naming_the_incident_clears_it(ws: ModuleType) -> None:
    closed = [issue(ws, n, ("incident",), closed=NOW - dt.timedelta(days=1)) for n in (3, 4)]
    rows = ws.postmortem_rows(closed, "incident", [POSTMORTEM], "Romamo/Shipmill", NOW)
    # #4 is named only for another repo, so it is still due
    assert [r.subject for r in rows] == ["#4"]


def test_an_open_incident_is_not_due_yet(ws: ModuleType) -> None:
    still_open = issue(ws, 5, ("incident",))
    unlabelled = issue(ws, 6, ("bug",), closed=NOW)
    assert ws.postmortem_rows([still_open, unlabelled], "incident", [], "romamo/shipmill", NOW) == []


def test_a_postmortem_names_an_incident_only_on_its_own_line(ws: ModuleType) -> None:
    texts = ["See Incident: romamo/shipmill#7 inline", "Incident: romamo/shipmill#N", "Incident:romamo/shipmill#8\n"]
    assert ws.postmortem_named(texts, "romamo/shipmill") == {8}


def test_an_incident_closed_as_not_planned_or_a_duplicate_is_not_due(ws: ModuleType) -> None:
    closed = NOW - dt.timedelta(days=1)
    incidents = [
        issue(ws, n, ("incident",), closed=closed, state_reason=reason)
        for n, reason in ((3, "NOT_PLANNED"), (4, "DUPLICATE"), (5, "COMPLETED"), (6, ""), (7, None))
    ]
    rows = ws.postmortem_rows(incidents, "incident", [], "romamo/shipmill", NOW)
    assert [r.subject for r in rows] == ["#5", "#6", "#7"]


def test_a_postmortem_names_an_incident_by_url_or_plain_number(ws: ModuleType) -> None:
    text = (
        "Incident: https://github.com/romamo/shipmill/issues/9\n"
        "Incident: #10\n"
        "Incident: https://github.com/other/repo/issues/11\n"
        "Incident:\nromamo/shipmill#12\n"  # the line ends at the colon: names nothing
    )
    assert ws.postmortem_named([text], "romamo/shipmill") == {9, 10}


def test_only_a_missing_postmortems_folder_reads_as_none(ws: ModuleType, capsys: pytest.CaptureFixture[str]) -> None:
    def gh(code: int, out: str = "", err: str = "") -> Any:
        return ws.subprocess.CompletedProcess([], code, out, err)

    assert ws.postmortem_paths(gh(1, err="gh: Not Found (HTTP 404)"), "o/r", "main") == []
    listing = '[{"type": "file", "name": "a.md", "path": "docs/postmortems/a.md"},'
    listing += ' {"type": "file", "name": "a.txt", "path": "docs/postmortems/a.txt"},'
    listing += ' {"type": "dir", "name": "b.md", "path": "docs/postmortems/b.md"}]'
    assert ws.postmortem_paths(gh(0, listing), "o/r", "main") == ["docs/postmortems/a.md"]
    with pytest.raises(SystemExit) as stopped:
        ws.postmortem_paths(gh(1, err="gh: No commit found for the ref main (HTTP 404)"), "o/r", "main")
    assert stopped.value.code == 2
    assert "No commit found" in capsys.readouterr().err
    with pytest.raises(SystemExit):
        ws.postmortem_paths(gh(1, err="gh: Server Error (HTTP 502)"), "o/r", "main")
    with pytest.raises(ws.Refused):
        ws.postmortem_paths(gh(0, '{"type": "file"}'), "o/r", "main")


def test_the_template_names_no_incident(ws: ModuleType) -> None:
    template = SCRIPT.parents[3] / "docs" / "postmortems" / "TEMPLATE.md"
    text = template.read_text(encoding="utf-8")
    assert "Incident: owner/repo#N" in text
    assert ws.postmortem_named([text], "owner/repo") == set()


def test_the_report_leads_with_incidents_and_holds(ws: ModuleType) -> None:
    rows = [ws.Row(s, "x", "") for s in ("BOT_OK", "HOLD", "UNHEALTHY", "INCIDENT_OPEN", "ISSUES", "HOLD")]
    assert states(ws.ordered(rows)) == ["INCIDENT_OPEN", "HOLD", "HOLD", "BOT_OK", "UNHEALTHY", "ISSUES"]


def test_a_failed_operate_run_is_reported(ws: ModuleType) -> None:
    failed = [run(ws, "completed", "failure", 5), run(ws, "completed", "success", 15)]
    assert states(ws.operate_rows(failed, "operate.yml")) == ["OPERATE_FAILED"]
    cancelled = [run(ws, "completed", "cancelled", 5), run(ws, "completed", "success", 15)]
    assert ws.operate_rows(cancelled, "operate.yml") == []
    assert ws.operate_rows([run(ws, "in_progress", "", 1), failed[1]], "operate.yml") == []


def test_the_current_deployment_is_the_newest_that_succeeded(ws: ModuleType) -> None:
    deployments = [deployment(ws, 3, "v1.3.0"), deployment(ws, 2, "refs/tags/v1.2.0")]
    statuses = {3: [status(ws, 30, "failure", "deploy failed", 60)], 2: [status(ws, 20, "success", "", 200)]}
    current = ws.current_deployment(deployments, statuses.__getitem__)
    assert current is not None and current.tag == "v1.2.0"
    assert ws.current_deployment(deployments[:1], statuses.__getitem__) is None


def test_an_environment_whose_newest_health_status_failed_is_unhealthy(ws: ModuleType) -> None:
    sick = ws.Current(
        deployment(ws, 2, "v1.2.0"),
        (
            status(ws, 1, "success", "", 200),
            status(ws, 2, "success", "shipmill health: healthy", 100),
            status(ws, 3, "failure", "shipmill health: HTTP 503", 25),
        ),
    )
    row = ws.unhealthy_row("staging", sick, NOW)
    assert (row.state, row.subject, row.detail) == ("UNHEALTHY", "staging", "v1.2.0: HTTP 503, since 25 min ago")
    recovered = ws.Current(sick.deployment, (*sick.statuses, status(ws, 4, "success", "shipmill health: healthy", 5)))
    assert ws.unhealthy_row("staging", recovered, NOW) is None
    # a failure someone else recorded isn't a shipmill health check
    other = ws.Current(sick.deployment, (status(ws, 1, "success", "", 200), status(ws, 2, "failure", "smoke", 5)))
    assert ws.unhealthy_row("staging", other, NOW) is None
    assert ws.unhealthy_row("staging", None, NOW) is None


PROPOSAL_BODY = "<!-- shipmill:propose deploy=production -->\n<!-- shipmill:tag=v1.2.0 -->\nshipmill would deploy"


def test_an_open_proposal_is_due_with_its_approve_command(ws: ModuleType) -> None:
    issues = [issue(ws, 9, body=PROPOSAL_BODY), issue(ws, 10, body="<!-- shipmill:propose lane=stable -->")]
    rows = ws.proposal_rows(issues, "operate.yml", held=False)
    assert [(r.state, r.subject) for r in rows] == [("PROMOTION_DUE", "production")]
    assert rows[0].detail == "#9 v1.2.0: gh workflow run operate.yml -f approve=production -f dry-run=false"
    held = ws.proposal_rows(issues, "operate.yml", held=True)
    assert held[0].detail.startswith("#9 v1.2.0: close the shipmill-hold issues, then gh workflow run")


def test_proposals_are_found_by_their_label_and_by_title(ws: ModuleType) -> None:
    labelled = [issue(ws, 9, labels=("shipmill-proposal",), body=PROPOSAL_BODY)]
    old = issue(ws, 4, body=PROPOSAL_BODY.replace("production", "staging"))  # opened before the label

    # an unlabelled proposal shows beside a labelled one; one found both ways shows once
    assert [i.number for i in ws.proposal_issues(labelled, lambda: [labelled[0], old])] == [9, 4]
    assert [i.number for i in ws.proposal_issues([], lambda: [old])] == [4]


def baked_source(ws: ModuleType, minutes_ago: int, *health: object) -> Any:
    return ws.Current(deployment(ws, 5, "v1.2.0"), (status(ws, 50, "success", "", minutes_ago), *health))


IDLE = "no workflow calls shipmill's operate.yml"


def due(
    ws: ModuleType,
    env: object,
    source: object,
    target: object = None,
    deployments: tuple[object, ...] = (),
    idle: str | None = IDLE,
    held: bool = False,
) -> Any:
    return ws.unpromoted_row(env, source, target, list(deployments), idle, held, "run it", NOW)


def on(ws: ModuleType, tag: str) -> Any:
    return ws.Current(deployment(ws, 1, tag), (status(ws, 10, "success", "", 300),))


def test_a_baked_source_nothing_promotes_is_due_while_operate_is_idle(ws: ModuleType) -> None:
    prod = ws.Environment("production", "staging", 60)
    source = baked_source(ws, 90)
    old = on(ws, "v1.1.0")
    row = due(ws, prod, source, old, (old.deployment,))
    assert (row.state, row.subject) == ("PROMOTION_DUE", "production")
    assert (
        row.detail
        == f"operate would promote v1.2.0 to production (v1.2.0 healthy on staging for 90 min), but {IDLE}; run it"
    )
    assert due(ws, prod, source) is not None  # nothing deployed there yet
    # operate is running: its proposal or its deploy is the signal
    assert due(ws, prod, source, idle=None) is None
    # still baking, unhealthy, or tried on production already
    assert due(ws, prod, baked_source(ws, 30)) is None
    sick = baked_source(ws, 90, status(ws, 51, "failure", "shipmill health: HTTP 500", 10))
    assert due(ws, prod, sick) is None
    assert due(ws, prod, source, deployments=(ws.Deployment(9, "refs/tags/v1.2.0", "other", NOW),)) is None
    # a lane environment is never promoted, nor a source whose ref names no release
    assert due(ws, ws.Environment("staging", None, 0), source) is None
    assert due(ws, prod, ws.Current(deployment(ws, 5, "main"), source.statuses)) is None


@pytest.mark.parametrize("held", [False, True])
def test_observe_is_never_due_without_operate(ws: ModuleType, held: bool) -> None:
    assert due(ws, ws.Environment("production", "staging", 60, "observe"), baked_source(ws, 90), held=held) is None


HEALTHY = "v1.2.0 healthy on staging for 90 min"
APPROVE = "approve with `shipmill operate --approve production` once it runs"


def test_propose_with_operate_idle_says_operate_would_propose(ws: ModuleType) -> None:
    # with operate idle no proposal issue ever opens, so this row is the only signal
    row = due(ws, ws.Environment("production", "staging", 60, "propose"), baked_source(ws, 90))
    want = f"operate would propose promoting v1.2.0 to production ({APPROVE}; {HEALTHY}), but {IDLE}; run it"
    assert (row.state, row.subject, row.detail) == ("PROMOTION_DUE", "production", want)


@pytest.mark.parametrize("level", ["act", "propose"])
def test_a_hold_with_operate_idle_says_operate_would_propose(ws: ModuleType, level: str) -> None:
    row = due(ws, ws.Environment("production", "staging", 60, level), baked_source(ws, 90), held=True)
    after = f"{APPROVE} after the shipmill-hold issues close"
    want = f"operate would propose promoting v1.2.0 to production ({after}; {HEALTHY}), but {IDLE}; run it"
    assert (row.state, row.subject, row.detail) == ("PROMOTION_DUE", "production", want)


@pytest.mark.parametrize("tag", ["v1.2.0", "v1.2.1", "v1.3.0rc1", "v2.0.0.dev3"])
def test_a_target_on_or_ahead_of_the_source_is_not_due(ws: ModuleType, tag: str) -> None:
    prod = ws.Environment("production", "staging", 60)
    assert due(ws, prod, baked_source(ws, 90), on(ws, tag)) is None


@pytest.mark.parametrize("tag", ["v1.1.9", "v1.2.0rc3", "v1.2.0.dev1", "main"])
def test_a_target_behind_the_source_is_due(ws: ModuleType, tag: str) -> None:
    prod = ws.Environment("production", "staging", 60)
    assert due(ws, prod, baked_source(ws, 90), on(ws, tag)) is not None


def test_a_failed_health_check_restarts_the_bake(ws: ModuleType) -> None:
    prod = ws.Environment("production", "staging", 60)
    failed = status(ws, 51, "failure", "shipmill health: HTTP 500", 70)
    # the first health status after the failure starts the bake again, as operate counts it
    recent = baked_source(ws, 300, failed, status(ws, 52, "in_progress", "shipmill health: baking for 60 min", 40))
    assert ws.bake_start(recent) == NOW - dt.timedelta(minutes=40)
    assert due(ws, prod, recent) is None
    long_ago = baked_source(ws, 300, failed, status(ws, 52, "success", "shipmill health: baked", 65))
    assert "(v1.2.0 healthy on staging for 65 min)" in due(ws, prod, long_ago).detail
    assert ws.bake_start(baked_source(ws, 300, failed)) is None


def test_operate_idle_reasons(ws: ModuleType) -> None:
    assert ws.operate_idle(None, [], NOW) == "no workflow calls shipmill's operate.yml"
    assert ws.operate_idle(("operate.yml", False), [], NOW) == "operate.yml has no schedule"
    assert ws.operate_idle(("operate.yml", True), [], NOW) == "operate.yml has never run"
    stale = [run(ws, "completed", "success", 180)]
    assert ws.operate_idle(("operate.yml", True), stale, NOW) == "operate.yml last ran 3 h ago"
    assert ws.operate_idle(("operate.yml", True), [run(ws, "completed", "success", 8)], NOW) is None


def test_the_operate_caller_is_found(ws: ModuleType, tmp_path: Path) -> None:
    workflows = tmp_path / ".github" / "workflows"
    workflows.mkdir(parents=True)
    assert ws.operate_caller(tmp_path) is None
    (workflows / "operate.yml").write_text("on:\n  workflow_call:\njobs:\n  operate:\n    runs-on: x\n")
    assert ws.operate_caller(tmp_path) is None  # shipmill's own reusable workflow calls nothing
    uses = "uses: romamo/shipmill/.github/workflows/operate.yml@v0"
    caller = f"on:\n  workflow_dispatch:\njobs:\n  operate:\n    {uses}\n"
    (workflows / "ops.yml").write_text(caller)
    assert ws.operate_caller(tmp_path) == ("ops.yml", False)
    (workflows / "ops.yml").write_text(caller.replace("on:\n", 'on:\n  schedule:\n    - cron: "*/10 * * * *"\n'))
    assert ws.operate_caller(tmp_path) == ("ops.yml", True)


CONFIG = """mode = "release"

[lanes.rc]
schedule = ["Mon 07:00 UTC"]

# [environments.example]
[environments.staging]
lane = "rc"
workflow = "deploy.yml"

[environments."production"]   # promoted
from = "staging"
workflow = "deploy.yml"
bake_minutes = 60

[[version_lines]]
file = "README.md"
"""


def test_environments_and_the_incident_label_come_from_the_config(ws: ModuleType) -> None:
    policy = Path(".github/shipmill.toml")
    want = [ws.Environment("staging", None, 0), ws.Environment("production", "staging", 60)]
    for envs, label in (
        (ws.environments_310(CONFIG, policy), ws.incident_label_310(CONFIG, policy)),
        (ws.config(CONFIG, policy).environments, ws.config(CONFIG, policy).incident_label),
    ):
        assert envs == want
        assert label == "incident"
    assert ws.environments_310('mode = "release"\n', policy) == []
    assert ws.environments_310(CONFIG.replace("# [environments.example]", "[environments]"), policy) == want
    sev1 = CONFIG + "\n[operate]\nincident_label = 'sev1'\n"
    assert ws.incident_label_310(sev1, policy) == "sev1"
    assert ws.config('mode = "release"\n', policy) == ws.Config([], "incident")


INLINE = """mode = "release"

[environments]
staging = { lane = "rc", workflow = "deploy.yml" }
production = { from = "staging", workflow = "deploy.yml", bake_minutes = 60 }
"""


@pytest.mark.skipif(sys.version_info < (3, 11), reason="tomllib is 3.11+")
@pytest.mark.parametrize(
    "text",
    [
        INLINE,
        'environments.staging.lane = "rc"\nenvironments.production = { from = "staging", bake_minutes = 60 }\n'
        + "operate.incident_label = 'sev1'\n",
    ],
)
def test_every_form_shipmill_accepts_is_read_on_311(ws: ModuleType, text: str) -> None:
    read = ws.config(text, Path(".github/shipmill.toml"))
    assert read.environments == [ws.Environment("staging", None, 0), ws.Environment("production", "staging", 60)]
    assert read.incident_label == ("sev1" if "sev1" in text else "incident")


AUTONOMY = [
    '[autonomy]\nrelease = "act"\ndeploy.production = "observe"  # by hand\n',
    "[autonomy.deploy]\n# by hand\nproduction = 'observe'\n",
]


@pytest.mark.parametrize("section", AUTONOMY)
def test_deploy_autonomy_comes_from_the_config(ws: ModuleType, section: str) -> None:
    policy = Path(".github/shipmill.toml")
    assert ws.deploy_autonomy_310(CONFIG + section, policy) == {"production": "observe"}
    read = ws.config(CONFIG + section, policy)
    assert [e.deploy for e in read.environments] == ["act", "observe"]
    with pytest.raises(SystemExit, match="must be one of") as refused:
        ws.config(CONFIG + section.replace("observe", "never"), policy)
    assert refused.value.code == 2


@pytest.mark.parametrize(
    "config", ['autonomy.deploy.production = "observe"\n', '[autonomy]\ndeploy = { production = "observe" }\n']
)
def test_the_310_fallback_refuses_autonomy_it_cannot_read(ws: ModuleType, config: str) -> None:
    with pytest.raises(SystemExit, match=r"can't read \[autonomy\] deploy on Python 3\.10"):
        ws.deploy_autonomy_310(config, Path(".github/shipmill.toml"))


@pytest.mark.parametrize(
    "config",
    [
        INLINE,
        'environments.staging.lane = "rc"\n',
        "[environments.a.b]\n",
        "[environments.prod]\nfrom = 'staging'\nbake_minutes = 1_000\n",
    ],
)
def test_the_310_fallback_refuses_what_it_cannot_read(ws: ModuleType, config: str) -> None:
    with pytest.raises(SystemExit, match=r"can't read \[environments\] on Python 3\.10: use 3\.11\+") as refused:
        ws.environments_310(config, Path(".github/shipmill.toml"))
    assert refused.value.code == 2


def test_the_issue_states_reported_are_the_ones_triage_state_acts_on(ws: ModuleType) -> None:
    spec = importlib.util.spec_from_file_location("triage_state", ws.TRIAGE_STATE)
    assert spec is not None and spec.loader is not None
    triage_state = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(triage_state)
    assert ws.TRIAGE_ACTION == triage_state.ACTION


AGENT_ROWS = [
    "BOT_FAILED",
    "BOT_STALLED",
    "WORK_BRANCH_STALE",
    "NOT_PUBLISHED",
    "UNANNOUNCED",
    "ISSUES",
    "OPERATE_FAILED",
    "INCIDENT_OPEN",
]
REPORT_ROWS = [
    "PROMOTION_DUE",
    "UNHEALTHY",
    "HOLD",
    "PRS_OPEN",
    "ISSUES_OPEN",
    "RUNS_ACTIVE",
    "BRANCH_DELETE_OFF",
    "POSTMORTEM_DUE",
    "BOT_OK",
    "PUBLISHED",
    "PUBLISHING",
    "WORKTREE_STALE",
    "NEEDS_DECISION",
    "UNTRUSTED",
]


@pytest.mark.parametrize("state", AGENT_ROWS + REPORT_ROWS)
def test_each_json_row_says_whether_it_needs_an_agent(ws: ModuleType, state: str) -> None:
    row = ws.Row(state, "o/r", "d")
    assert row.json() == {"state": state, "subject": "o/r", "detail": "d", "agent": state in AGENT_ROWS}
    assert row.text() == f"{state:<14} o/r              d"  # the table is unchanged


def test_agent_rows_are_action_rows(ws: ModuleType) -> None:
    assert set(AGENT_ROWS) == ws.AGENT
    assert ws.AGENT <= ws.ACTION


def worktree(path: str, verdict: str, reason: str | None, age_hours: int | None) -> dict[str, object]:
    """One row of `shipmill worktrees --json`"""
    return {
        "path": path,
        "branch": None,
        "head": "0" * 40,
        "verdict": verdict,
        "reason": reason,
        "created": None,
        "age_hours": age_hours,
    }


def test_s002_17_only_kept_shipmill_worktrees_over_seven_days_report(ws: ModuleType) -> None:
    week = 7 * 24
    trees = [
        worktree(".", "KEPT", "main checkout", None),
        worktree("tmp/shipmill-gate", "KEPT", "current checkout", week * 4),
        worktree("tmp/mine", "KEPT", "not a shipmill worktree", week * 4),
        worktree(".claude/worktrees/done", "REMOVABLE", None, week * 4),
        worktree(".claude/worktrees/week", "KEPT", "detached HEAD", week),
        worktree("tmp/wt-old", "KEPT", "2 commit(s) not landed", week + 1),
        worktree(".claude/worktrees/pr", "KEPT", "open PR #7", week * 3),
    ]
    rows = ws.stale_rows(json.dumps({"worktrees": trees}))
    assert [(r.state, r.subject, r.detail) for r in rows] == [
        ("WORKTREE_STALE", "tmp/wt-old", "2 commit(s) not landed; created 7d ago"),
        ("WORKTREE_STALE", ".claude/worktrees/pr", "open PR #7; created 21d ago"),
    ]
    assert dt.timedelta(days=7) == ws.STALE
    assert "WORKTREE_STALE" not in ws.ACTION | ws.AGENT  # report-only: never exits 1, never starts a session


@pytest.mark.parametrize(
    "text",
    [
        "not json",
        "[]",
        '{"trees": []}',
        json.dumps({"worktrees": [{"path": "tmp/wt-a"}]}),
        json.dumps({"worktrees": [worktree("tmp/wt-a", "KEPT", None, 500)]}),
        json.dumps({"worktrees": [worktree("tmp/wt-a", "KEPT", "detached HEAD", None)]}),
    ],
)
def test_s002_17_a_worktrees_report_it_cannot_read_stops_the_watch(ws: ModuleType, text: str) -> None:
    with pytest.raises(SystemExit) as refused:
        ws.stale_rows(text)
    assert refused.value.code == 2


SKILLS = Path(__file__).resolve().parents[1] / "skills"


def skill_text(name: str) -> str:
    """A skill's SKILL.md with its line wrapping folded, so a statement reads as one line"""
    return " ".join((SKILLS / name / "SKILL.md").read_text(encoding="utf-8").split())


def test_s002_18_the_skills_document_the_report_and_the_gates_prune() -> None:
    watch = skill_text("github-ship-watch")
    assert "| WORKTREE_STALE |" in watch
    assert "| `false` | WORKTREE_STALE |" in watch
    gate = skill_text("shipmill-setup").split("## The gate", 1)[1].split(" ## ", 1)[0]
    assert "prunes landed worktrees" in gate
    assert "`pruned <path> (<branch>)`" in gate
    assert "worktrees` lists every worktree as REMOVABLE or KEPT" in gate
    for name in ("github-issue-resolve", "github-pr-triage", "github-issue-triage"):
        text = skill_text(name)
        assert "leaves behind once it exits" in text, name
        assert "the gate's prune's to remove (D-12)" in text, name


def triage_line(number: int, state: str) -> str:
    return json.dumps({"number": number, "state": state, "title": "t", "note": ""})


def issue_states(ws: ModuleType, code: int, lines: list[str]) -> list[tuple[str, str]]:
    rows = ws.intake("o/r", code, "\n".join(lines), [], lambda n: [])
    return [(r.state, r.detail) for r in rows]


def test_the_open_issues_triage_owes_nothing_are_reported_by_state(ws: ModuleType) -> None:
    # #182: a status report left out the triaged issues; they read ISSUES_OPEN, never an action
    lines = [
        triage_line(n, s) for n, s in [(175, "IN_PROGRESS"), (164, "BLOCKED"), (26, "TRIAGED"), (161, "IN_PROGRESS")]
    ]
    want = [("ISSUES_OPEN", "BLOCKED #164; IN_PROGRESS #175 #161; TRIAGED #26")]
    assert issue_states(ws, 0, lines) == want
    assert "ISSUES_OPEN" not in ws.ACTION


def test_issues_triage_acts_on_or_that_wait_keep_their_own_rows(ws: ModuleType) -> None:
    lines = [triage_line(12, "NEW"), triage_line(9, "BLOCKED"), triage_line(4, "NEEDS_DECISION")]
    lines.append(triage_line(5, "UNTRUSTED"))
    assert issue_states(ws, 1, lines) == [
        ("ISSUES", "NEW #12"),
        ("ISSUES_OPEN", "BLOCKED #9"),
        ("NEEDS_DECISION", "#4"),
        ("UNTRUSTED", "#5"),
    ]


def test_no_open_issues_print_no_issue_rows(ws: ModuleType) -> None:
    assert issue_states(ws, 0, []) == []


def active(ws: ModuleType, workflow: str, status: str, minutes_ago: int) -> object:
    created = NOW - dt.timedelta(minutes=minutes_ago)
    return ws.ActiveRun(workflow, status, "push", "main", created, f"https://run/{minutes_ago}")


def test_queued_and_running_workflow_runs_are_reported_oldest_first(ws: ModuleType) -> None:
    # #182: a run in progress never showed; each active run of any workflow reads RUNS_ACTIVE
    runs = [
        active(ws, "CI", "queued", 1),
        active(ws, "Release", "completed", 3),
        active(ws, "Deploy", "in_progress", 7),
    ]
    rows = ws.active_rows(runs, NOW)
    assert [(r.state, r.subject, r.detail) for r in rows] == [
        ("RUNS_ACTIVE", "Deploy", "in_progress 7 min: push on main, https://run/7"),
        ("RUNS_ACTIVE", "CI", "queued 1 min: push on main, https://run/1"),
    ]
    assert "RUNS_ACTIVE" not in ws.ACTION and "RUNS_ACTIVE" not in ws.AGENT


def test_finished_runs_print_no_active_rows(ws: ModuleType) -> None:
    assert ws.active_rows([active(ws, "CI", "completed", 2)], NOW) == []


AGENTS_CONFIG = """name = "demo"

[agents]
prompt = "/github-issue-triage {repo} triage the new issues; merge when green"  # the gate's
prs = true
retry_hours = 24
"""


def test_the_triage_mode_is_the_agents_table_prompt_first(ws: ModuleType) -> None:
    # #182: a status report says what the gate's sessions are told to do
    row = ws.triage_mode_row(AGENTS_CONFIG, Path("shipmill.toml"))
    assert (row.state, row.subject) == ("TRIAGE_MODE", "[agents]")
    prompt = '"/github-issue-triage {repo} triage the new issues; merge when green"'
    assert row.detail == f"prompt = {prompt}; prs = true; retry_hours = 24"


def test_the_310_fallback_reads_the_agents_table_as_tomllib_does(ws: ModuleType) -> None:
    policy = Path("shipmill.toml")
    want = ws.agents_table_310(AGENTS_CONFIG, policy)
    assert ws.agents_table_toml(tomllib.loads(AGENTS_CONFIG), policy) == want
    assert want is not None and want["prs"] == "true"
    assert ws.agents_table_310('name = "demo"\n', policy) is None
    with pytest.raises(ws.Refused):
        ws.agents_table_310(AGENTS_CONFIG + "[agents.extra]\nx = 1\n", policy)


def test_no_agents_table_reads_no_gate(ws: ModuleType) -> None:
    row = ws.triage_mode_row('name = "demo"\n', Path("shipmill.toml"))
    assert row.detail.startswith("no [agents]")


def claude_agent(name: str, cwd: str, minutes_ago: int, **more: object) -> dict[str, object]:
    started = int((NOW - dt.timedelta(minutes=minutes_ago)).timestamp() * 1000)
    return {"name": name, "cwd": cwd, "startedAt": started, "status": "busy", **more}


def test_agent_sessions_are_the_gates_and_those_in_the_checkout(ws: ModuleType) -> None:
    # #182: the sessions working the repo, by the gate's name or by folder; others are left out
    sessions = [
        claude_agent("shipmill o/r 2026-10-06 14:21", "/elsewhere", 2, id="80a9", kind="background", state="blocked"),
        claude_agent("mine", "/work/r/sub", 13, pid=2400, kind="interactive"),
        claude_agent("other", "/work/rr", 5, pid=3, kind="interactive"),
        claude_agent("shipmill o/rx", "/elsewhere", 5, id="x", kind="background"),
    ]
    rows = ws.session_rows(json.dumps(sessions), "o/r", Path("/work/r"), NOW)
    assert [(r.state, r.subject, r.detail) for r in rows] == [
        ("AGENT_SESSION", "80a9", "gate busy/blocked, started 2 min ago: shipmill o/r 2026-10-06 14:21"),
        ("AGENT_SESSION", "2400", "interactive busy, started 13 min ago: mine"),
    ]


def test_a_session_list_without_start_times_is_refused(ws: ModuleType) -> None:
    with pytest.raises(ws.Refused):
        ws.session_rows(json.dumps([{"name": "x"}]), "o/r", Path("/work/r"), NOW)


def test_the_gate_label_is_the_one_shipmill_launchd_installs(ws: ModuleType) -> None:
    from shipmill import launchd

    for repo in ["shipmill/shipmill", "Owner/My_Repo.x"]:
        assert ws.gate_label(repo) == launchd.label(repo)


PRINTED = "gui/501/dev.shipmill.gate.o.r = {\n\tactive count = 0\n\tstate = not running\n\tlast exit code = 0\n}\n"
GATE_LOG = "QUIET: nothing needs an agent\n  ISSUES o/r: NEW #3\nLAUNCH: 1 finding(s) need an agent\n  launched x\n"


def test_the_gate_loop_reports_its_interval_state_and_last_decision(ws: ModuleType) -> None:
    row = ws.loop_row("dev.shipmill.gate.o.r", {"StartInterval": 900}, PRINTED, GATE_LOG, NOW)
    want = "launchd every 15 min; not running; last exit 0; last: LAUNCH: 1 finding(s) need an agent"
    assert (row.state, row.subject, row.detail) == ("LOOP", "dev.shipmill.gate.o.r", want)


def test_an_unloaded_or_missing_gate_loop_says_so(ws: ModuleType) -> None:
    assert "not loaded" in ws.loop_row("l", {"StartInterval": 900}, None, None, NOW).detail
    assert ws.loop_row("l", None, None, None, NOW).subject == "none"


@pytest.mark.parametrize("state", ["TRIAGE_MODE", "AGENT_SESSION", "LOOP", "HOST_UNKNOWN"])
def test_the_agent_rows_are_report_only(ws: ModuleType, state: str) -> None:
    assert state not in ws.ACTION and state not in ws.AGENT


def test_delete_branch_on_merge_off_is_an_action_for_a_person(ws: ModuleType) -> None:
    # #194: the setting was off after a repo move and 74 merged branches piled up unnoticed
    rows = ws.settings_rows("o/r", False)
    assert [(r.state, r.subject) for r in rows] == [("BRANCH_DELETE_OFF", "o/r")]
    assert "gh repo edit o/r --delete-branch-on-merge" in rows[0].detail
    assert "BRANCH_DELETE_OFF" in ws.ACTION and "BRANCH_DELETE_OFF" not in ws.AGENT


def test_delete_branch_on_merge_on_prints_no_row(ws: ModuleType) -> None:
    assert ws.settings_rows("o/r", True) == []


def test_a_setting_that_is_not_a_boolean_is_refused(ws: ModuleType) -> None:
    with pytest.raises(ws.Refused):
        ws.settings_rows("o/r", None)
