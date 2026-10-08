"""Changelog fragments (spec 013): read at the planned revision, pending alongside Unreleased,
written into a stable release's section and deleted by its release commit, untouched by dev
and rc releases"""

import subprocess
import sys

import pytest

from shipmill import fragments
from shipmill.changelog import Changelog, Entry, fragment_entries
from shipmill.cli import main
from shipmill.doctor import doctor
from shipmill.errors import ReleaseError
from shipmill.land import prepare
from shipmill.planner import Decision, Event, Hotfix, Proposal
from shipmill.policy import Lane, Style
from shipmill.propose import propose
from shipmill.version import Version

from .conftest import Repo, at_day
from .test_lanes import _tag_on_main, plan, release

FOLDER = "changelog.d"
README = "# Changelog fragments\n\nOne file per pull request, such as `244-merged-pr-head-landed.md`.\n"


def use_fragments(repo: Repo, readme: bool = True, policy: str = "") -> None:
    """Set [changelog] fragments on main, with the folder's README.md unless readme is False"""
    repo.git.run("checkout", "-q", "main")
    text = policy or repo.read(repo.policy_file)
    repo.write(repo.policy_file, text.replace("[changelog]\n", f'[changelog]\nfragments = "{FOLDER}"\n', 1))
    if readme:
        repo.write(f"{FOLDER}/README.md", README)
    repo.git.run("add", "-A")
    repo.git.run("commit", "-q", "-m", "Use changelog fragments")
    repo.git.run("push", "-q", "origin", "main")


def add_fragment(repo: Repo, name: str, text: str) -> str:
    """Land a pull request that adds one fragment on main; its merge commit"""
    repo.git.run("checkout", "-q", "main")
    repo.git.run("reset", "-q", "--hard", "origin/main")
    repo.write(f"{FOLDER}/{name}", text)
    repo.git.run("add", "-A")
    repo.git.run("commit", "-q", "-m", f"Add {name}")
    repo.git.run("push", "-q", "origin", "main")
    return repo.git.sha()


def released_section(repo: Repo, sha: str, version: str, style: Style = Style.KEEP_A_CHANGELOG) -> str:
    return Changelog(repo.git.show(sha, "CHANGELOG.md") or "", style).section(Version.parse(version))


def folder_files(repo: Repo, rev: str) -> list[str]:
    return repo.git.run("ls-tree", "-r", "--name-only", rev, "--", f"{FOLDER}/").split()


def test_s013_2_a_missing_folder_fails_plan_and_doctor_with_exit_2(repo: Repo) -> None:
    use_fragments(repo, readme=False)
    for argv in (["plan", "--event", "workflow_dispatch"], ["doctor"]):
        done = subprocess.run(
            [sys.executable, "-m", "shipmill", "--repo", str(repo.root), *argv], capture_output=True, text=True
        )
        assert done.returncode == 2, (argv, done.stdout, done.stderr)
        assert f"[changelog] fragments names {FOLDER}, which is not a folder" in done.stderr


@pytest.mark.parametrize(
    ("name", "text", "error"),
    [
        ("1-empty.md", "\n\n", "changelog.d/1-empty.md: an empty fragment"),
        ("2-prose.md", "### Added\n\n- Feature A (#2)\n\nSome prose\n", "changelog.d/2-prose.md:5: text outside"),
        ("3-bare.md", "- Feature A (#3)\n", "changelog.d/3-bare.md:1: an entry has no '### ' category heading"),
        ("4-version.md", "## [Unreleased]\n\n### Added\n\n- A (#4)\n", "changelog.d/4-version.md:1: a '## ' heading"),
        ("sub/5-nested.md", "### Added\n\n- A (#5)\n", "changelog.d/sub/5-nested.md: a fragment sits right in"),
        ("6-text.txt", "### Added\n\n- A (#6)\n", "changelog.d/6-text.txt: not a fragment"),
        ("7-heading-only.md", "### Added\n", "changelog.d/7-heading-only.md: the fragment holds no entry"),
    ],
)
def test_s013_3_a_malformed_fragment_fails_plan_naming_the_file(repo: Repo, name: str, text: str, error: str) -> None:
    use_fragments(repo)
    add_fragment(repo, name, text)
    with pytest.raises(ReleaseError, match=f"^{error}"):
        plan(repo, at_day(1))
    # as `shipmill plan` runs it: exit 2, the file named on stderr
    done = subprocess.run(
        [sys.executable, "-m", "shipmill", "--repo", str(repo.root), "plan", "--event", "workflow_dispatch"],
        capture_output=True,
        text=True,
    )
    assert done.returncode == 2 and error in done.stderr


