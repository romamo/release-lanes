import datetime as dt
from pathlib import Path

import pytest

from shipmill.changelog import Changelog, Entry
from shipmill.cli import main
from shipmill.errors import ReleaseError
from shipmill.github import PROPOSAL_LABEL, Forbidden, GhCli
from shipmill.policy import Lane, Policy, Style
from shipmill.schedule import Freeze, Window
from shipmill.version import Part, Version

from .conftest import POLICY


def v(text: str) -> Version:
    return Version.parse(text)


class TestVersion:
    def test_pep440_order(self) -> None:
        ordered = ["1.4.0.dev3", "1.4.0a1", "1.4.0b2", "1.4.0rc1.dev9", "1.4.0rc1", "1.4.0rc2", "1.4.0", "1.4.1.dev1"]
        assert sorted(ordered, key=v) == ordered
        assert sorted(reversed(ordered), key=v) == ordered

    def test_round_trip_and_semver(self) -> None:
        assert str(v("1.4.0rc2.dev7")) == "1.4.0rc2.dev7"
        assert v("1.4.0rc2").semver == "1.4.0-rc.2"
        assert v("1.4.0.dev7").semver == "1.4.0-dev.7"
        assert v("1.4.0rc2.dev7").release == v("1.4.0")

    def test_bump(self) -> None:
        assert v("1.4.2").bump(Part.MAJOR) == v("2.0.0")
        assert v("1.4.2").bump(Part.MINOR) == v("1.5.0")
        assert v("1.4.2").bump(Part.PATCH) == v("1.4.3")
        assert v("0.4.2").bump(Part.MAJOR) == v("0.5.0")  # before 1.0 a major bump moves the minor

    @pytest.mark.parametrize("text", ["1.4", "v1.4.0", "1.4.0-rc.1", "1.4.0rc", "01.4.0x"])
    def test_rejects(self, text: str) -> None:
        with pytest.raises(ReleaseError):
            v(text)


class TestSchedule:
    def test_latest_start_skips_weekends(self) -> None:
        window = Window.parse("Mon-Fri 07:00 Europe/Kyiv")
        saturday = dt.datetime(2026, 10, 3, 12, 0, tzinfo=dt.UTC)
        assert window.latest_start(saturday) == dt.datetime(2026, 10, 2, 4, 0, tzinfo=dt.UTC)  # Fri 07:00 +03:00

    def test_before_todays_window_takes_yesterdays(self) -> None:
        window = Window.parse("daily 07:00 UTC")
        early = dt.datetime(2026, 10, 5, 6, 59, tzinfo=dt.UTC)
        assert window.latest_start(early) == dt.datetime(2026, 10, 4, 7, 0, tzinfo=dt.UTC)

    @pytest.mark.parametrize("text", ["Mon 7:00 UTC", "Fri-Mon 07:00 UTC", "Mon 25:00 UTC", "Mon 07:00 Mars/Base"])
    def test_rejects(self, text: str) -> None:
        with pytest.raises(ReleaseError):
            Window.parse(text)

    def test_freeze(self) -> None:
        freeze = Freeze.parse("2026-12-24..2027-01-02")
        assert freeze.holds(dt.datetime(2026, 12, 31, tzinfo=dt.UTC))
        assert not freeze.holds(dt.datetime(2027, 1, 3, tzinfo=dt.UTC))


