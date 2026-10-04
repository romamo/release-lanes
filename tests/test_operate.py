"""shipyard operate (#30): health checks recorded as deployment statuses, promotion after an
unbroken bake, a missed lane deploy, autonomy and the hold, --approve, and the operate caller"""

import datetime as dt
import re
from dataclasses import dataclass, field, replace
from pathlib import Path

import pytest

from shipyard.autonomy import HOLD_LABEL
from shipyard.cli import main
from shipyard.doctor import OPERATE_CALLER, doctor
from shipyard.errors import ReleaseError
from shipyard.github import Deployment, DeploymentState
from shipyard.init import init_operate, operate_caller_text
from shipyard.operate import (
    STATUS_PREFIX,
    HttpResponse,
    Report,
    Unreachable,
    approve,
    deploy_marker,
    named_version,
    operate,
    tag_of,
)
from shipyard.version import Version

from .conftest import POLICY, T0, Repo

S = DeploymentState
STAGING = "https://staging.example.com/health"
PRODUCTION = "https://example.com/health"
ENVIRONMENTS = f"""
[environments.staging]
lane = "rc"
workflow = "deploy.yml"
health = "{STAGING}"

[environments.production]
from = "staging"
workflow = "deploy.yml"
health = "{PRODUCTION}"
bake_minutes = 60
"""
TO_PRODUCTION = ("deploy.yml", "v1.1.0rc1", "v1.1.0rc1", {"environment": "production"})


@dataclass
class FakeHttp:
    """Health answers by URL: a response, or Unreachable"""

    answers: dict[str, HttpResponse | Unreachable] = field(default_factory=dict)
    asked: list[tuple[str, float]] = field(default_factory=list)

    def get(self, url: str, timeout: float) -> HttpResponse:
        self.asked.append((url, timeout))
        answer = self.answers[url]
        if isinstance(answer, Unreachable):
            raise answer
        return answer

    def answer(self, url: str, body: str = "ok", status: int = 200, seconds: float = 0.2) -> None:
        self.answers[url] = HttpResponse(status, body, seconds)


def minutes(n: float) -> dt.datetime:
    return T0 + dt.timedelta(minutes=n)


def configure(repo: Repo, autonomy: str = "") -> None:
    repo.write(repo.policy_file, POLICY + ENVIRONMENTS + (f"\n[autonomy]\n{autonomy}" if autonomy else ""))


def tag(repo: Repo, name: str, at: dt.datetime) -> None:
    repo.at(at)
    repo.git.run("tag", "-a", name, "-m", name)


@pytest.fixture
def setup(repo: Repo) -> tuple[Repo, FakeHttp]:
    """production runs v1.0.0, staging got v1.1.0rc1 at T0; both answer healthy"""
    configure(repo)
    tag(repo, "v1.1.0rc1", minutes(-5))
    repo.github.deploy("production", "v1.0.0", T0 - dt.timedelta(days=20), S.IN_PROGRESS, S.SUCCESS)
    repo.github.deploy("staging", "v1.1.0rc1", minutes(-1), S.IN_PROGRESS, S.SUCCESS)  # success at T0
    http = FakeHttp()
    http.answer(STAGING, '{"status": "ok", "version": "1.1.0rc1"}')
    http.answer(PRODUCTION, '{"version": "v1.0.0"}')
    return repo, http


def run(repo: Repo, http: FakeHttp, at: dt.datetime, dry_run: bool = False) -> dict[str, Report]:
    repo.github.now = at
    reports = operate(repo.policy, repo.git.tags(), repo.github, http, at, dry_run)
    return {r.environment: r for r in reports}


def staging_writes(repo: Repo) -> list[DeploymentState]:
    [staging] = repo.github.envs["staging"]
    return [state for deployment, state, _ in repo.github.status_writes if deployment == staging.id]


# -- health ----------------------------------------------------------------------------------