NOTHING = "rc: nothing pending under Unreleased or in changelog.d"


def rc_skip(repo: Repo) -> str:
    """The rc lane's reason, started by hand: the dev lane builds any new head, so a
    scheduled plan would release a dev build instead"""
    decision = plan(repo, at_day(1), event=Event.MANUAL, lane=Lane.RC)
    assert decision.action == "skip", decision.reason
    return decision.reason


def test_s013_3_readme_in_the_folder_is_never_a_fragment(repo: Repo) -> None:
    use_fragments(repo)  # README.md holds prose that, as a fragment, would fail to read
    assert rc_skip(repo) == NOTHING


def test_s013_4_a_fragment_alone_plans_the_release_its_heading_bumps(repo: Repo) -> None:
    use_fragments(repo)
    assert rc_skip(repo) == NOTHING
    add_fragment(repo, "1-feature-a.md", "### Added\n\n- Feature A (#1)\n")
    decision = plan(repo, at_day(1))
    assert (decision.action, decision.lane, str(decision.version)) == ("release", Lane.RC, "1.1.0rc1")
    assert decision.reason.startswith("rc 1.1.0rc1: 1 pending entries")


def test_s013_4_a_fix_fragment_bumps_the_patch(repo: Repo) -> None:
    use_fragments(repo)
    add_fragment(repo, "1-fix.md", "### Fixed\n\n- Fix A (#1)\n")
    assert str(plan(repo, at_day(1)).version) == "1.0.1rc1"


def test_s013_5_the_planner_reads_fragments_at_the_revision_not_the_checkout(repo: Repo) -> None:
    use_fragments(repo)
    repo.git.run("checkout", "-q", "--detach", "origin/main")
    repo.write(f"{FOLDER}/1-local.md", "### Added\n\n- Only in the working tree (#1)\n")
    assert rc_skip(repo) == NOTHING
    # and the other way round: a committed fragment deleted from the working tree still counts
    (repo.root / FOLDER / "1-local.md").unlink()
    add_fragment(repo, "2-committed.md", "### Fixed\n\n- Fix B (#2)\n")
    repo.git.run("checkout", "-q", "--detach", "origin/main")
    (repo.root / FOLDER / "2-committed.md").unlink()
    assert str(plan(repo, at_day(1)).version) == "1.0.1rc1"


def test_s013_6_a_released_fragment_is_not_pending_and_doctor_names_it(repo: Repo) -> None:
    use_fragments(repo)
    add_fragment(repo, "1-old.md", "### Added\n\n- The first release\n")
    assert rc_skip(repo) == NOTHING
    repo.git.run("checkout", "-q", "main")
    checks = {(c.status, c.name): c.detail for c in doctor(repo.root, repo.github)}
    assert checks[("PASS", "changelog")] == "0 pending entries (Unreleased and 1 fragment in changelog.d)"
    assert checks[("WARN", "fragments")] == (
        "changelog.d/1-old.md: a released section holds every entry; delete it: git rm changelog.d/1-old.md"
    )


def test_doctor_reports_a_fragment_that_fails_to_read(repo: Repo) -> None:
    use_fragments(repo)
    add_fragment(repo, "1-bad.md", "- no heading (#1)\n")
    checks = {(c.status, c.name): c.detail for c in doctor(repo.root, repo.github)}
    assert checks[("FAIL", "changelog")].startswith("changelog.d/1-bad.md:1: an entry has no '### ' category heading")


STABLE_ONLY = """\
[lanes.stable]
promote_from = "rc"
min_soak_days = 3
"""