class TestPolicy:
    def load(self, text: str) -> Policy:
        import tomllib

        return Policy.parse(tomllib.loads(text), "policy")

    def test_loads(self) -> None:
        policy = self.load(POLICY)
        assert set(policy.lanes) == set(Lane)
        assert policy.quiet_minutes == 30
        assert policy.rule(Lane.STABLE).promote
        assert policy.blocker_lanes == {Lane.RC, Lane.STABLE}

    @pytest.mark.parametrize(("mode", "minutes"), [("off", "0"), ("dry-run", "30"), ("release", "30")])
    def test_settle_minutes_is_0_when_the_mode_is_off(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str], mode: str, minutes: str
    ) -> None:
        # a private repo pays for the settle job's sleep; with mode off plan skips anyway (#318)
        path = tmp_path / ".github" / "shipmill.toml"
        path.parent.mkdir()
        path.write_text(POLICY.replace('mode = "release"', f'mode = "{mode}"'), encoding="utf-8")
        assert main(["--repo", str(tmp_path), "settle-minutes"]) == 0
        assert capsys.readouterr().out == f"{minutes}\n"
        assert self.load(POLICY.replace('mode = "release"', f'mode = "{mode}"')).quiet_minutes == 30

    def test_settle_minutes_is_0_with_no_quiet_lane(self) -> None:
        assert self.load(POLICY.replace("[lanes.dev]\nquiet_minutes = 30\n", "")).settle_minutes == 0

    @pytest.mark.parametrize(
        ("change", "message"),
        [
            (('mode = "release"', 'mode = "on"'), "mode must be one of"),
            (("[lanes.hotfix]\n", "[lanes.hotfix]\nschedule = ['Mon 07:00 UTC']\n"), "unknown keys"),
            (("quiet_minutes = 30", "quiet_minutes = 301"), "quiet_minutes"),
            (('patch = ["Fixed"]', 'patch = ["Added"]'), "twice"),
            (('dispatch = ["publish.yml"]', 'dispatch = ["../x.yml"]'), "dispatch"),
            (('name = "demo"\n', 'name = "demo"\ncolour = 1\n'), "unknown keys"),
        ],
    )
    def test_rejects(self, change: tuple[str, str], message: str) -> None:
        assert change[0] in POLICY
        with pytest.raises(ReleaseError, match=message):
            self.load(POLICY.replace(*change))

    def test_promotion_needs_rc_lane(self) -> None:
        text = POLICY.replace("[lanes.rc]\nschedule", "[lanes.unused]\nschedule")
        with pytest.raises(ReleaseError):
            self.load(text)


KAC = """\
# Changelog

Intro.

## [Unreleased]

### Added

- Feature A (#1)
- Feature B, with a long
  continuation line (#2)

### Fixed

- Fix C (#3)

## [1.0.0] - 2026-09-01

The first release.

### Added

- The first release

[Unreleased]: https://github.com/o/demo/compare/v1.0.0...HEAD
[1.0.0]: https://github.com/o/demo/releases/tag/v1.0.0
"""

DASH = """\
# Changelog

## Versioning

- Spec version

## Unreleased

### Topic one

- Detail

**Why:** reasons.

### Topic two

- Detail two

## 1.9.0 — 2026-09-30

### Old

- Old detail

## 1.6.0

- Undated
"""


