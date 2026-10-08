"""The lanes end to end, against a bare origin: plan, prepare (stamp, commit, push), land
(push, tag, sync, publish), as the workflows run them"""

import datetime as dt
from pathlib import Path

import pytest

from shipmill.changelog import Changelog
from shipmill.cli import main
from shipmill.errors import ReleaseError
from shipmill.github import WorkflowRun
from shipmill.gitrepo import Git
from shipmill.land import ActionsRun, Stop, cleanup, land, prepare, work_branch
from shipmill.planner import Decision, Event, Hotfix, Planner
from shipmill.policy import Lane, Mode, Policy, Style
from shipmill.version import Version

from .conftest import Repo, at_day


def plan(
    repo: Repo,
    now: dt.datetime,
    event: Event = Event.SCHEDULE,
    lane: Lane | None = None,
    hotfix: Hotfix | None = None,
) -> Decision:
    repo.at(now)  # the release this plan leads to is made at the same time
    repo.git.fetch("+refs/heads/main:refs/remotes/origin/main")
    repo.git.run("checkout", "-q", "--detach", "origin/main")
    return Planner(repo.git, repo.policy, repo.github, now).plan(event, lane, hotfix=hotfix)


def release(repo: Repo, decision: Decision) -> str:
    """Run prepare and land as the workflows do; returns the release commit"""
    assert decision.action == "release", decision.reason
    assert decision.lane is not None and decision.version is not None
    day = dt.date.fromisoformat(repo.git.env["GIT_COMMITTER_DATE"][:10])
    prepared = prepare(
        repo.git,
        repo.policy,
        decision.lane,
        decision.version,
        decision.base,
        day,
        decision.merges,
        decision.prs,
        commit=True,
        push=True,
    )
    assert prepared.pushed
    land(repo.git, repo.policy, repo.github, decision.lane, decision.version, prepared.sha, decision.base, day)
    assert cleanup(repo.git, decision.version)
    return prepared.sha


def test_nothing_pending_skips_every_lane(repo: Repo) -> None:
    decision = plan(repo, at_day(1))
    assert decision.action == "skip"
    assert "rc: nothing pending" in decision.reason
    assert "dev: v1.0.0 already holds main's head" in decision.reason


def test_rc_lane_cuts_a_detached_release(repo: Repo) -> None:
    repo.merge(1, "Added", "Feature A", "src/a.py", "A = 1\n")
    main_before = repo.git.remote_branch("main")
    decision = plan(repo, at_day(1))
    assert (decision.lane, str(decision.version)) == (Lane.RC, "1.1.0rc1")
    sha = release(repo, decision)
    assert repo.git.remote_branch("main") == main_before  # a pre-release never moves main
    assert repo.git.remote_tag("v1.1.0rc1")
    assert repo.git.show(sha, repo.policy_file) is not None
    assert 'version = "1.1.0rc1"' in (repo.git.show(sha, "pyproject.toml") or "")
    assert 'version = "1.1.0rc1"' in (repo.git.show(sha, "uv.lock") or "")
    assert "demo 1.1.0rc1" in (repo.git.show(sha, "README.md") or "")
    assert "- Feature A (#1)" in (repo.git.show(sha, "CHANGELOG.md") or "").split("## [1.0.0]")[0]
    tag, title, notes, prerelease = repo.github.releases[-1]
    assert (tag, title, prerelease) == ("v1.1.0rc1", "demo 1.1.0rc1", True)
    assert notes.startswith("Changes since v1.0.0:\n\n### Added\n\n- Feature A (#1)")
    assert repo.git.remote_branch(work_branch(Version.parse("1.1.0rc1"))) is None