def test_s013_7_a_stable_release_writes_unreleased_then_fragments_and_deletes_them(repo: Repo) -> None:
    use_fragments(repo, policy=repo.read(repo.policy_file).replace(STABLE_ONLY, "[lanes.stable]\n"))
    repo.merge(1, "Fixed", "Fix U, under Unreleased")
    add_fragment(repo, "2-a.md", "### Fixed\n\n- Fix A (#2)\n")
    add_fragment(repo, "10-b.md", "### Added\n\n- Feature B (#10)\n\n### Fixed\n\n- Fix B (#10)\n")
    decision = plan(repo, at_day(1), event=Event.MANUAL, lane=Lane.STABLE)
    assert (decision.action, str(decision.version)) == ("release", "1.1.0")
    sha = release(repo, decision)

    # Unreleased first, then the fragments in file-name order ("10-b" sorts before "2-a"), grouped
    assert released_section(repo, sha, "1.1.0") == (
        "### Fixed\n\n- Fix U, under Unreleased (#1)\n- Fix B (#10)\n- Fix A (#2)\n\n### Added\n\n- Feature B (#10)\n"
    )
    assert "## [Unreleased]\n\n## [1.1.0] - " in (repo.git.show(sha, "CHANGELOG.md") or "")
    assert folder_files(repo, sha) == ["changelog.d/README.md"]
    assert repo.git.run("diff", "--name-status", decision.base, sha, "--", FOLDER).splitlines() == [
        "D\tchangelog.d/10-b.md",
        "D\tchangelog.d/2-a.md",
    ]
    assert repo.git.remote_branch("main") == sha  # main was the base: the release commit is main
    assert rc_skip(repo) == NOTHING


def test_s013_8_a_promotion_folds_unreleased_fragments_and_rc_sections_once(repo: Repo) -> None:
    from .test_lanes import LEGACY_RC_SECTIONS

    use_fragments(repo)
    # Fix B is in the rc2 section already: it is released once, from the fold
    add_fragment(repo, "5-e.md", "### Added\n\n- Feature E (#5)\n\n### Fixed\n\n- Fix B (#2)\n")
    repo.at(at_day(0))
    unreleased = LEGACY_RC_SECTIONS.replace("## [Unreleased]\n", "## [Unreleased]\n\n### Fixed\n\n- Fix U (#6)\n")
    rc_base = _tag_on_main(repo, unreleased, "v1.1.0rc2")

    decision = plan(repo, at_day(4), event=Event.MANUAL, lane=Lane.STABLE)
    assert (decision.action, str(decision.version), decision.base) == ("release", "1.1.0", rc_base)
    sha = release(repo, decision)

    assert released_section(repo, sha, "1.1.0") == (
        "### Fixed\n\n- Fix U (#6)\n- Fix B (#2)\n\n### Added\n\n- Feature E (#5)\n- Feature C (#3)\n- Feature A (#1)\n"
    )
    released = repo.git.show(sha, "CHANGELOG.md") or ""
    assert "1.1.0rc" not in released and released.count("Fix B (#2)") == 1
    assert folder_files(repo, sha) == ["changelog.d/README.md"]


def test_s013_9_dev_and_rc_releases_leave_the_folder_and_notes_list_fragments(
    repo: Repo, capsys: pytest.CaptureFixture[str]
) -> None:
    use_fragments(repo)
    repo.merge(1, "Fixed", "Fix U")
    add_fragment(repo, "2-a.md", "### Added\n\n- Feature A (#2)\n")
    rc = plan(repo, at_day(1))
    assert (rc.lane, str(rc.version)) == (Lane.RC, "1.1.0rc1")
    rc_sha = release(repo, rc)
    assert repo.git.run("diff", "--name-only", rc.base, rc_sha, "--", FOLDER) == ""
    notes = "Changes since v1.0.0:\n\n### Fixed\n\n- Fix U (#1)\n\n### Added\n\n- Feature A (#2)\n"
    assert repo.github.releases[-1][2] == notes
    assert main(["--repo", str(repo.root), "notes", "--version", "1.1.0rc1"], repo.github) == 0
    assert capsys.readouterr().out == notes

    add_fragment(repo, "3-b.md", "### Fixed\n\n- Fix B (#3)\n")
    dev = plan(repo, at_day(1, 10), event=Event.MANUAL, lane=Lane.DEV)
    assert dev.lane is Lane.DEV and dev.version is not None and dev.version.dev is not None
    dev_sha = release(repo, dev)
    assert repo.git.run("diff", "--name-only", dev.base, dev_sha, "--", FOLDER) == ""
    assert folder_files(repo, dev_sha) == ["changelog.d/2-a.md", "changelog.d/3-b.md", "changelog.d/README.md"]
    assert main(["--repo", str(repo.root), "notes", "--version", str(dev.version)], repo.github) == 0
    assert capsys.readouterr().out == (
        "Changes since v1.0.0:\n\n### Fixed\n\n- Fix U (#1)\n- Fix B (#3)\n\n### Added\n\n- Feature A (#2)\n"
    )