class TestChangelog:
    def test_entries_keep_continuations(self) -> None:
        entries = Changelog(KAC, Style.KEEP_A_CHANGELOG).unreleased()
        assert [e.heading for e in entries] == ["Added", "Added", "Fixed"]
        assert entries[1].text == "- Feature B, with a long\n  continuation line (#2)"

    def test_release_moves_entries_and_links(self) -> None:
        changelog = Changelog(KAC, Style.KEEP_A_CHANGELOG)
        a, b, c = changelog.unreleased()
        text = changelog.release(v("1.1.0"), dt.date(2026, 10, 5), [a, c], from_unreleased=True)
        assert (
            "## [Unreleased]\n\n### Added\n\n- Feature B, with a long\n  continuation line (#2)\n\n## [1.1.0]" in text
        )
        assert (
            "## [1.1.0] - 2026-10-05\n\n### Added\n\n- Feature A (#1)\n\n### Fixed\n\n- Fix C (#3)\n\n## [1.0.0]"
            in text
        )
        assert "The first release.\n" in text  # an untouched section keeps its text
        assert text.endswith(
            "[Unreleased]: https://github.com/o/demo/compare/v1.1.0...HEAD\n"
            "[1.1.0]: https://github.com/o/demo/compare/v1.0.0...v1.1.0\n"
            "[1.0.0]: https://github.com/o/demo/releases/tag/v1.0.0\n"
        )
        after = Changelog(text, Style.KEEP_A_CHANGELOG)
        assert after.pending() == [b]
        assert after.versions() == [v("1.1.0"), v("1.0.0")]

    def test_older_release_goes_below_newer_and_keeps_unreleased_link(self) -> None:
        changelog = Changelog(KAC.replace("[1.0.0]", "[1.1.0]").replace("v1.0.0", "v1.1.0"), Style.KEEP_A_CHANGELOG)
        entry = changelog.unreleased()[2]
        text = changelog.release(v("1.0.1"), dt.date(2026, 10, 5), [entry], from_unreleased=True)
        assert text.index("## [1.1.0]") < text.index("## [1.0.1]")
        assert "[Unreleased]: https://github.com/o/demo/compare/v1.1.0...HEAD" in text
        assert "[1.0.1]: https://github.com/o/demo/releases/tag/v1.0.1" in text

    def test_missing_entry_fails(self) -> None:
        changelog = Changelog(KAC, Style.KEEP_A_CHANGELOG)
        with pytest.raises(ReleaseError, match="no longer under Unreleased"):
            changelog.release(v("1.1.0"), dt.date(2026, 10, 5), [Entry("Added", "- Gone")], from_unreleased=True)

    def test_dash_style(self) -> None:
        changelog = Changelog(DASH, Style.DASH)
        one, two = changelog.unreleased()
        assert one.text == "### Topic one\n\n- Detail\n\n**Why:** reasons."
        text = changelog.release(v("1.10.0"), dt.date(2026, 10, 5), [one, two], from_unreleased=True)
        assert "## Versioning\n\n- Spec version\n\n## Unreleased\n\n## 1.10.0 — 2026-10-05\n\n### Topic one" in text
        assert text.index("## 1.10.0") < text.index("## 1.9.0") < text.index("## 1.6.0")
        assert Changelog(text, Style.DASH).section(v("1.10.0")).startswith("### Topic one")

    @pytest.mark.parametrize(
        ("text", "message"),
        [
            (KAC.replace("### Added\n\n- Feature A", "- Feature A"), "no '### ' category"),
            (KAC.replace("- Fix C (#3)", "Fix C, not a bullet"), "outside a '- ' entry"),
            (KAC.replace("## [Unreleased]\n", ""), "exactly one"),
            (KAC.replace("2026-09-01", "Sept 1"), "YYYY-MM-DD"),
        ],
    )
    def test_strict(self, text: str, message: str) -> None:
        with pytest.raises(ReleaseError, match=message):
            Changelog(text, Style.KEEP_A_CHANGELOG).pending()

    def test_without_rc_sections_a_stable_release_holds_what_is_pending(self) -> None:  # #243
        changelog = Changelog(KAC, Style.KEEP_A_CHANGELOG)
        assert changelog.rc_sections(v("1.1.0")) == []
        assert changelog.promoted(v("1.1.0")) == changelog.pending()

    def test_folds_only_the_rc_sections_of_the_version(self) -> None:  # #243, D-25
        text = KAC.replace(
            "## [1.0.0]",
            "## [1.2.0rc1] - 2026-10-04\n\n### Added\n\n- Next series (#9)\n\n"
            "## [1.1.0rc1] - 2026-10-03\n\n### Fixed\n\n- Fix C (#3)\n- Fix D (#4)\n\n"
            "## [1.0.0]",
        ).replace("[1.0.0]: https", "[1.1.0rc1]: https://github.com/o/demo/compare/v1.0.0...v1.1.0rc1\n[1.0.0]: https")
        changelog = Changelog(text, Style.KEEP_A_CHANGELOG)
        assert changelog.rc_sections(v("1.1.0")) == [v("1.1.0rc1")]
        entries = changelog.promoted(v("1.1.0"))
        # Unreleased first, then the rc section; Fix C, in both, once
        assert [e.text for e in entries] == [
            "- Feature A (#1)",
            "- Feature B, with a long\n  continuation line (#2)",
            "- Fix C (#3)",
            "- Fix D (#4)",
        ]
        with pytest.raises(ReleaseError, match="1 of their entries are not in the release: '- Fix D"):
            changelog.release(v("1.1.0"), dt.date(2026, 10, 5), entries[:3], from_unreleased=True)
        after = changelog.release(v("1.1.0"), dt.date(2026, 10, 5), entries, from_unreleased=True)
        assert "1.1.0rc1" not in after
        assert "## [1.2.0rc1] - 2026-10-04\n\n### Added\n\n- Next series (#9)\n\n## [1.1.0] - 2026-10-05" in after
        assert Changelog(after, Style.KEEP_A_CHANGELOG).section(v("1.1.0")) == (
            "### Added\n\n- Feature A (#1)\n- Feature B, with a long\n  continuation line (#2)\n\n"
            "### Fixed\n\n- Fix C (#3)\n- Fix D (#4)\n"
        )
        assert "[1.1.0]: https://github.com/o/demo/compare/v1.0.0...v1.1.0\n" in after

    @pytest.mark.parametrize(
        "body",
        [
            "### Added\n\n- Fix C (#3)\n\nThis candidate reworks the parser.\n",
            "The intro.\n\n### Added\n\n- Fix C (#3)\n\nProse after the first heading.\n\n### Fixed\n\n- Fix D (#4)\n",
            "### Added\n\n- Fix C (#3)\n\n#### Internals\n\n- Fix D (#4)\n",
            "### Added\n\n1. Fix C (#3)\n",
            "- Fix C (#3)\n",
            "The intro.\n\n- Fix C (#3)\n\n### Fixed\n\n- Fix D (#4)\n",
            "The intro.\n\n#### Internals\n\n### Fixed\n\n- Fix D (#4)\n",
            "The intro.\n\n1. Fix C (#3)\n\n### Fixed\n\n- Fix D (#4)\n",
            "The intro.\n\n+ Fix C (#3)\n\n### Fixed\n\n- Fix D (#4)\n",
            "The intro:\n  - Fix C (#3)\n\n### Fixed\n\n- Fix D (#4)\n",
            "The intro:\n   2) Fix C (#3)\n\n### Fixed\n\n- Fix D (#4)\n",
        ],
    )
    def test_folding_refuses_an_rc_section_with_text_that_is_not_an_entry(self, body: str) -> None:  # #243, #283
        # the fold removes the rc section, so what it can't carry over must not vanish silently
        text = KAC.replace("## [1.0.0]", f"## [1.1.0rc1] - 2026-10-03\n\n{body}\n## [1.0.0]")
        changelog = Changelog(text, Style.KEEP_A_CHANGELOG)
        with pytest.raises(ReleaseError, match=r"1\.1\.0rc1, folded into 1\.1\.0: "):
            changelog.promoted(v("1.1.0"))

    @pytest.mark.parametrize(
        "intro",
        [
            "The 34th 1.0 release candidate: 2 additions and 1 fix.\n\n",
            "\n\nThe 34th 1.0 release candidate.\n\n\n",
            "A first paragraph,\nwrapped.\n\nA second paragraph.\n\n",
            "",
        ],
    )
    def test_folding_drops_the_prose_before_an_rc_sections_first_heading(self, intro: str) -> None:  # #283, D-26
        rc2 = f"## [1.1.0rc2] - 2026-10-04\n\n{intro}### Added\n\n- Feature D (#4)\n\n### Fixed\n\n- Fix E (#5)\n\n"
        rc1 = "## [1.1.0rc1] - 2026-10-03\n\nThe 1st 1.1 release candidate.\n\n### Fixed\n\n- Fix F (#6)\n\n"
        changelog = Changelog(KAC.replace("## [1.0.0]", f"{rc2}{rc1}## [1.0.0]"), Style.KEEP_A_CHANGELOG)
        entries = changelog.promoted(v("1.1.0"))
        assert [e.text for e in entries] == [
            "- Feature A (#1)",
            "- Feature B, with a long\n  continuation line (#2)",
            "- Fix C (#3)",
            "- Feature D (#4)",
            "- Fix E (#5)",
            "- Fix F (#6)",
        ]
        after = changelog.release(v("1.1.0"), dt.date(2026, 10, 5), entries, from_unreleased=True)
        assert "1.1.0rc" not in after and "release candidate" not in after and "paragraph" not in after
        assert Changelog(after, Style.KEEP_A_CHANGELOG).section(v("1.1.0")) == (
            "### Added\n\n- Feature A (#1)\n- Feature B, with a long\n  continuation line (#2)\n- Feature D (#4)\n\n"
            "### Fixed\n\n- Fix C (#3)\n- Fix E (#5)\n- Fix F (#6)\n"
        )

    def test_an_rc_section_with_only_an_intro_folds_to_nothing(self) -> None:  # #283
        rc = "## [1.1.0rc1] - 2026-10-03\n\nThe 1st 1.1 release candidate: no changes.\n\n"
        changelog = Changelog(KAC.replace("## [1.0.0]", f"{rc}## [1.0.0]"), Style.KEEP_A_CHANGELOG)
        assert changelog.rc_sections(v("1.1.0")) == [v("1.1.0rc1")]
        entries = changelog.promoted(v("1.1.0"))
        assert entries == changelog.pending()
        after = changelog.release(v("1.1.0"), dt.date(2026, 10, 5), entries, from_unreleased=True)
        assert "1.1.0rc1" not in after and "release candidate" not in after

    def test_dash_style_folding_drops_the_intro_and_keeps_text_after_a_title(self) -> None:  # #283, D-26
        rc2 = "## 1.10.0rc2 — 2026-10-04\n\n### Topic three\n\n- Detail three\n\n"
        rc1 = (
            "## 1.10.0rc1 — 2026-10-03\n\nThe 1st 1.10 release candidate.\n\nMore prose.\n\n"
            "### Topic four\n\n- Detail four\n\nNot an entry, but part of Topic four.\n\n"
        )
        changelog = Changelog(DASH.replace("## 1.9.0", f"{rc2}{rc1}## 1.9.0"), Style.DASH)
        entries = changelog.promoted(v("1.10.0"))
        assert [e.text for e in entries][2:] == [
            "### Topic three\n\n- Detail three",
            "### Topic four\n\n- Detail four\n\nNot an entry, but part of Topic four.",
        ]
        after = changelog.release(v("1.10.0"), dt.date(2026, 10, 5), entries, from_unreleased=True)
        assert "1.10.0rc" not in after and "release candidate" not in after and "More prose" not in after
        section = Changelog(after, Style.DASH).section(v("1.10.0"))
        assert section.endswith(
            "### Topic three\n\n- Detail three\n\n"
            "### Topic four\n\n- Detail four\n\nNot an entry, but part of Topic four.\n"
        )

    def test_needs_unreleased_link(self) -> None:
        changelog = Changelog(KAC.replace("[Unreleased]: https", "[Other]: https"), Style.KEEP_A_CHANGELOG)
        entry = changelog.unreleased()[0]
        with pytest.raises(ReleaseError, match="Unreleased"):
            changelog.release(v("1.1.0"), dt.date(2026, 10, 5), [entry], from_unreleased=True)


