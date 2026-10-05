"""[autonomy] and the stop switch (#32): parsing, the hold, release autonomy in the planner,
the proposal issue, and doctor's report"""

import datetime as dt
import json
import re
import tomllib
from pathlib import Path

import pytest

from shipmill.autonomy import HOLD_LABEL, Autonomy, AutonomyPolicy, EnvironmentName, Hold, Stage
from shipmill.cli import main
from shipmill.doctor import doctor
from shipmill.errors import ReleaseError
from shipmill.github import OPEN_LIMIT, PROPOSAL_LABEL
from shipmill.planner import Decision, Event, Hotfix, Proposal
from shipmill.policy import Lane, Policy
from shipmill.propose import Outcome, close_released, marker, propose, run_url
from shipmill.version import Version

from .conftest import POLICY, Repo, at_day
from .test_lanes import plan

HELD = Hold(("#7 Investigating the 1.1 regression",))
PRODUCTION = Stage.deploy(EnvironmentName("production"))
ENVIRONMENTS = """
[environments.staging]
lane = "rc"
workflow = "deploy.yml"

[environments.production]
from = "staging"
workflow = "deploy.yml"
"""


def autonomy(text: str, environments: str = ENVIRONMENTS) -> AutonomyPolicy:
    return Policy.parse(tomllib.loads(POLICY + environments + "\n[autonomy]\n" + text), "shipmill.toml").autonomy


def set_autonomy(repo: Repo, text: str) -> None:
    repo.write(repo.policy_file, POLICY + ENVIRONMENTS + "\n[autonomy]\n" + text)


# -- the config ------------------------------------------------------------------------------


def test_no_autonomy_section_acts_everywhere(repo: Repo) -> None:
    policy = repo.policy
    for stage in (Stage.release(), Stage.rollback(), PRODUCTION):
        assert policy.autonomy.configured(stage) is Autonomy.ACT
        assert policy.autonomy.effective(stage, Hold()) is Autonomy.ACT


def test_autonomy_parses_each_stage() -> None:
    parsed = autonomy(
        'release = "propose"\nrollback = "observe"\ndeploy.production = "propose"\ndeploy.staging = "act"\n'
    )
    assert parsed.configured(Stage.release()) is Autonomy.PROPOSE
    assert parsed.configured(Stage.rollback()) is Autonomy.OBSERVE
    assert parsed.configured(PRODUCTION) is Autonomy.PROPOSE
    assert parsed.configured(Stage.deploy(EnvironmentName("staging"))) is Autonomy.ACT
    assert parsed.configured(Stage.deploy(EnvironmentName("qa"))) is Autonomy.ACT  # unlisted: act (D-7)
    assert [str(s) for s in parsed.stages()] == ["release", "deploy.production", "deploy.staging", "rollback"]


@pytest.mark.parametrize(
    ("text", "error"),
    [
        ('releases = "act"', r"\[autonomy\]: unknown keys \['releases'\]"),
        ('release = "auto"', r"release must be one of \['observe', 'propose', 'act'\], got 'auto'"),
        ("release = true", "release must be one of"),
        ('deploy = "propose"', "deploy is a table of environments"),
        ('deploy.production = "yes"', r"\[autonomy\] \[deploy\]: production must be one of"),
        ('deploy."pro duction" = "act"', "an environment name is"),
    ],
)
def test_autonomy_refuses_what_it_does_not_know(text: str, error: str) -> None:
    with pytest.raises(ReleaseError, match=error):
        autonomy(text)


def test_intake_proposes_by_default_and_never_acts() -> None:
    assert autonomy("").intake is Autonomy.PROPOSE
    assert autonomy('intake = "observe"\n').intake is Autonomy.OBSERVE
    assert autonomy('intake = "propose"\n').intake is Autonomy.PROPOSE
    with pytest.raises(ReleaseError, match=r"intake is observe or propose: only the maintainer accepts"):
        autonomy('intake = "act"\n')
    with pytest.raises(ReleaseError, match=r"intake must be one of \['observe', 'propose'\], got 1"):
        autonomy("intake = 1\n")
    # not a stage: the hold has nothing to turn, and doctor's stage list is unchanged
    assert [str(s) for s in autonomy('intake = "observe"\n').stages()] == ["release", "rollback"]