HEADINGS = 'from = "headings"\nmajor = ["Breaking"]\nminor = ["Added", "Changed"]\npatch = ["Fixed"]'
DASH_POLICY_CHANGES = (
    ('style = "keep-a-changelog"', 'style = "dash"'),
    (HEADINGS, 'from = "paths"\nminor_paths = ["src/*"]'),
)

DASH_CHANGELOG = """\
# Changelog

## Unreleased

## 1.0.0 — 2026-09-01

### The first release

It works.
"""


def test_s013_12_dash_fragments_are_pending_and_released(repo: Repo) -> None:
    policy = repo.read(repo.policy_file)
    for old, new in DASH_POLICY_CHANGES:
        assert old in policy
        policy = policy.replace(old, new)
    policy = policy.replace(STABLE_ONLY, "[lanes.stable]\n")
    repo.write("CHANGELOG.md", DASH_CHANGELOG)
    use_fragments(repo, policy=policy)
    assert rc_skip(repo) == NOTHING
    add_fragment(repo, "1-topic.md", "### Topic one\n\n- Detail\n\n**Why:** reasons.\n")
    assert str(plan(repo, at_day(1), event=Event.MANUAL, lane=Lane.RC).version) == "1.0.1rc1"

    decision = plan(repo, at_day(1), event=Event.MANUAL, lane=Lane.STABLE)
    sha = release(repo, decision)
    assert released_section(repo, sha, "1.0.1", Style.DASH) == "### Topic one\n\n- Detail\n\n**Why:** reasons.\n"
    assert (repo.git.show(sha, "CHANGELOG.md") or "").startswith("# Changelog\n\n## Unreleased\n\n## 1.0.1 — ")
    assert folder_files(repo, sha) == ["changelog.d/README.md"]


def test_a_proposal_lists_the_fragments_at_its_base(repo: Repo) -> None:
    use_fragments(repo)
    base = add_fragment(repo, "1-a.md", "### Added\n\n- Feature A (#1)\n")
    proposal = Proposal(Lane.RC, Version.parse("1.1.0rc1"), base, "release autonomy is propose")
    (done,) = propose(repo.git, repo.policy, repo.github, (proposal,))
    assert "#### Added\n\n- Feature A (#1)\n" in repo.github.issues[done.issue].body
    # proves: S-013-17
    older = Proposal(Lane.RC, Version.parse("1.1.0rc1"), repo.git.sha("v1.0.0"), "release autonomy is propose")
    repo.github.issues.clear()
    (done,) = propose(repo.git, repo.policy, repo.github, (older,))  # v1.0.0 predates the folder: none there
    assert "Nothing pending under Unreleased or in changelog.d." in repo.github.issues[done.issue].body


def test_fragment_entries_read_like_unreleased() -> None:
    text = "### Added\n\n- Feature A (#1)\n  continued\n\n### Fixed\n\n- Fix B (#1)\n"
    assert fragment_entries(text, Style.KEEP_A_CHANGELOG, "changelog.d/1.md") == [
        Entry("Added", "- Feature A (#1)\n  continued"),
        Entry("Fixed", "- Fix B (#1)"),
    ]
    with pytest.raises(ReleaseError, match=r"^changelog.d/1.md:1: text outside a '### ' entry"):
        fragment_entries("Prose\n### Topic\n", Style.DASH, "changelog.d/1.md")


def test_a_release_names_a_fragment_entry_it_cannot_find() -> None:
    changelog = Changelog(
        "# Changelog\n\n## [Unreleased]\n\n[Unreleased]: https://x/compare/v1.0.0...HEAD\n", Style.KEEP_A_CHANGELOG
    )
    gone = Entry("Added", "- Gone (#1)")
    with pytest.raises(ReleaseError, match="no longer under Unreleased or a fragment as released"):
        changelog.release(
            Version.parse("1.1.0"), at_day(0).date(), [gone], from_unreleased=True, fragments=[Entry("Added", "- B")]
        )