class TestEnvironments:
    BASE = '\n[environments.staging]\nlane = "rc"\nworkflow = "deploy.yml"\n'

    def load(self, text: str) -> Policy:
        import tomllib

        return Policy.parse(tomllib.loads(text), "policy")

    def test_loads_in_order(self) -> None:
        policy = self.load(
            POLICY + self.BASE + '\n[environments.production]\nfrom = "staging"\nworkflow = "deploy.yml"\n'
            'health = "https://example.com/health"\nbake_minutes = 60\n'
        )
        staging, production = policy.environments.values()
        assert (staging.name, staging.lane, staging.source, staging.bake_minutes) == ("staging", Lane.RC, None, 0)
        assert (production.lane, production.source, production.bake_minutes) == (None, "staging", 60)
        assert production.health == "https://example.com/health"
        assert dict(production.inputs) == {"environment": "production"}

    def test_none_by_default(self) -> None:
        assert self.load(POLICY).environments == {}

    @pytest.mark.parametrize(
        ("extra", "message"),
        [
            ('\n[environments.a]\nworkflow = "deploy.yml"\n', "exactly one of lane"),
            ('\n[environments.a]\nlane = "rc"\nfrom = "staging"\nworkflow = "deploy.yml"\n', "exactly one of lane"),
            ('\n[environments.a]\nfrom = "nowhere"\nworkflow = "deploy.yml"\n', "unknown environment 'nowhere'"),
            (
                '\n[environments.a]\nfrom = "b"\nworkflow = "deploy.yml"\n'
                '\n[environments.b]\nfrom = "a"\nworkflow = "deploy.yml"\n',
                "cycle: a -> b -> a",
            ),
            ('\n[environments.a]\nfrom = "a"\nworkflow = "deploy.yml"\n', "cycle: a -> a"),
            ('\n[environments.a]\nlane = "beta"\nworkflow = "deploy.yml"\n', "lane must be one of"),
            ('\n[environments.a]\nlane = "rc"\nworkflow = "deploy.yml"\nregion = "eu"\n', "unknown keys"),
            ('\n[environments.a]\nlane = "rc"\n', "workflow is required"),
            ('\n[environments.a]\nlane = "rc"\nworkflow = "../deploy.yml"\n', "workflow names a file"),
            ('\n[environments.a]\nlane = "rc"\nworkflow = "deploy.yml"\nhealth = "example.com"\n', "http"),
            ('\n[environments.a]\nlane = "rc"\nworkflow = "deploy.yml"\nbake_minutes = 5\n', "bake_minutes"),
            ('\n[environments.a]\nfrom = "staging"\nworkflow = "deploy.yml"\nbake_minutes = -1\n', "0..10080"),
            ('\n[environments.a]\nfrom = "staging"\nworkflow = "deploy.yml"\nbake_minutes = 1e3\n', "an integer"),
            ('\n[environments."a b"]\nlane = "rc"\nworkflow = "deploy.yml"\n', "environment name"),
            ("\n[environments]\nstaging2 = 1\n", "must be a table"),
        ],
    )
    def test_rejects(self, extra: str, message: str) -> None:
        with pytest.raises(ReleaseError, match=message):
            self.load(POLICY + self.BASE + extra)

    def test_the_lane_must_be_enabled(self) -> None:
        text = POLICY.replace("[lanes.dev]\nquiet_minutes = 30\n", "") + self.BASE.replace('"rc"', '"dev"')
        with pytest.raises(ReleaseError, match="lane dev is not enabled"):
            self.load(text)