def test_autonomy_deploy_names_a_declared_environment() -> None:
    known = r"known: staging, production$"
    with pytest.raises(
        ReleaseError,
        match=r"^shipmill\.toml \[autonomy\]: deploy\.qa names no environment in \[environments\]; " + known,
    ):
        autonomy('deploy.qa = "propose"\n')
    with pytest.raises(
        ReleaseError, match=r"deploy\.production names no environment in \[environments\]; known: none$"
    ):
        autonomy('deploy.production = "propose"\n', environments="")
    assert autonomy('release = "propose"\n', environments="").release is Autonomy.PROPOSE


def test_a_deploy_stage_names_its_environment() -> None:
    with pytest.raises(ReleaseError, match="a deploy stage names its environment"):
        Stage(Stage.release().kind, EnvironmentName("production"))


# -- the hold --------------------------------------------------------------------------------


def test_the_hold_turns_every_act_into_propose() -> None:
    parsed = autonomy('rollback = "observe"\ndeploy.staging = "propose"\n')
    assert parsed.effective(Stage.release(), HELD) is Autonomy.PROPOSE
    assert parsed.effective(PRODUCTION, HELD) is Autonomy.PROPOSE
    assert parsed.effective(Stage.deploy(EnvironmentName("staging")), HELD) is Autonomy.PROPOSE
    assert parsed.effective(Stage.rollback(), HELD) is Autonomy.OBSERVE  # the hold never raises a level
    assert parsed.cause(Stage.release(), HELD) == f"held by {HOLD_LABEL} #7"
    assert parsed.cause(Stage.rollback(), HELD) == "rollback autonomy is observe"


def test_the_hold_is_read_from_the_label(repo: Repo) -> None:
    repo.github.blockers = ["#9 Data loss"]  # another label: no hold
    assert not Hold.read(repo.github).on
    repo.github.holds = ["#7 Investigating", "#8 Also"]
    assert Hold.read(repo.github).reason == f"held by {HOLD_LABEL} #7, #8"


# -- release autonomy in the planner ---------------------------------------------------------


def test_act_releases_as_before(repo: Repo) -> None:
    repo.merge(1, "Added", "Feature A")
    set_autonomy(repo, 'release = "act"\n')
    decision = plan(repo, at_day(1))
    assert (decision.action, decision.lane, str(decision.version)) == ("release", Lane.RC, "1.1.0rc1")
    assert decision.outputs()["proposals"] == ""


def test_observe_reports_and_releases_nothing(repo: Repo) -> None:
    repo.merge(1, "Added", "Feature A")
    set_autonomy(repo, 'release = "observe"\n')
    decision = plan(repo, at_day(1))
    assert decision.action == "skip" and not decision.proposals
    assert "rc: 1.1.0rc1 is due (window 'daily 07:00 UTC' opened" in decision.reason
    assert "but release autonomy is observe" in decision.reason


def test_propose_proposes_every_due_lane(repo: Repo) -> None:
    repo.merge(1, "Added", "Feature A")
    set_autonomy(repo, 'release = "propose"\n')
    decision = plan(repo, at_day(1))
    assert decision.action == "propose" and decision.lane is None and decision.version is None
    assert [(p.lane, str(p.version), p.cause) for p in decision.proposals] == [
        (Lane.RC, "1.1.0rc1", "release autonomy is propose"),
        (Lane.DEV, "1.1.0.dev2", "release autonomy is propose"),
    ]
    assert "rc: 1.1.0rc1 is due, proposed instead of released: release autonomy is propose" in decision.reason
    proposals = json.loads(decision.outputs()["proposals"])
    assert tuple(Proposal.from_dict(p) for p in proposals) == decision.proposals