def merge_fragment(repo: Repo, pr: int, name: str, text: str, path: str = "", code: str = "") -> str:
    """Land pull request pr adding one fragment, and a code change when path is given"""
    if path:
        repo.git.run("checkout", "-q", "main")
        repo.git.run("reset", "-q", "--hard", "origin/main")
        repo.write(path, code)
    sha = add_fragment(repo, name, text)
    repo.github.merges[pr] = sha
    return sha


def commit_on_main(repo: Repo, message: str) -> str:
    repo.git.run("add", "-A")
    repo.git.run("commit", "-q", "-m", message)
    repo.git.run("push", "-q", "origin", "main")
    return repo.git.sha()


def hotfix_of(repo: Repo, *prs: int) -> tuple[str, Decision]:
    """Plan and release a hotfix of prs; its release commit and plan"""
    decision = plan(repo, at_day(1), event=Event.MANUAL, lane=Lane.HOTFIX, hotfix=Hotfix(prs))
    assert (decision.action, str(decision.version)) == ("release", "1.0.1"), decision.reason
    return release(repo, decision), decision


def test_s013_10_a_hotfix_ships_exactly_its_merges_fragments_and_entries(repo: Repo) -> None:
    use_fragments(repo)
    merge_fragment(repo, 1, "1-a.md", "### Added\n\n- Feature A, not ready (#1)\n", "src/a.py", "A = 1\n")
    # PR 2 adds an Unreleased entry and a fragment in one merge
    repo.write(f"{FOLDER}/2-b.md", "### Fixed\n\n- Fix B, as a fragment (#2)\n")
    repo.merge(2, "Fixed", "Fix B, under Unreleased", "src/app.py", "VALUE = 2\n")
    merge_fragment(repo, 3, "3-c.md", "### Fixed\n\n- Fix C (#3)\n\n### Security\n\n- Patch D (#3)\n")
    sha, decision = hotfix_of(repo, 2, 3)

    # PR 2's Unreleased entry, then its fragment, then PR 3's fragment; nothing of PR 1
    assert released_section(repo, sha, "1.0.1") == (
        "### Fixed\n\n- Fix B, under Unreleased (#2)\n- Fix B, as a fragment (#2)\n- Fix C (#3)\n\n"
        "### Security\n\n- Patch D (#3)\n"
    )
    assert repo.git.show(sha, "src/app.py") == "VALUE = 2\n" and repo.git.show(sha, "src/a.py") is None
    # none of the merges' fragment files reach the release branch, cut from v1.0.0, which
    # predates the folder: the hotfix reads only what each merge added
    assert folder_files(repo, sha) == []
    assert repo.git.run("diff", "--name-only", decision.base, sha, "--", FOLDER) == ""


def test_s013_10_a_merge_that_adds_neither_entry_nor_fragment_fails(repo: Repo) -> None:
    use_fragments(repo)
    repo.write("src/app.py", "VALUE = 3\n")
    repo.write(f"{FOLDER}/README.md", README + "\nEdited.\n")  # a change in the folder adds no fragment
    repo.github.merges[5] = commit_on_main(repo, "No entry")
    decision = plan(repo, at_day(1), event=Event.MANUAL, lane=Lane.HOTFIX, hotfix=Hotfix((5,)))
    with pytest.raises(ReleaseError, match="adds no CHANGELOG entry or fragment in changelog.d: a hotfix ships"):
        prepare(
            repo.git, repo.policy, Lane.HOTFIX, Version.parse("1.0.1"), decision.base, at_day(1).date(),
            decision.merges, decision.prs, commit=True,
        )  # fmt: skip