def test_the_version_rule_reads_a_json_version_only() -> None:
    assert named_version('{"version": "1.2.0", "python": "3.14.0"}') == "1.2.0"
    assert named_version("ok 1.2.0") is None  # plain text names no version
    assert named_version('{"version": 3}') is None
    assert named_version('["1.2.0"]') is None


def test_a_version_mismatch_is_unhealthy_and_holds_the_promotion(setup: tuple[Repo, FakeHttp]) -> None:
    repo, http = setup
    http.answer(STAGING, '{"version": "1.0.0"}')  # a stale instance answering 200
    found = run(repo, http, minutes(90))
    assert found["staging"].health.startswith("unhealthy: answers version 1.0.0, but v1.1.0rc1 is deployed")
    assert staging_writes(repo) == [S.FAILURE]
    assert repo.github.dispatched == []
    assert "staging is unhealthy on v1.1.0rc1" in found["production"].action


@pytest.mark.parametrize(
    ("answer", "detail"),
    [
        (HttpResponse(503, "", 0.1), "HTTP 503"),
        (HttpResponse(200, "ok", 12.0), "answered in 12.0 s, over 10 s"),
        (Unreachable("connection refused"), "unreachable: connection refused"),
    ],
)
def test_a_failed_check_is_unhealthy(setup: tuple[Repo, FakeHttp], answer: HttpResponse, detail: str) -> None:
    repo, http = setup
    http.answers[STAGING] = answer
    assert run(repo, http, minutes(5))["staging"].health.startswith(f"unhealthy: {detail}")
    assert (STAGING, 10.0) in http.asked


def test_no_health_url_is_reported_and_bakes_on_time_alone(setup: tuple[Repo, FakeHttp]) -> None:
    repo, http = setup
    repo.write(repo.policy_file, repo.read(repo.policy_file).replace(f'health = "{STAGING}"\n', ""))
    assert run(repo, http, minutes(30))["staging"].health == "no health URL"
    assert repo.github.dispatched == []
    run(repo, http, minutes(61))
    assert repo.github.dispatched == [TO_PRODUCTION]
    assert staging_writes(repo) == []  # nothing checked, nothing recorded


# -- promotion -------------------------------------------------------------------------------


def test_promotion_only_after_an_unbroken_bake(setup: tuple[Repo, FakeHttp]) -> None:
    repo, http = setup
    found = run(repo, http, minutes(10))
    assert found["staging"].health == "healthy (HTTP 200, version 1.1.0rc1), baking 10 of 60 min"
    assert "v1.1.0rc1 baking on staging: 10 of 60 min" in found["production"].action
    run(repo, http, minutes(59))
    assert repo.github.dispatched == []
    found = run(repo, http, minutes(61))
    # dispatched on the tag, so the deployment GitHub records names it
    assert repo.github.dispatched == [TO_PRODUCTION]
    assert "dispatch deploy.yml with v1.1.0rc1 (healthy on staging for the 60 min bake)" in found["production"].action
    assert staging_writes(repo) == [S.IN_PROGRESS, S.SUCCESS]
    # production's own latest status was success already, and nothing is promoted from it
    assert len(repo.github.status_writes) == 2


def test_a_failed_check_restarts_the_bake(setup: tuple[Repo, FakeHttp]) -> None:
    repo, http = setup
    run(repo, http, minutes(10))
    http.answer(STAGING, "", status=500)
    assert "failing for 0 min" in run(repo, http, minutes(30))["staging"].health
    assert "failing for 5 min" in run(repo, http, minutes(35))["staging"].health
    http.answer(STAGING, '{"version": "1.1.0rc1"}')
    run(repo, http, minutes(40))
    run(repo, http, minutes(61))  # 61 min since the success, but only 21 since the recovery
    assert repo.github.dispatched == []
    run(repo, http, minutes(101))
    assert repo.github.dispatched == [TO_PRODUCTION]
    assert staging_writes(repo) == [S.IN_PROGRESS, S.FAILURE, S.IN_PROGRESS, S.SUCCESS]