def test_a_person_starting_a_lane_releases_under_propose(repo: Repo) -> None:
    repo.merge(1, "Added", "Feature A")
    set_autonomy(repo, 'release = "propose"\n')
    decision = plan(repo, at_day(1), event=Event.MANUAL, lane=Lane.RC)
    assert (decision.action, str(decision.version)) == ("release", "1.1.0rc1")
    # lane=policy by hand follows the policy, as a scheduled run does (D-1)
    assert plan(repo, at_day(1), event=Event.MANUAL).action == "propose"


def test_the_hold_proposes_scheduled_and_push_runs(repo: Repo) -> None:
    repo.merge(1, "Added", "Feature A")
    repo.github.holds = ["#7 Investigating"]
    for event in (Event.SCHEDULE, Event.PUSH, Event.MANUAL):
        decision = plan(repo, at_day(1), event=event)
        assert decision.action == "propose", event
        assert {p.cause for p in decision.proposals} == {f"held by {HOLD_LABEL} #7"}


def test_the_hold_stops_a_hand_started_lane_until_closed(repo: Repo) -> None:
    repo.merge(1, "Added", "Feature A")
    repo.github.holds = ["#7 Investigating"]
    decision = plan(repo, at_day(1), event=Event.MANUAL, lane=Lane.RC)
    assert decision.action == "skip"
    assert decision.reason == (
        f"rc: 1.1.0rc1 held by {HOLD_LABEL} #7; close it to release by hand (a hotfix can still be started by hand)"
    )
    repo.github.holds = []
    assert plan(repo, at_day(1), event=Event.MANUAL, lane=Lane.RC).action == "release"


def test_a_hand_started_hotfix_releases_under_the_hold(repo: Repo) -> None:
    repo.merge(1, "Added", "Feature A, not ready", "src/a.py", "A = 1\n")
    repo.merge(2, "Fixed", "Urgent fix", "src/app.py", "VALUE = 2\n")
    repo.github.holds = ["#7 Investigating"]
    set_autonomy(repo, 'release = "propose"\n')
    decision = plan(repo, at_day(1), event=Event.MANUAL, lane=Lane.HOTFIX, hotfix=Hotfix((2,)))
    assert (decision.action, str(decision.version), decision.prs) == ("release", "1.0.1", (2,))
    assert plan(repo, at_day(1), event=Event.MANUAL, lane=Lane.RC).action == "skip"


def test_observe_stays_observe_under_the_hold(repo: Repo) -> None:
    repo.merge(1, "Added", "Feature A")
    set_autonomy(repo, 'release = "observe"\n')
    repo.github.holds = ["#7 Investigating"]
    decision = plan(repo, at_day(1))
    assert decision.action == "skip" and "release autonomy is observe" in decision.reason


def test_the_hold_is_not_read_when_nothing_is_due(repo: Repo) -> None:
    repo.github.holds = ["#7 Investigating"]
    assert plan(repo, at_day(1)).action == "skip"
    assert HOLD_LABEL not in repo.github.labels_read


# -- the proposal issue ----------------------------------------------------------------------


def proposed(repo: Repo, now: dt.datetime) -> Decision:
    decision = plan(repo, now)
    assert decision.action == "propose", decision.reason
    return decision