def test_s013_10_a_hotfix_without_fragments_configured_is_unchanged(repo: Repo) -> None:
    """No [changelog] fragments: a file under changelog.d is code like any other, applied to
    the release branch, and the section holds the Unreleased entries alone"""
    assert repo.policy.fragments is None
    repo.merge(2, "Fixed", "Urgent fix", f"{FOLDER}/2-b.md", "### Fixed\n\n- Not a fragment here (#2)\n")
    sha, _ = hotfix_of(repo, 2)
    assert released_section(repo, sha, "1.0.1") == "### Fixed\n\n- Urgent fix (#2)\n"
    assert folder_files(repo, sha) == ["changelog.d/2-b.md"]
    assert repo.main_text(f"{FOLDER}/2-b.md")  # the sync deletes nothing on main


def test_s013_11_sync_deletes_on_main_the_fragments_the_hotfix_released(repo: Repo) -> None:
    use_fragments(repo)
    merge_fragment(repo, 1, "1-a.md", "### Added\n\n- Feature A, not ready (#1)\n")
    merge_fragment(repo, 2, "2-b.md", "### Fixed\n\n- Fix B (#2)\n", "src/app.py", "VALUE = 2\n")
    merge_fragment(repo, 3, "3-c.md", "### Fixed\n\n- Fix C (#3)\n")
    # a fragment on main that shares an entry with the hotfix but holds another stays
    repo.write(f"{FOLDER}/4-d.md", "### Fixed\n\n- Fix C (#3)\n- Fix D (#4)\n")
    before_sync = commit_on_main(repo, "Add 4-d.md")
    hotfix_of(repo, 2, 3)

    synced = repo.git.sha("origin/main")
    assert repo.git.first_parent(synced) == before_sync
    assert repo.git.run("diff", "--name-status", before_sync, synced, "--", FOLDER).splitlines() == [
        "D\tchangelog.d/2-b.md",
        "D\tchangelog.d/3-c.md",
    ]
    on_main = Changelog(repo.main_text("CHANGELOG.md"), Style.KEEP_A_CHANGELOG)
    assert on_main.section(Version.parse("1.0.1")) == "### Fixed\n\n- Fix B (#2)\n- Fix C (#3)\n"
    left = fragments.at_revision(repo.git, repo.policy, synced)
    assert [e.text for e in on_main.pending(fragments.entries(left))] == ["- Feature A, not ready (#1)", "- Fix D (#4)"]


def test_s013_11_sync_exits_2_naming_an_entry_main_no_longer_holds(repo: Repo) -> None:
    use_fragments(repo)
    merge_fragment(repo, 2, "2-b.md", "### Fixed\n\n- Fix B (#2)\n", "src/app.py", "VALUE = 2\n")
    merge_fragment(repo, 3, "3-c.md", "### Fixed\n\n- Fix C (#3)\n")
    hotfix_of(repo, 2, 3)
    # main as it was before the sync commit, with Fix C's fragment gone
    repo.git.run("checkout", "-q", "--detach", "origin/main^1")
    repo.git.run("rm", "-q", f"{FOLDER}/3-c.md")
    done = subprocess.run(
        [sys.executable, "-m", "shipmill", "--repo", str(repo.root), "sync", "--version", "1.0.1"],
        capture_output=True,
        text=True,
    )
    assert done.returncode == 2, (done.stdout, done.stderr)
    assert "no longer under Unreleased or a fragment as released" in done.stderr
    assert "'- Fix C (#3)'" in done.stderr
    assert (repo.root / FOLDER / "2-b.md").is_file()  # a failed sync deletes nothing


def test_a_stable_sync_deletes_the_released_fragments_on_a_moved_main(repo: Repo) -> None:
    use_fragments(repo)
    merge_fragment(repo, 1, "1-a.md", "### Added\n\n- Feature A (#1)\n")
    release(repo, plan(repo, at_day(1)))
    repo.at(at_day(1, 9))
    merge_fragment(repo, 2, "2-b.md", "### Fixed\n\n- Fix B, after the rc (#2)\n")
    release(repo, plan(repo, at_day(4), event=Event.MANUAL, lane=Lane.STABLE))
    synced = repo.git.sha("origin/main")
    assert folder_files(repo, synced) == ["changelog.d/2-b.md", "changelog.d/README.md"]
    on_main = Changelog(repo.main_text("CHANGELOG.md"), Style.KEEP_A_CHANGELOG)
    assert on_main.section(Version.parse("1.1.0")) == "### Added\n\n- Feature A (#1)\n"