def test_a_promotion_is_dispatched_once(setup: tuple[Repo, FakeHttp]) -> None:
    repo, http = setup
    run(repo, http, minutes(61))
    repo.github.deploy("production", "v1.1.0rc1", minutes(62), S.IN_PROGRESS)
    found = run(repo, http, minutes(71))
    assert repo.github.dispatched == [TO_PRODUCTION]
    assert "v1.1.0rc1 was deployed here already (in_progress)" in found["production"].action


def test_a_promotion_still_queued_is_not_dispatched_again(setup: tuple[Repo, FakeHttp]) -> None:
    # a deploy job creates its deployment only when it starts: behind a build job or a wait
    # for a runner, the next operate run finds no deployment of the tag yet, but the run
    repo, http = setup
    run(repo, http, minutes(61))
    found = run(repo, http, minutes(71))
    assert repo.github.dispatched == [TO_PRODUCTION]
    assert "deploy.yml already ran with v1.1.0rc1 (queued, run 1)" in found["production"].action


def test_a_deploy_run_that_failed_before_its_deployment_is_not_retried(setup: tuple[Repo, FakeHttp]) -> None:
    repo, http = setup
    run(repo, http, minutes(61))
    workflow, ref, queued = repo.github.runs[0]
    repo.github.runs[0] = (workflow, ref, replace(queued, status="completed"))  # its build job failed
    found = run(repo, http, minutes(81))
    assert repo.github.dispatched == [TO_PRODUCTION]
    assert "deploy.yml already ran with v1.1.0rc1 (completed, run 1)" in found["production"].action


def test_the_source_run_of_a_shared_workflow_does_not_block_the_promotion(setup: tuple[Repo, FakeHttp]) -> None:
    repo, http = setup
    repo.github.now = minutes(-3)  # staging's own deploy.yml run on the tag, before its deployment
    repo.github.dispatch("deploy.yml", "v1.1.0rc1", "v1.1.0rc1", {"environment": "staging"})
    repo.github.dispatched.clear()
    run(repo, http, minutes(61))
    assert repo.github.dispatched == [TO_PRODUCTION]


def test_approve_refuses_while_a_run_of_the_tag_is_queued(setup: tuple[Repo, FakeHttp]) -> None:
    repo, http = setup
    configure(repo, 'deploy.production = "propose"\n')
    run(repo, http, minutes(61))
    repo.github.dispatch(*TO_PRODUCTION)  # approved by hand already; its run is still queued
    with pytest.raises(ReleaseError, match=r"deploy.yml is running with v1.1.0rc1 already \(run 1, queued\)"):
        approve(repo.policy, repo.github, "production", dry_run=False)


def test_repeated_runs_write_no_duplicate_statuses(setup: tuple[Repo, FakeHttp]) -> None:
    repo, http = setup
    for at in (5, 10, 20):
        run(repo, http, minutes(at))
    assert staging_writes(repo) == [S.IN_PROGRESS]
    for at in (70, 80, 90):
        run(repo, http, minutes(at))
    assert staging_writes(repo) == [S.IN_PROGRESS, S.SUCCESS]
    assert all(d.startswith(STATUS_PREFIX) for _, _, d in repo.github.status_writes)


def test_a_dry_run_acts_on_nothing(setup: tuple[Repo, FakeHttp]) -> None:
    repo, http = setup
    assert run(repo, http, minutes(30), dry_run=True)["staging"].action.startswith("would mark in_progress")
    found = run(repo, http, minutes(90), dry_run=True)
    assert found["production"].action.startswith("would dispatch deploy.yml with v1.1.0rc1")
    configure(repo, 'deploy.production = "propose"\n')
    assert run(repo, http, minutes(90), dry_run=True)["production"].action.startswith("would propose v1.1.0rc1")
    assert repo.github.status_writes == [] and repo.github.dispatched == [] and repo.github.issues == {}