def test_next_rc_and_dev_build_follow_the_series(repo: Repo) -> None:
    repo.merge(1, "Added", "Feature A")
    release(repo, plan(repo, at_day(1)))
    repo.at(at_day(1, 8) + dt.timedelta(minutes=30))
    repo.merge(2, "Fixed", "Fix B")
    # today's rc window was used by rc1, so only the dev lane is due, 30 quiet minutes later
    decision = plan(repo, at_day(1, 9) + dt.timedelta(minutes=5))
    assert decision.lane is Lane.DEV
    assert str(decision.version).startswith("1.1.0rc2.dev")  # sorts after rc1, before rc2
    # tomorrow's window cuts rc2; rc1 has not soaked 3 days, so stable passes
    decision = plan(repo, at_day(2))
    assert str(decision.version) == "1.1.0rc2"
    assert (
        "stable: no rc has soaked 3 day(s)"
        in Planner(repo.git, repo.policy, repo.github, at_day(2)).plan(Event.MANUAL, Lane.STABLE).reason
    )


def test_blocker_and_freeze_hold_the_rc(repo: Repo) -> None:
    repo.merge(1, "Added", "Feature A")
    repo.github.blockers = ["#9 Data loss"]
    decision = plan(repo, at_day(1), event=Event.MANUAL, lane=Lane.RC)
    assert decision.action == "skip"
    assert "held, open 'release-blocker' issues: #9 Data loss" in decision.reason
    repo.github.blockers = []
    day = at_day(1).date()
    repo.write(
        repo.policy_file,
        repo.read(repo.policy_file).replace("[lanes.dev]", f'[gates]\nfreeze = ["{day}..{day}"]\n\n[lanes.dev]'),
    )
    decision = plan(repo, at_day(1))
    assert "frozen" in decision.reason and decision.action == "skip"


def test_stable_promotes_a_soaked_rc_and_syncs_main(repo: Repo) -> None:
    repo.merge(1, "Added", "Feature A", "src/a.py", "A = 1\n")
    release(repo, plan(repo, at_day(1)))
    rc_base = repo.git.remote_branch("main")
    repo.at(at_day(1, 9))
    repo.merge(2, "Fixed", "Fix B, after the rc")  # main moves on after the rc

    not_ripe = plan(repo, at_day(1, 9), event=Event.MANUAL, lane=Lane.STABLE)
    assert "soaked 3 day(s)" in not_ripe.reason

    decision = plan(repo, at_day(4), event=Event.MANUAL, lane=Lane.STABLE)
    assert (str(decision.version), decision.base) == ("1.1.0", rc_base)
    sha = release(repo, decision)

    # the release commit is the rc's code with the stable version, and only Feature A
    released = repo.git.show(sha, "CHANGELOG.md") or ""
    assert "## [1.1.0] - " in released and "Fix B" not in released
    assert repo.git.show(sha, "src/a.py") == "A = 1\n"
    # main got a sync commit: Feature A moved into 1.1.0, Fix B still pending, version 1.1.0
    main = repo.main_text("CHANGELOG.md")
    changelog = Changelog(main, Style.KEEP_A_CHANGELOG)
    assert [e.text for e in changelog.pending()] == ["- Fix B, after the rc (#2)"]
    assert changelog.section(Version.parse("1.1.0")) == "### Added\n\n- Feature A (#1)\n"
    assert 'version = "1.1.0"' in repo.main_text("pyproject.toml")
    assert "demo 1.1.0" in repo.main_text("README.md")
    assert "[Unreleased]: https://github.com/o/demo/compare/v1.1.0...HEAD" in main
    assert repo.github.releases[-1][:2] == ("v1.1.0", "demo 1.1.0")
    assert repo.github.releases[-1][3] is False
    assert repo.github.dispatched == [("publish.yml", "main", "v1.1.0", {})]

    # the next rc counts from 1.1.0: Fix B alone is a patch
    assert str(plan(repo, at_day(5)).version) == "1.1.1rc1"