def test_the_proposal_issue_is_opened_once_and_updated(repo: Repo) -> None:
    repo.merge(1, "Added", "Feature A")
    set_autonomy(repo, 'release = "propose"\n')
    decision = proposed(repo, at_day(1))
    first = propose(repo.git, repo.policy, repo.github, decision.proposals)
    assert [(d.proposal.lane, d.outcome) for d in first] == [(Lane.RC, Outcome.OPENED), (Lane.DEV, Outcome.OPENED)]
    rc = repo.github.issues[first[0].issue]
    assert rc.title == "Ready to release v1.1.0rc1 on rc"
    assert rc.body.startswith(marker(Lane.RC) + "\n")
    assert "but release autonomy is propose." in rc.body
    assert "#### Added\n\n- Feature A (#1)" in rc.body
    assert "gh workflow run release.yml -f lane=rc -f dry-run=false" in rc.body
    again = propose(repo.git, repo.policy, repo.github, proposed(repo, at_day(1)).proposals)
    assert {d.outcome for d in again} == {Outcome.UNCHANGED}
    assert len(repo.github.issues) == 2 and repo.github.edits == 0
    repo.merge(2, "Fixed", "Fix B")
    set_autonomy(repo, 'release = "propose"\n')  # merge resets the checkout to main
    later = propose(repo.git, repo.policy, repo.github, proposed(repo, at_day(1)).proposals)
    assert [(d.issue, d.outcome) for d in later][0] == (first[0].issue, Outcome.UPDATED)
    assert len(repo.github.issues) == 2
    assert "- Fix B (#2)" in repo.github.issues[first[0].issue].body


def test_a_held_proposal_says_to_close_the_hold(repo: Repo) -> None:
    repo.merge(1, "Added", "Feature A")
    repo.github.holds = ["#7 Investigating"]
    [rc, _] = propose(repo.git, repo.policy, repo.github, proposed(repo, at_day(1)).proposals)
    body = repo.github.issues[rc.issue].body
    assert f"but held by {HOLD_LABEL} #7." in body
    assert f"Close the open `{HOLD_LABEL}` issues first" in body


def test_cli_propose(repo: Repo, capsys: pytest.CaptureFixture[str]) -> None:
    repo.merge(1, "Added", "Feature A")
    set_autonomy(repo, 'release = "propose"\n')
    outputs = proposed(repo, at_day(1)).outputs()
    args = ["--repo", str(repo.root), "propose", "--proposals", outputs["proposals"]]
    assert main(args, repo.github) == 0
    assert capsys.readouterr().out.startswith("opened #100: rc 1.1.0rc1, release autonomy is propose\n")
    with pytest.raises(ReleaseError, match="non-empty JSON list"):
        main(["--repo", str(repo.root), "propose", "--proposals", "[]"], repo.github)
    with pytest.raises(ReleaseError, match="a proposal's lane is one of"):
        bad = json.dumps([{"lane": "beta", "version": "1.0.0", "base": "abc", "cause": "x"}])
        main(["--repo", str(repo.root), "propose", "--proposals", bad], repo.github)


def test_a_proposal_is_labelled_and_found_by_its_label_past_the_scan(repo: Repo) -> None:
    repo.merge(1, "Added", "Feature A")
    set_autonomy(repo, 'release = "propose"\n')
    rc, _ = propose(repo.git, repo.policy, repo.github, proposed(repo, at_day(1)).proposals)
    assert repo.github.labels[rc.issue] == (PROPOSAL_LABEL,)
    for n in range(3):  # newer issues push the proposal off the page find_issue reads
        repo.github.create_issue(f"Bug {n}", "something broke")
    repo.github.scan_limit = 2
    assert repo.github.find_issue(marker(Lane.RC)) is None
    again = propose(repo.git, repo.policy, repo.github, proposed(repo, at_day(1)).proposals)
    assert [(d.issue, d.outcome) for d in again][0] == (rc.issue, Outcome.UNCHANGED)
    assert len(repo.github.issues) == 5  # two proposals and three bugs: no duplicate


