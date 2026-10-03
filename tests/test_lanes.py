"""The lanes end to end, against a bare origin: plan, prepare (stamp, commit, push), land
(push, tag, sync, publish), as the workflows run them"""

import datetime as dt
from pathlib import Path

import pytest

from shipyard.changelog import Changelog
from shipyard.errors import ReleaseError
from shipyard.land import cleanup, land, prepare, work_branch
from shipyard.planner import Decision, Event, Hotfix, Planner
from shipyard.policy import Lane, Mode, Style
from shipyard.version import Version

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
    assert repo.git.show(sha, "pyproject.toml") is not None
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
        ".github/release-policy.toml",
        repo.read(".github/release-policy.toml").replace(
            "[lanes.dev]", f'[gates]\nfreeze = ["{day}..{day}"]\n\n[lanes.dev]'
        ),
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
    assert repo.github.dispatched == [("publish.yml", "main", "v1.1.0")]

    # the next rc counts from 1.1.0: Fix B alone is a patch
    assert str(plan(repo, at_day(5)).version) == "1.1.1rc1"


def test_stable_on_an_unmoved_main_fast_forwards_it(repo: Repo) -> None:
    repo.merge(1, "Added", "Feature A")
    release(repo, plan(repo, at_day(1)))
    decision = plan(repo, at_day(4), event=Event.MANUAL, lane=Lane.STABLE)
    sha = release(repo, decision)
    assert repo.git.remote_branch("main") == sha  # no sync commit: main is the release commit
    assert "## [1.1.0]" in repo.main_text("CHANGELOG.md")


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
    policy = repo.read(".github/release-policy.toml")
    repo.write(".github/release-policy.toml", policy.replace('schedule = ["daily 07:00 UTC"]', "schedule = []"))
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
        ".github/release-policy.toml",
        repo.read(".github/release-policy.toml").replace('mode = "release"', 'mode = "off"'),
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