def test_stable_on_an_unmoved_main_fast_forwards_it(repo: Repo) -> None:
    repo.merge(1, "Added", "Feature A")
    release(repo, plan(repo, at_day(1)))
    decision = plan(repo, at_day(4), event=Event.MANUAL, lane=Lane.STABLE)
    sha = release(repo, decision)
    assert repo.git.remote_branch("main") == sha  # no sync commit: main is the release commit
    assert "## [1.1.0]" in repo.main_text("CHANGELOG.md")


LEGACY_RC_SECTIONS = """\
# Changelog

## [Unreleased]

## [1.1.0rc2] - 2026-10-04

### Added

- Feature C (#3)

### Fixed

- Fix B (#2)

## [1.1.0rc1] - 2026-10-03

### Added

- Feature A (#1)

## [1.0.0] - 2026-09-01

### Added

- The first release

[Unreleased]: https://github.com/o/demo/compare/v1.1.0rc2...HEAD
[1.1.0rc2]: https://github.com/o/demo/compare/v1.1.0rc1...v1.1.0rc2
[1.1.0rc1]: https://github.com/o/demo/compare/v1.0.0...v1.1.0rc1
[1.0.0]: https://github.com/o/demo/releases/tag/v1.0.0
"""


def _tag_on_main(repo: Repo, changelog: str, tag: str) -> str:
    """A release commit an earlier release bot made on main: its CHANGELOG, tagged"""
    repo.git.run("checkout", "-q", "main")
    repo.write("CHANGELOG.md", changelog)
    repo.git.run("commit", "-q", "--allow-empty", "-am", f"Release {tag}")
    repo.git.run("tag", "-a", tag, "-m", tag)
    repo.git.run("push", "-q", "origin", "main", tag)
    return repo.git.sha()


def test_stable_folds_the_rc_sections_an_earlier_bot_wrote(repo: Repo) -> None:  # #243, D-25
    repo.at(at_day(0))
    rc_base = _tag_on_main(repo, LEGACY_RC_SECTIONS, "v1.1.0rc2")
    repo.at(at_day(1))
    repo.merge(4, "Fixed", "Fix D, after the rc")  # main moves on: the release syncs back

    decision = plan(repo, at_day(4), event=Event.MANUAL, lane=Lane.STABLE)
    assert (decision.action, str(decision.version), decision.base) == ("release", "1.1.0", rc_base)
    sha = release(repo, decision)

    # every entry of the rc sections, newest first, merged under their headings; no rc section left
    folded = "### Added\n\n- Feature C (#3)\n- Feature A (#1)\n\n### Fixed\n\n- Fix B (#2)\n"
    released = repo.git.show(sha, "CHANGELOG.md") or ""
    assert Changelog(released, Style.KEEP_A_CHANGELOG).section(Version.parse("1.1.0")) == folded
    assert "## [Unreleased]\n\n## [1.1.0] - " in released
    assert "1.1.0rc" not in released
    assert released.endswith(
        "[Unreleased]: https://github.com/o/demo/compare/v1.1.0...HEAD\n"
        "[1.1.0]: https://github.com/o/demo/compare/v1.0.0...v1.1.0\n"
        "[1.0.0]: https://github.com/o/demo/releases/tag/v1.0.0\n"
    )
    assert repo.github.releases[-1][:3] == ("v1.1.0", "demo 1.1.0", folded)

    # main's sync folds the same sections and keeps what landed after the rc pending
    main = Changelog(repo.main_text("CHANGELOG.md"), Style.KEEP_A_CHANGELOG)
    assert main.section(Version.parse("1.1.0")) == folded
    assert [e.text for e in main.pending()] == ["- Fix D, after the rc (#4)"]
    assert [str(x) for x in main.versions()] == ["1.1.0", "1.0.0"]