def test_a_proposal_opened_before_the_label_is_found_once_and_labelled(repo: Repo) -> None:
    repo.merge(1, "Added", "Feature A")
    set_autonomy(repo, 'release = "propose"\n')
    decision = proposed(repo, at_day(1))
    first = propose(repo.git, repo.policy, repo.github, decision.proposals)
    old = first[0].issue
    repo.github.labels[old] = ()  # as an earlier shipmill opened it
    again = propose(repo.git, repo.policy, repo.github, decision.proposals)
    assert [(d.issue, d.outcome) for d in again][0] == (old, Outcome.UPDATED)  # labelled, though unchanged
    assert repo.github.labels[old] == (PROPOSAL_LABEL,)
    assert {d.outcome for d in propose(repo.git, repo.policy, repo.github, decision.proposals)} == {Outcome.UNCHANGED}
    assert len(repo.github.issues) == 2


def test_closed_proposals_do_not_count_and_too_many_open_ones_are_refused(repo: Repo) -> None:
    repo.merge(1, "Added", "Feature A")
    set_autonomy(repo, 'release = "propose"\n')
    decision = proposed(repo, at_day(1))
    for n in range(OPEN_LIMIT):  # released long ago: closed proposals the lookup never reads
        number = repo.github.create_issue(f"Ready to release v0.{n}.0 on rc", marker(Lane.RC), (PROPOSAL_LABEL,))
        repo.github.close_issue(number, "Released")
    rc, _ = propose(repo.git, repo.policy, repo.github, decision.proposals)
    assert rc.outcome is Outcome.OPENED
    assert {d.outcome for d in propose(repo.git, repo.policy, repo.github, decision.proposals)} == {Outcome.UNCHANGED}
    for n in range(OPEN_LIMIT - 2):
        repo.github.create_issue(f"Stray {n}", "", (PROPOSAL_LABEL,))
    with pytest.raises(ReleaseError, match=f"{OPEN_LIMIT} open issues carry the {PROPOSAL_LABEL} label"):
        propose(repo.git, repo.policy, repo.github, decision.proposals)


def test_closing_finds_the_proposal_by_its_label_past_the_scan(repo: Repo) -> None:
    repo.merge(1, "Added", "Feature A")
    set_autonomy(repo, 'release = "propose"\n')
    rc, _ = propose(repo.git, repo.policy, repo.github, proposed(repo, at_day(1)).proposals)
    repo.github.create_issue("Bug", "something broke")
    repo.github.scan_limit = 1
    done = close_released(repo.policy, repo.github, Lane.RC, Version.parse("1.1.0rc1"), None)
    assert done == f"closed proposal #{rc.issue}: v1.1.0rc1 released"


# -- doctor ----------------------------------------------------------------------------------


def checks(repo: Repo, github: object) -> dict[str, tuple[str, str]]:
    return {c.name: (c.status, c.detail) for c in doctor(repo.root, github)}  # type: ignore[arg-type]


def test_doctor_reports_autonomy_and_the_hold(repo: Repo) -> None:
    set_autonomy(repo, 'deploy.production = "propose"\n')
    found = checks(repo, repo.github)
    assert found["hold"] == ("PASS", f"no open {HOLD_LABEL} issue")
    assert found["autonomy"][1].startswith(
        "release act, deploy.production propose, rollback act; any other deploy environment act"
    )
    assert found["autonomy"][1].endswith("; intake propose in the product-intake skill")
    repo.github.holds = ["#7 Investigating"]
    found = checks(repo, repo.github)
    assert found["hold"][0] == "WARN" and found["hold"][1].startswith(f"held by {HOLD_LABEL} #7")
    assert found["autonomy"][1].startswith("release propose, deploy.production propose, rollback propose")


def test_doctor_warns_when_it_cannot_read_issues(repo: Repo) -> None:
    assert checks(repo, None)["hold"] == ("WARN", f"can't read issues for {HOLD_LABEL}: gh isn't installed")


CALLER_TEXT = """\
jobs:
  prepare:
    uses: romamo/shipmill/.github/workflows/prepare.yml@v0
    permissions:
      contents: write
      issues: {prepare}
  other:
    runs-on: ubuntu-latest
    permissions:
      issues: {other}
"""


