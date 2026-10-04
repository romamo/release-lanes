import datetime as dt

import pytest

from shipyard.changelog import Changelog, Entry
from shipyard.errors import ReleaseError
from shipyard.policy import Lane, Policy, Style
from shipyard.schedule import Freeze, Window
from shipyard.version import Part, Version

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