@pytest.mark.parametrize(
    ("changelog", "found"),
    [
        (None, "no [1.1.0rcN] section"),
        (
            LEGACY_RC_SECTIONS.split("### Added")[0] + "## [1.0.0] - 2026-09-01\n",
            "its rc sections [1.1.0rc2] hold no entries",
        ),
    ],
)
def test_a_promotion_with_nothing_to_fold_names_the_rc_sections(
    repo: Repo, changelog: str | None, found: str
) -> None:  # #243
    repo.at(at_day(0))
    _tag_on_main(repo, changelog or repo.read("CHANGELOG.md"), "v1.1.0rc2")
    decision = plan(repo, at_day(4), event=Event.MANUAL, lane=Lane.STABLE)
    assert decision.action == "skip"
    assert f"v1.1.0rc2's base has nothing pending under Unreleased, and {found}" in decision.reason


def test_hotfix_ships_chosen_prs_from_the_release_branch(repo: Repo) -> None:
    repo.merge(1, "Added", "Feature A, not ready", "src/a.py", "A = 1\n")
    repo.merge(2, "Fixed", "Urgent fix", "src/app.py", "VALUE = 2\n")
    decision = plan(repo, at_day(1), event=Event.MANUAL, lane=Lane.HOTFIX, hotfix=Hotfix((2,)))
    assert (str(decision.version), decision.prs) == ("1.0.1", (2,))
    sha = release(repo, decision)

    assert repo.git.remote_branch("release/1.0") == sha
    assert repo.git.show(sha, "src/app.py") == "VALUE = 2\n"
    assert repo.git.show(sha, "src/a.py") is None  # PR #1 is not in the hotfix
    released = repo.git.show(sha, "CHANGELOG.md") or ""
    assert "## [1.0.1] - " in released and "Urgent fix" in released and "Feature A" not in released
    main = Changelog(repo.main_text("CHANGELOG.md"), Style.KEEP_A_CHANGELOG)
    assert [e.text for e in main.pending()] == ["- Feature A, not ready (#1)"]
    assert "1.0.1" in [str(x) for x in main.versions()]

    # a second hotfix stacks on the release branch
    repo.merge(3, "Fixed", "Second fix", "src/b.py", "B = 1\n")
    second = plan(repo, at_day(1), event=Event.MANUAL, lane=Lane.HOTFIX, hotfix=Hotfix((3,)))
    assert (str(second.version), second.base) == ("1.0.2", sha)


def test_hotfix_refuses_a_pr_without_an_entry(repo: Repo) -> None:
    repo.git.run("checkout", "-q", "main")
    repo.write("src/app.py", "VALUE = 3\n")
    repo.git.run("commit", "-qam", "No entry")
    repo.git.run("push", "-q", "origin", "main")
    repo.github.merges[5] = repo.git.sha()
    decision = plan(repo, at_day(1), event=Event.MANUAL, lane=Lane.HOTFIX, hotfix=Hotfix((5,)))
    with pytest.raises(ReleaseError, match="adds no CHANGELOG entry"):
        prepare(
            repo.git,
            repo.policy,
            Lane.HOTFIX,
            Version.parse("1.0.1"),
            decision.base,
            at_day(1).date(),
            decision.merges,
            decision.prs,
            commit=True,
        )


def test_dev_lane_waits_for_quiet(repo: Repo) -> None:
    repo.at(at_day(1, 10))
    repo.merge(1, "Fixed", "Fix A")
    decision = plan(repo, at_day(1, 10) + dt.timedelta(minutes=5), event=Event.PUSH)
    assert decision.action == "release" and decision.lane is Lane.RC  # today's rc window is still open
    policy = repo.read(repo.policy_file)
    repo.write(repo.policy_file, policy.replace('schedule = ["daily 07:00 UTC"]', "schedule = []"))
    decision = plan(repo, at_day(1, 10) + dt.timedelta(minutes=5), event=Event.PUSH)
    assert decision.action == "skip" and "no trigger is due" in decision.reason
    decision = plan(repo, at_day(1, 10) + dt.timedelta(minutes=31), event=Event.PUSH)
    assert decision.lane is Lane.DEV and str(decision.version).startswith("1.0.1.dev")