class TestLabelCreation:
    """A label another run created meanwhile: gh label create fails with GitHub's 422 (#65)"""

    @staticmethod
    def gh(tmp_path: Path, create_error: str) -> GhCli:
        script = tmp_path / "gh"
        script.write_text(
            "#!/bin/sh\n"
            'case "$1 $2" in\n'
            "  'label list') echo '[]' ;;\n"
            f"  'label create') echo '{create_error}' >&2; exit 1 ;;\n"
            "  'issue create') echo https://github.com/o/demo/issues/7 ;;\n"
            "  *) exit 2 ;;\n"
            "esac\n",
            encoding="utf-8",
        )
        script.chmod(0o755)
        return GhCli(tmp_path, gh=str(script))

    def test_already_exists_counts_as_created(self, tmp_path: Path) -> None:
        exists = f'label with name "{PROPOSAL_LABEL}" already exists; use `--force` to update its color'
        assert self.gh(tmp_path, exists).create_issue("t", "b", (PROPOSAL_LABEL,)) == 7

    def test_another_failure_still_fails(self, tmp_path: Path) -> None:
        with pytest.raises(ReleaseError, match=f"gh label create {PROPOSAL_LABEL} failed: HTTP 403"):
            self.gh(tmp_path, "HTTP 403: Resource not accessible").create_issue("t", "b", (PROPOSAL_LABEL,))