# -- autonomy and the hold -------------------------------------------------------------------


def test_propose_opens_one_issue_and_keeps_it(setup: tuple[Repo, FakeHttp]) -> None:
    repo, http = setup
    configure(repo, 'deploy.production = "propose"\n')
    found = run(repo, http, minutes(61))
    assert repo.github.dispatched == []
    [issue] = repo.github.issues.values()
    assert issue.title == "Ready to promote v1.1.0rc1 to production"
    assert deploy_marker("production") in issue.body
    assert "gh workflow run operate.yml -f approve=production -f dry-run=false" in issue.body
    opened = f"proposal #{issue.number} opened: v1.1.0rc1 (deploy.production autonomy is propose)"
    assert found["production"].action == opened
    run(repo, http, minutes(71))
    assert len(repo.github.issues) == 1 and repo.github.edits == 0


def test_observe_only_reports(setup: tuple[Repo, FakeHttp]) -> None:
    repo, http = setup
    configure(repo, 'deploy.production = "observe"\n')
    found = run(repo, http, minutes(61))
    assert repo.github.dispatched == [] and repo.github.issues == {}
    assert "would deploy v1.1.0rc1" in found["production"].action
    assert found["production"].action.endswith("but deploy.production autonomy is observe")


def test_the_hold_turns_a_promotion_into_a_proposal(setup: tuple[Repo, FakeHttp]) -> None:
    repo, http = setup
    repo.github.holds = ["#7 Investigating"]
    run(repo, http, minutes(61))
    assert repo.github.dispatched == []
    [issue] = repo.github.issues.values()
    assert f"but held by {HOLD_LABEL} #7." in issue.body
    assert f"Close the open `{HOLD_LABEL}` issues first" in issue.body


def test_approve_deploys_the_proposed_tag_once(setup: tuple[Repo, FakeHttp]) -> None:
    repo, http = setup
    configure(repo, 'deploy.production = "propose"\n')
    run(repo, http, minutes(61))
    [number] = repo.github.issues
    repo.github.holds = ["#7 Investigating"]
    with pytest.raises(ReleaseError, match=f"held by {HOLD_LABEL} #7; close it to approve a deploy"):
        approve(repo.policy, repo.github, "production", dry_run=False)
    repo.github.holds = []
    assert approve(repo.policy, repo.github, "production", dry_run=True).startswith("would dispatch")
    assert repo.github.dispatched == []
    done = approve(repo.policy, repo.github, "production", dry_run=False)
    assert done == f"dispatched deploy.yml with v1.1.0rc1 to production; closed #{number}"
    assert repo.github.dispatched == [TO_PRODUCTION]
    assert "Approved" in repo.github.closed[number]
    with pytest.raises(ReleaseError, match="no open proposal to deploy to production"):
        approve(repo.policy, repo.github, "production", dry_run=False)
    with pytest.raises(ReleaseError, match="no environment 'qa'"):
        approve(repo.policy, repo.github, "qa", dry_run=False)


# -- a missed lane deploy --------------------------------------------------------------------


def test_a_missed_lane_deploy_is_dispatched_once(setup: tuple[Repo, FakeHttp]) -> None:
    repo, http = setup
    tag(repo, "v1.1.0rc2", minutes(100))  # land's dispatch to staging failed
    assert "v1.1.0rc2 just landed" in run(repo, http, minutes(110))["staging"].action
    rc2 = ("deploy.yml", "v1.1.0rc2", "v1.1.0rc2", {"environment": "staging"})
    assert rc2 not in repo.github.dispatched
    found = run(repo, http, minutes(131))
    assert rc2 in repo.github.dispatched
    assert "the newest rc release, which its deploy missed" in found["staging"].action
    repo.github.deploy("staging", "v1.1.0rc2", minutes(132), S.IN_PROGRESS, S.FAILURE)
    found = run(repo, http, minutes(141))
    assert repo.github.dispatched.count(rc2) == 1
    assert "v1.1.0rc2 was deployed here already (failure)" in found["staging"].action