def test_dry_run_and_off(repo: Repo) -> None:
    repo.merge(1, "Added", "Feature A")
    repo.git.fetch()
    repo.git.run("checkout", "-q", "--detach", "origin/main")
    planner = Planner(repo.git, repo.policy, repo.github, at_day(1))
    assert planner.plan(Event.SCHEDULE, dry_run=True).mode is Mode.DRY_RUN
    repo.write(
        repo.policy_file,
        repo.read(repo.policy_file).replace('mode = "release"', 'mode = "off"'),
    )
    assert plan(repo, at_day(1)).reason == "the policy's mode is off"


def test_unknown_heading_fails(repo: Repo) -> None:
    repo.merge(1, "Improved", "Something")
    with pytest.raises(ReleaseError, match="'Improved' is not in the policy's"):
        plan(repo, at_day(1))


def test_land_refuses_an_existing_tag(repo: Repo) -> None:
    repo.merge(1, "Added", "Feature A")
    decision = plan(repo, at_day(1))
    assert decision.version is not None and decision.lane is not None
    day = at_day(1).date()
    prepared = prepare(
        repo.git, repo.policy, decision.lane, decision.version, decision.base, day, commit=True, push=True
    )
    repo.git.run("push", "-q", "origin", f"{decision.base}:refs/tags/{decision.version.tag}")
    with pytest.raises(ReleaseError, match="exists on origin"):
        land(repo.git, repo.policy, repo.github, decision.lane, decision.version, prepared.sha, decision.base, day)


def test_prepare_names_a_branch_that_blocks_the_work_branch(repo: Repo) -> None:
    repo.merge(1, "Added", "Feature A")
    repo.git.run("push", "-q", "origin", "main:refs/heads/shipmill", "main:refs/heads/shipmill-x")
    decision = plan(repo, at_day(1))
    assert decision.version is not None and decision.lane is not None
    day = at_day(1).date()
    args = (repo.git, repo.policy, decision.lane, decision.version, decision.base, day)
    work = work_branch(decision.version)
    with pytest.raises(ReleaseError) as caught:
        prepare(*args, commit=True, push=True)
    assert str(caught.value) == (
        f"origin has a branch 'shipmill', which blocks the work branch {work}; delete or rename it"
    )
    assert repo.git.remote_branch(work) is None

    repo.git.run("push", "-q", "origin", ":refs/heads/shipmill")  # shipmill-x stays: it blocks nothing
    assert prepare(*args, commit=True, push=True).pushed
    assert repo.git.remote_branch(work) is not None


def _reject_tags(repo: Repo, times: int | None) -> None:
    """Make origin refuse tag pushes: the next `times` ones, or every one when None"""
    origin = Path(repo.git.run("remote", "get-url", "origin").strip())
    counter = origin / "rejections"
    counter.write_text("0", encoding="utf-8")
    limit = "999999" if times is None else str(times)
    hook = origin / "hooks" / "pre-receive"
    hook.write_text(
        "#!/bin/sh\n"
        "while read old new ref; do\n"
        '  case "$ref" in refs/tags/*)\n'
        f'    n=$(cat {counter}); if [ "$n" -lt {limit} ]; then echo $((n + 1)) > {counter};\n'
        '      echo "tags are refused for now" >&2; exit 1; fi;;\n'
        "  esac\n"
        "done\n",
        encoding="utf-8",
    )
    hook.chmod(0o755)


def _land_rc(repo: Repo, waits: tuple[int, ...]) -> tuple[Decision, str]:
    repo.merge(1, "Added", "Feature A")
    decision = plan(repo, at_day(1))
    assert decision.version is not None and decision.lane is not None
    day = at_day(1).date()
    prepared = prepare(
        repo.git, repo.policy, decision.lane, decision.version, decision.base, day, commit=True, push=True
    )
    land(repo.git, repo.policy, repo.github, decision.lane, decision.version, prepared.sha, decision.base, day, waits)
    return decision, prepared.sha