class TestActiveRuns:
    """prepare asks for a release workflow's unfinished runs; a 403 means no `actions: read` (#175)"""

    @staticmethod
    def gh(tmp_path: Path, body: str) -> GhCli:
        script = tmp_path / "gh"
        script.write_text("#!/bin/sh\n" + body, encoding="utf-8")
        script.chmod(0o755)
        return GhCli(tmp_path, gh=str(script))

    def test_each_unfinished_status_is_asked_for(self, tmp_path: Path) -> None:
        body = (
            'n=; for a in "$@"; do case "$a" in status=queued) n=7 ;; status=in_progress) n=9 ;; esac; done\n'
            'if [ -n "$n" ]; then\n'
            '  echo "{\\"workflow_runs\\": [{\\"id\\": $n, \\"status\\": \\"s$n\\",'
            ' \\"created_at\\": \\"2026-10-06T08:00:00Z\\"}]}"\n'
            "else echo '{\"workflow_runs\": []}'; fi\n"
        )
        runs = self.gh(tmp_path, body).active_runs("release.yml")
        assert [(r.id, r.status) for r in runs] == [(9, "s9"), (7, "s7")]

    def test_a_refusal_is_forbidden(self, tmp_path: Path) -> None:
        refused = self.gh(tmp_path, "echo 'gh: Resource not accessible by integration (HTTP 403)' >&2; exit 1\n")
        with pytest.raises(Forbidden, match=r"gh api -X GET failed: .*\(HTTP 403\)"):
            refused.active_runs("release.yml")

    @pytest.mark.parametrize(
        "error", ["gh: API rate limit exceeded for installation (HTTP 403)", "gh: Not Found (HTTP 404)"]
    )
    def test_another_failure_is_not_forbidden(self, tmp_path: Path, error: str) -> None:
        with pytest.raises(ReleaseError) as caught:
            self.gh(tmp_path, f"echo '{error}' >&2; exit 1\n").active_runs("release.yml")
        assert not isinstance(caught.value, Forbidden)

    def test_a_run_approved_between_two_queries_is_still_seen(self, tmp_path: Path) -> None:
        # run 7 waits on an environment when in_progress is asked for, and is approved before
        # waiting is asked for: one query per status in turn would see it in neither (#175)
        seen = tmp_path / "seen"
        body = (
            'for a in "$@"; do case "$a" in status=*) s="${a#status=}" ;; esac; done\n'
            f'if [ "$s" = in_progress ] && [ -e "{seen}" ]; then\n'
            '  echo \'{"workflow_runs": [{"id": 7, "status": "in_progress", "created_at": "2026-10-06T08:00:00Z"}]}\'\n'
            "else echo '{\"workflow_runs\": []}'; fi\n"
            f'[ "$s" = in_progress ] && touch "{seen}"\n'
            "exit 0\n"
        )
        runs = self.gh(tmp_path, body).active_runs("release.yml")
        assert [r.id for r in runs] == [7]