def test_s013_10_a_hotfix_ships_no_fragment_its_merge_only_renamed(repo: Repo) -> None:
    """A merge that renames another PR's fragment adds none of its entries: the hotfix ships
    only the merge's own fragment, and the renamed one stays pending on main"""
    use_fragments(repo)
    merge_fragment(repo, 1, "1-a.md", "### Added\n\n- Feature A, not ready (#1)\n", "src/a.py", "A = 1\n")
    repo.git.run("mv", f"{FOLDER}/1-a.md", f"{FOLDER}/1-feature-a.md")
    repo.write(f"{FOLDER}/2-b.md", "### Fixed\n\n- Fix B (#2)\n")
    repo.github.merges[2] = commit_on_main(repo, "Rename 1-a.md, add 2-b.md")
    sha, _ = hotfix_of(repo, 2)
    assert released_section(repo, sha, "1.0.1") == "### Fixed\n\n- Fix B (#2)\n"
    assert repo.main_text(f"{FOLDER}/1-feature-a.md")


def test_s013_10_a_hotfix_ships_the_entry_its_merge_added_to_another_fragment(repo: Repo) -> None:
    """A merge that adds an entry to an existing fragment ships that entry alone; the
    fragment, partly released, stays on main with its other entry pending"""
    use_fragments(repo)
    merge_fragment(repo, 1, "1-a.md", "### Added\n\n- Feature A, not ready (#1)\n", "src/a.py", "A = 1\n")
    repo.write(f"{FOLDER}/1-a.md", "### Added\n\n- Feature A, not ready (#1)\n\n### Fixed\n\n- Fix B (#2)\n")
    repo.write("src/app.py", "VALUE = 2\n")
    repo.github.merges[2] = commit_on_main(repo, "Fix B in 1-a.md")
    sha, _ = hotfix_of(repo, 2)
    assert released_section(repo, sha, "1.0.1") == "### Fixed\n\n- Fix B (#2)\n"
    on_main = Changelog(repo.main_text("CHANGELOG.md"), Style.KEEP_A_CHANGELOG)
    left = fragments.at_revision(repo.git, repo.policy, repo.git.sha("origin/main"))
    assert [f.path for f in left] == [f"{FOLDER}/1-a.md"]
    assert [e.text for e in on_main.pending(fragments.entries(left))] == ["- Feature A, not ready (#1)"]


def rc_before_fragments(repo: Repo) -> Decision:
    """Release 1.1.0rc1 with no fragments configured, then turn fragments on; the rc's plan"""
    repo.merge(1, "Added", "Feature A")
    rc = plan(repo, at_day(1))
    assert (rc.lane, str(rc.version)) == (Lane.RC, "1.1.0rc1")
    release(repo, rc)
    use_fragments(repo)
    return rc


def test_s013_17_the_promotion_of_an_rc_cut_before_the_folder_plans(repo: Repo) -> None:
    rc = rc_before_fragments(repo)
    assert repo.git.show(rc.base, FOLDER) is None
    decision = plan(repo, at_day(4), event=Event.MANUAL, lane=Lane.STABLE)
    assert (decision.action, str(decision.version), decision.base) == ("release", "1.1.0", rc.base)


def test_s013_17_the_promotion_of_an_rc_cut_before_the_folder_releases(repo: Repo) -> None:
    """plan, prepare, and land: the stamp at the rc's base, which predates the folder, reads
    no fragments, and main keeps its folder after the sync"""
    rc = rc_before_fragments(repo)
    decision = plan(repo, at_day(4), event=Event.MANUAL, lane=Lane.STABLE)
    sha = release(repo, decision)
    assert repo.git.first_parent(sha) == rc.base
    assert released_section(repo, sha, "1.1.0") == "### Added\n\n- Feature A (#1)\n"
    assert folder_files(repo, sha) == []
    assert folder_files(repo, repo.git.sha("origin/main")) == ["changelog.d/README.md"]


def test_s013_17_a_stable_stamp_whose_base_config_sets_the_key_still_needs_the_folder(repo: Repo) -> None:
    repo.merge(1, "Added", "Feature A")
    use_fragments(repo, readme=False)
    base = repo.git.sha()
    with pytest.raises(ReleaseError, match=f"names {FOLDER}, which is not a folder in"):
        prepare(repo.git, repo.policy, Lane.STABLE, Version.parse("1.1.0"), base, at_day(1).date(), (), (), commit=True)