def test_a_rejected_tag_push_is_retried(repo: Repo) -> None:
    _reject_tags(repo, times=1)
    _, sha = _land_rc(repo, waits=(0, 0))
    assert repo.git.remote_tag("v1.1.0rc1")
    assert repo.github.releases[-1][0] == "v1.1.0rc1"
    assert sha


def test_a_refused_tag_push_reports_gits_error(repo: Repo) -> None:
    _reject_tags(repo, times=None)
    with pytest.raises(ReleaseError, match="rejected 2 times: .*tags are refused for now"):
        _land_rc(repo, waits=(0, 0))


ENVIRONMENTS = """
[environments.staging]
lane = "rc"
workflow = "deploy.yml"
health = "https://staging.example.com/health"

[environments.production]
from = "staging"
workflow = "deploy.yml"
bake_minutes = 60

[environments.preview]
lane = "rc"
workflow = "preview.yml"

[environments.nightly]
lane = "dev"
workflow = "deploy.yml"
"""


def test_land_deploys_each_environment_that_takes_the_lane(repo: Repo) -> None:
    repo.write(repo.policy_file, repo.read(repo.policy_file) + ENVIRONMENTS)
    repo.git.run("commit", "-qam", "Deploy to environments")
    repo.git.run("push", "-q", "origin", "main")
    repo.merge(1, "Added", "Feature A")
    decision = plan(repo, at_day(1))
    assert decision.version is not None and decision.lane is Lane.RC
    day = at_day(1).date()
    prepared = prepare(
        repo.git, repo.policy, decision.lane, decision.version, decision.base, day, commit=True, push=True
    )
    landed = land(repo.git, repo.policy, repo.github, decision.lane, decision.version, prepared.sha, decision.base, day)
    # each environment that takes the lane, once; production is promoted from staging, and
    # nightly takes the dev lane
    assert repo.github.dispatched == [
        ("deploy.yml", "v1.1.0rc1", "v1.1.0rc1", {"environment": "staging"}),
        ("preview.yml", "v1.1.0rc1", "v1.1.0rc1", {"environment": "preview"}),
    ]
    assert landed.published == ("github-release", "deploy.yml@staging", "preview.yml@preview")


# A Release run whose cleanup never got a runner leaves its work branch (#175, D-18)
WORKFLOW_REF = "o/demo/.github/workflows/release.yml@refs/heads/main"
THIS_RUN = ActionsRun("release.yml", 20)


Args = tuple[Git, Policy, Lane, Version, str, dt.date]  # prepare's positional arguments


def _orphan(repo: Repo) -> tuple[Args, str, str]:
    """A due rc whose work branch an earlier run left on origin: prepare's arguments, the
    work branch, and the commit it holds"""
    repo.merge(1, "Added", "Feature A")
    decision = plan(repo, at_day(1))
    assert decision.version is not None and decision.lane is not None
    work = work_branch(decision.version)
    left = repo.git.sha("origin/main")
    repo.git.run("push", "-q", "origin", f"{left}:refs/heads/{work}")
    args: Args = (repo.git, repo.policy, decision.lane, decision.version, decision.base, at_day(1).date())
    return args, work, left


def _run(number: int, status: str = "in_progress") -> WorkflowRun:
    return WorkflowRun(number, status, at_day(1))


@pytest.mark.parametrize("status", ["queued", "in_progress", "waiting"])
def test_prepare_leaves_a_work_branch_another_active_run_may_own(repo: Repo, status: str) -> None:
    args, work, left = _orphan(repo)
    repo.github.active["release.yml"] = [_run(20), _run(19, status)]
    prepared = prepare(*args, commit=True, push=True, github=repo.github, run=THIS_RUN)
    assert (prepared.pushed, prepared.stop, prepared.found) == (False, Stop.OWNED, left)
    assert repo.git.remote_branch(work) == left