def test_doctor_warns_on_a_caller_that_cannot_open_the_proposal_only_when_needed(repo: Repo) -> None:
    caller = ".github/workflows/release.yml"
    repo.write(caller, CALLER_TEXT.format(prepare="read", other="write"))
    assert "permissions" not in checks(repo, repo.github)  # no propose, no hold: nothing opens an issue
    set_autonomy(repo, 'rollback = "propose"\n')
    status, detail = checks(repo, repo.github)["permissions"]
    assert status == "WARN" and detail.startswith("the config sets a stage to propose")
    assert detail.endswith("change its `issues: read` to `issues: write`")  # another job's write doesn't count
    set_autonomy(repo, "")
    repo.github.holds = ["#7 Investigating"]
    assert checks(repo, repo.github)["permissions"][1].startswith(f"held by {HOLD_LABEL} #7")
    repo.write(caller, CALLER_TEXT.format(prepare="write", other="read"))
    assert "permissions" not in checks(repo, repo.github)


# -- the workflow ----------------------------------------------------------------------------


def test_prepare_yml_never_asks_a_caller_for_issues_write() -> None:
    # A reusable workflow whose job asks for more than the caller grants fails to start, so a
    # caller still granting `issues: read` must keep working; the propose job takes the caller's
    # grant instead, and only it, so there is no workflow-level permissions block to inherit
    text = (Path(__file__).parent.parent / ".github" / "workflows" / "prepare.yml").read_text()
    assert not re.search(r"^\s+issues:\s*write", text, re.MULTILINE)
    assert "\npermissions:" not in text
    propose_job = text.split("\n  propose:\n", 1)[1]
    assert "\n    permissions:" not in propose_job
    assert "needs.prepare.outputs.action == 'propose' && needs.prepare.outputs.mode == 'release'" in propose_job


def test_a_held_proposal_says_to_close_the_hold_under_propose_too(repo: Repo) -> None:
    # under release = "propose" a hold still refuses the hand-started lane, so the issue
    # must say to close the hold, not just to start the lane
    repo.merge(1, "Added", "Feature A")
    set_autonomy(repo, 'release = "propose"\n')
    repo.github.holds = ["#7 Investigating"]
    [rc, _] = propose(repo.git, repo.policy, repo.github, proposed(repo, at_day(1)).proposals)
    assert f"Close the open `{HOLD_LABEL}` issues first" in repo.github.issues[rc.issue].body


# -- closing the proposal once released (#40) ------------------------------------------------

RUN = "https://github.com/o/demo/actions/runs/9"


def test_a_release_closes_its_lanes_proposal_only(repo: Repo) -> None:
    repo.merge(1, "Added", "Feature A")
    set_autonomy(repo, 'release = "propose"\n')
    rc, dev = propose(repo.git, repo.policy, repo.github, proposed(repo, at_day(1)).proposals)
    done = close_released(repo.policy, repo.github, Lane.RC, Version.parse("1.1.0rc1"), RUN)
    assert done == f"closed proposal #{rc.issue}: v1.1.0rc1 released"
    assert repo.github.closed == {rc.issue: f"Released v1.1.0rc1 on the rc lane in {RUN}."}
    assert dev.issue in repo.github.issues  # another lane's proposal stays open
    assert close_released(repo.policy, repo.github, Lane.RC, Version.parse("1.1.0rc1"), None) == (
        "no open proposal for the rc lane"
    )


def test_closing_says_when_the_lane_released_another_version(repo: Repo) -> None:
    repo.merge(1, "Added", "Feature A")
    set_autonomy(repo, 'release = "propose"\n')
    rc, _ = propose(repo.git, repo.policy, repo.github, proposed(repo, at_day(1)).proposals)
    close_released(repo.policy, repo.github, Lane.RC, Version.parse("1.1.0rc2"), None)
    assert repo.github.closed[rc.issue] == (
        "Released v1.1.0rc2 on the rc lane. This issue proposed v1.1.0rc1; the lane released v1.1.0rc2,"
        " so it is closed too."
    )