def test_s013_17_notes_run_against_a_tag_older_than_the_folder(repo: Repo, capsys: pytest.CaptureFixture[str]) -> None:
    rc_before_fragments(repo)
    assert main(["--repo", str(repo.root), "notes", "--version", "1.1.0rc1"], repo.github) == 0
    assert capsys.readouterr().out == "Changes since v1.0.0:\n\n### Added\n\n- Feature A (#1)\n"


def test_s013_17_a_revision_whose_config_sets_the_key_still_needs_the_folder(repo: Repo) -> None:
    use_fragments(repo, readme=False)
    asked = repo.git.sha()
    repo.git.run("tag", "-a", "v1.1.0rc1", "-m", "rc", asked)
    add_fragment(repo, "1-a.md", "### Added\n\n- Feature A (#1)\n")  # HEAD has the folder now
    done = subprocess.run(
        [sys.executable, "-m", "shipmill", "--repo", str(repo.root), "notes", "--version", "1.1.0rc1"],
        capture_output=True,
        text=True,
    )
    assert done.returncode == 2, (done.stdout, done.stderr)
    assert f"[changelog] fragments names {FOLDER}, which is not a folder at v1.1.0rc1" in done.stderr
    proposal = Proposal(Lane.RC, Version.parse("1.1.0rc1"), asked, "release autonomy is propose")
    with pytest.raises(ReleaseError, match=f"names {FOLDER}, which is not a folder at"):
        propose(repo.git, repo.policy, repo.github, (proposal,))


def test_s013_17_the_current_revision_still_needs_the_folder(repo: Repo) -> None:
    """HEAD is the revision shipmill runs on: a missing folder fails there even when only the
    checkout's config sets the key"""
    repo.write(
        repo.policy_file,
        repo.read(repo.policy_file).replace("[changelog]\n", f'[changelog]\nfragments = "{FOLDER}"\n', 1),
    )
    with pytest.raises(ReleaseError, match=f"names {FOLDER}, which is not a folder at"):
        fragments.at_revision(repo.git, repo.policy, "HEAD")
    with pytest.raises(ReleaseError, match=f"names {FOLDER}, which is not a folder at"):
        fragments.at_revision(repo.git, repo.policy, "v1.0.0")  # v1.0.0 is HEAD here


@pytest.mark.parametrize(
    ("change", "error"),
    [
        (("[changelog]\n", "[changelog\n"), r"^\.github/shipmill\.toml at [0-9a-f]{12}: "),
        (
            ("[changelog]\n", "[changelog]\nfragments = 1\n"),
            r"^\.github/shipmill\.toml at [0-9a-f]{12} \[changelog\]: fragments must be a string, got 1",
        ),
    ],
)
def test_s013_17_a_revision_whose_config_fails_to_read_fails(repo: Repo, change: tuple[str, str], error: str) -> None:
    good = repo.read(repo.policy_file)
    assert change[0] in good
    repo.write(repo.policy_file, good.replace(*change, 1))
    broken = commit_on_main(repo, "Break the config")
    use_fragments(repo, policy=good)
    with pytest.raises(ReleaseError, match=error):
        fragments.at_revision(repo.git, repo.policy, broken)


def test_s013_17_only_the_key_is_read_from_an_older_config(repo: Repo) -> None:
    """A config valid when it was committed may not pass today's schema; only [changelog]
    fragments is read from it"""
    good = repo.read(repo.policy_file)
    repo.write(repo.policy_file, 'retired_key = "since removed"\n' + good)
    older = commit_on_main(repo, "A config with a key today's schema rejects")
    use_fragments(repo, policy=good)
    assert fragments.at_revision(repo.git, repo.policy, older) == []


def test_s013_17_a_folder_older_than_the_key_is_still_read(repo: Repo) -> None:
    repo.git.run("checkout", "-q", "main")
    repo.write(f"{FOLDER}/1-a.md", "### Added\n\n- Feature A (#1)\n")
    early = commit_on_main(repo, "Add the folder before the key")
    use_fragments(repo)
    assert [f.path for f in fragments.at_revision(repo.git, repo.policy, early)] == [f"{FOLDER}/1-a.md"]