def test_prepare_replaces_a_work_branch_no_other_run_owns(repo: Repo) -> None:
    args, work, left = _orphan(repo)
    repo.github.active["release.yml"] = [_run(20)]  # this run alone; other workflows' runs don't count
    repo.github.active["ci.yml"] = [_run(21)]
    prepared = prepare(*args, commit=True, push=True, github=repo.github, run=THIS_RUN)
    assert prepared.pushed and prepared.recovered and prepared.stop is None
    assert prepared.found == left and prepared.sha != left
    assert repo.git.remote_branch(work) == prepared.sha


def test_prepare_stops_when_the_token_cant_list_runs(repo: Repo) -> None:
    args, work, left = _orphan(repo)
    repo.github.actions_error = "Resource not accessible by integration (HTTP 403)"
    prepared = prepare(*args, commit=True, push=True, github=repo.github, run=THIS_RUN)
    assert (prepared.pushed, prepared.stop, prepared.found) == (False, Stop.FORBIDDEN, left)
    assert repo.git.remote_branch(work) == left


def test_prepare_fails_on_any_other_error_listing_runs(repo: Repo) -> None:
    args, work, left = _orphan(repo)
    repo.github.actions_error = "HTTP 404: Not Found"
    with pytest.raises(ReleaseError, match="HTTP 404"):
        prepare(*args, commit=True, push=True, github=repo.github, run=THIS_RUN)
    assert repo.git.remote_branch(work) == left


def test_prepare_outside_actions_leaves_the_work_branch(repo: Repo) -> None:
    args, work, left = _orphan(repo)
    prepared = prepare(*args, commit=True, push=True, github=repo.github)
    assert (prepared.pushed, prepared.stop) == (False, Stop.OUTSIDE)
    assert repo.git.remote_branch(work) == left


def test_prepare_without_push_never_asks_for_runs(repo: Repo) -> None:
    args, work, left = _orphan(repo)
    repo.github.actions_error = "must not be called"
    prepared = prepare(*args, commit=True, github=repo.github, run=THIS_RUN)
    assert prepared.sha and not prepared.pushed and prepared.stop is None
    assert repo.git.remote_branch(work) == left


def test_actions_run_is_read_from_githubs_variables() -> None:
    assert ActionsRun.parse(WORKFLOW_REF, "37362724489") == ActionsRun("release.yml", 37362724489)
    assert ActionsRun.parse("", "") is None
    for ref, run_id in [(WORKFLOW_REF, ""), ("release.yml", "1"), (WORKFLOW_REF, "x")]:
        with pytest.raises(ReleaseError, match="name no workflow run"):
            ActionsRun.parse(ref, run_id)


def _cli_prepare(repo: Repo, tmp_path: Path, run_id: str) -> tuple[str, str]:
    """`shipmill prepare --push` as the workflow runs it: its GITHUB_OUTPUT, stdout, and step summary"""
    decision = plan(repo, at_day(1))
    assert decision.version is not None and decision.lane is not None
    output, summary = tmp_path / "output", tmp_path / "summary"
    argv = [
        "--repo", str(repo.root), "prepare", "--lane", decision.lane.value, "--version", str(decision.version),
        "--base", decision.base, "--date", "2026-10-06", "--push", "--github-output", str(output),
        "--workflow-ref", WORKFLOW_REF, "--run-id", run_id, "--step-summary", str(summary),
    ]  # fmt: skip
    assert main(argv, repo.github) == 0
    return output.read_text("utf-8"), summary.read_text("utf-8") if summary.exists() else ""