def test_closing_leaves_a_proposal_for_a_later_version_open(repo: Repo) -> None:
    repo.merge(1, "Added", "Feature A")
    set_autonomy(repo, 'release = "propose"\n')
    rc, _ = propose(repo.git, repo.policy, repo.github, proposed(repo, at_day(1)).proposals)
    done = close_released(repo.policy, repo.github, Lane.RC, Version.parse("1.0.1"), None)
    assert done == f"proposal #{rc.issue} names v1.1.0rc1, later than v1.0.1: left open"
    assert rc.issue in repo.github.issues and repo.github.closed == {}


def test_closing_reads_no_issue_unless_release_proposes(repo: Repo) -> None:
    repo.merge(1, "Added", "Feature A")
    repo.github.holds = ["#7 Investigating"]
    rc, _ = propose(repo.git, repo.policy, repo.github, proposed(repo, at_day(1)).proposals)
    assert close_released(repo.policy, repo.github, Lane.RC, Version.parse("1.1.0rc1"), RUN) == (
        "release autonomy is act: no proposal to close"
    )
    assert rc.issue in repo.github.issues and repo.github.closed == {}


def test_the_run_url_comes_from_the_actions_variables() -> None:
    env = {"GITHUB_SERVER_URL": "https://github.com", "GITHUB_REPOSITORY": "o/demo", "GITHUB_RUN_ID": "9"}
    assert run_url(env) == RUN
    assert run_url({**env, "GITHUB_RUN_ID": ""}) is None


def test_cli_close_proposal(repo: Repo, capsys: pytest.CaptureFixture[str]) -> None:
    repo.merge(1, "Added", "Feature A")
    set_autonomy(repo, 'release = "propose"\n')
    rc, _ = propose(repo.git, repo.policy, repo.github, proposed(repo, at_day(1)).proposals)
    args = ["--repo", str(repo.root), "close-proposal", "--lane", "rc", "--version", "1.1.0rc1"]
    assert main(args, repo.github) == 0
    assert capsys.readouterr().out == f"closed proposal #{rc.issue}: v1.1.0rc1 released\n"
    assert rc.issue in repo.github.closed


LAND_CALLER = """\
jobs:
  land:
    uses: romamo/shipmill/.github/workflows/land.yml@v0
    permissions:
      contents: write
      actions: write
{issues}"""


def test_doctor_warns_on_a_land_job_that_cannot_close_the_proposal_only_under_propose(repo: Repo) -> None:
    repo.write(".github/workflows/release.yml", LAND_CALLER.format(issues=""))

    def land_warnings() -> list[str]:
        found = doctor(repo.root, repo.github)
        return [c.detail for c in found if c.name == "permissions" and "land job" in c.detail]

    assert land_warnings() == []
    set_autonomy(repo, 'release = "propose"\n')
    [detail] = land_warnings()
    assert detail.startswith("release autonomy is propose") and detail.endswith("grants no `issues: write`: add it")
    repo.write(".github/workflows/release.yml", LAND_CALLER.format(issues="      issues: write\n"))
    assert land_warnings() == []


def test_land_yml_closes_the_proposal_with_the_callers_grant_only() -> None:
    # as prepare.yml's propose job: no job asks for `issues`, and no workflow-level block
    # lends one to the job that takes the caller's grant
    text = (Path(__file__).parent.parent / ".github" / "workflows" / "land.yml").read_text()
    assert not re.search(r"^\s+issues:", text, re.MULTILINE)
    assert "\npermissions:" not in text
    job = text.split("\n  close-proposal:\n", 1)[1].split("\n  cleanup:\n", 1)[0]
    assert "    permissions:" not in job and job.startswith("    needs: land\n")
    assert 'shipmill close-proposal --lane "$LANE" --version "$VERSION"' in job