# -- reading deployments ---------------------------------------------------------------------


def test_a_deployment_names_its_tag_by_ref_or_sha(repo: Repo) -> None:
    tags = repo.git.tags()
    sha = tags[0].commit

    def of(ref: str) -> Version | None:
        return tag_of(Deployment(1, ref, sha, T0), tags)

    assert of("v1.2.0") == Version(1, 2, 0)
    assert of("refs/tags/v1.2.0rc1") == Version(1, 2, 0, "rc", 1)
    assert of(sha) == Version(1, 0, 0)
    assert of("main") is None


def test_a_deployment_that_never_succeeded_is_not_current(setup: tuple[Repo, FakeHttp]) -> None:
    repo, http = setup
    repo.github.deploy("production", "v1.1.0rc1", minutes(1), S.IN_PROGRESS, S.FAILURE)
    assert run(repo, http, minutes(5))["production"].tag == "v1.0.0"


# -- the CLI, init, doctor, and the workflow -------------------------------------------------


def test_cli_operate_writes_the_summary(setup: tuple[Repo, FakeHttp], tmp_path: Path) -> None:
    repo, http = setup
    summary = tmp_path / "summary.md"
    argv = ["--repo", str(repo.root), "operate", "--now", minutes(61).isoformat(), "--step-summary", str(summary)]
    assert main(argv, repo.github, http) == 0
    text = summary.read_text()
    assert "| Environment | Tag | Health | Action |" in text
    assert "| production | v1.0.0 |" in text
    assert repo.github.dispatched == [TO_PRODUCTION]


def test_init_operate_writes_the_caller_and_doctor_wants_it_only_when_used(repo: Repo) -> None:
    def operate_check() -> tuple[str, str] | None:
        found = {c.name: (c.status, c.detail) for c in doctor(repo.root, repo.github)}
        return found.get("operate")

    repo.write(repo.policy_file, POLICY + '\n[environments.staging]\nlane = "rc"\nworkflow = "deploy.yml"\n')
    assert operate_check() is None  # D-9: no from, no health, no warning
    configure(repo)
    status, detail = operate_check() or ("", "")
    assert status == "WARN" and "`shipyard init --operate` writes it" in detail
    init_operate(repo.root, force=False)
    assert operate_check() == ("PASS", f"{OPERATE_CALLER} runs shipyard operate with deployments, actions: write")
    with pytest.raises(ReleaseError, match="exists; pass --force"):
        init_operate(repo.root, force=False)
    caller = operate_caller_text()
    repo.write(str(OPERATE_CALLER), caller.replace("      deployments: write", "      deployments: read"))
    assert operate_check() == ("WARN", f"the job in {OPERATE_CALLER} that calls operate.yml lacks deployments: write")
    repo.write(str(OPERATE_CALLER), caller.replace("      issues: write", "      issues: read"))
    assert (operate_check() or ("", ""))[0] == "PASS"
    configure(repo, 'deploy.production = "propose"\n')  # now a deploy opens an issue
    assert operate_check() == ("WARN", f"the job in {OPERATE_CALLER} that calls operate.yml lacks issues: write")


def test_operate_yml_runs_one_at_a_time_and_takes_the_callers_grant() -> None:
    text = (Path(__file__).parent.parent / ".github" / "workflows" / "operate.yml").read_text()
    assert re.search(r"^    concurrency:\n      group: shipyard-operate\n      cancel-in-progress: false$", text, re.M)
    assert not re.search(r"^\s*permissions:", text, re.MULTILINE)
    assert 'cron: "*/10 * * * *"' in operate_caller_text()