def test_cli_warns_about_a_held_work_branch_and_stays_green(
    repo: Repo, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _, work, left = _orphan(repo)
    repo.github.actions_error = "Resource not accessible by integration (HTTP 403)"
    output, summary = _cli_prepare(repo, tmp_path, "20")
    out = capsys.readouterr()
    assert "sha=\n" in output
    assert f"::warning title=Work branch held::{work} is on origin at {left[:12]};" in out.out
    assert "grant `actions: read` to the prepare job in .github/workflows/release.yml" in out.out
    assert summary.startswith(f"Work branch held: {work} is on origin at {left[:12]};")
    assert "grant `actions: read`" in out.err


def test_cli_reports_a_replaced_work_branch(repo: Repo, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _, work, left = _orphan(repo)
    repo.github.active["release.yml"] = [_run(20)]
    output, summary = _cli_prepare(repo, tmp_path, "20")
    sha = repo.git.remote_branch(work)
    assert sha is not None and sha != left and f"sha={sha}\n" in output
    assert "::notice title=Orphaned work branch replaced::" in capsys.readouterr().out
    assert "no other run of release.yml is queued or in progress" in summary


STABLE_1_1_0 = """\
# Changelog

## [Unreleased]

## [1.1.0] - 2026-10-09

### Fixed

- Fix B (#2)

### Added

- Feature A (#1)

## [1.0.0] - 2026-09-01

### Added

- The first release

[Unreleased]: https://github.com/o/demo/compare/v1.1.0...HEAD
[1.1.0]: https://github.com/o/demo/compare/v1.0.0...v1.1.0
[1.0.0]: https://github.com/o/demo/releases/tag/v1.0.0
"""


def test_s013_1_without_fragments_plan_stamp_and_notes_are_unchanged(
    repo: Repo, capsys: pytest.CaptureFixture[str]
) -> None:
    """The planner and stamp fixtures with no [changelog] fragments, pinned whole as they were
    before spec 013: the plan's outputs, the stamped files and CHANGELOG, and the notes"""
    assert repo.policy.fragments is None
    skipped = plan(repo, at_day(1))
    assert skipped.outputs()["reason"] == (
        "stable: no rc after 1.0.0 to promote; rc: nothing pending under Unreleased; "
        "dev: v1.0.0 already holds main's head"
    )
    repo.merge(1, "Added", "Feature A", "src/a.py", "A = 1\n")
    repo.merge(2, "Fixed", "Fix B")
    rc = plan(repo, at_day(1))
    assert rc.outputs() == {
        "mode": "release",
        "action": "release",
        "reason": "rc 1.1.0rc1: 2 pending entries; window 'daily 07:00 UTC' opened at 2026-10-06 07:00 UTC",
        "lane": "rc",
        "version": "1.1.0rc1",
        "base": rc.base,
        "merges": "",
        "prs": "",
        "proposals": "",
    }
    rc_sha = release(repo, rc)
    assert repo.git.run("diff", "--name-only", rc.base, rc_sha).split() == ["README.md", "pyproject.toml", "uv.lock"]
    notes = "Changes since v1.0.0:\n\n### Fixed\n\n- Fix B (#2)\n\n### Added\n\n- Feature A (#1)\n"
    assert repo.github.releases[-1][2] == notes
    assert main(["--repo", str(repo.root), "notes", "--version", "1.1.0rc1"], repo.github) == 0
    assert capsys.readouterr().out == notes

    stable = plan(repo, at_day(4), event=Event.MANUAL, lane=Lane.STABLE)
    assert stable.reason == "stable 1.1.0: promotes v1.1.0rc1, soaked 3 day(s); started by hand"
    assert stable.version is not None
    prepared = prepare(
        repo.git, repo.policy, Lane.STABLE, stable.version, stable.base, at_day(4).date(), commit=True, push=True
    )
    assert prepared.changed == ("CHANGELOG.md", "pyproject.toml", "uv.lock", "README.md")
    assert repo.git.show(prepared.sha, "CHANGELOG.md") == STABLE_1_1_0
    assert repo.git.run("diff", "--name-only", stable.base, prepared.sha).split() == [
        "CHANGELOG.md",
        "README.md",
        "pyproject.toml",
        "uv.lock",
    ]
