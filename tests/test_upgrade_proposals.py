"""Spec 014: `shipmill upgrade --propose`, land.yml's upgrade job, doctor's
permission warning, and the pending upgrades in `shipmill status`"""

import dataclasses
import json
import re
import subprocess
from collections.abc import Sequence
from pathlib import Path

import pytest

from shipmill.cli import main
from shipmill.config import CONFIG_PATH
from shipmill.doctor import CALLER, Check, doctor
from shipmill.errors import ReleaseError
from shipmill.github import UPGRADE_LABEL, UPGRADE_LATER_LABEL, Forbidden, Issue, parse_labelled
from shipmill.init import Detected, caller_text
from shipmill.policy import Style, VersionFiles
from shipmill.status import Issue as StatusIssue
from shipmill.status import read_upgrades
from shipmill.upgrades import Offer, UpgradeId, asked, body, find, marker, renewed
from shipmill.version import Version

from .conftest import POLICY, FakeGitHub, Repo
from .test_status_picture import REPO, facts, lines

ROOT = Path(__file__).parent.parent
WORKFLOWS = ROOT / ".github" / "workflows"
AGENTS = '\n[agents]\nprompt = "triage {repo}"\n'
FRAGMENTS = find(UpgradeId("changelog-fragments"))
PLUGIN = find(UpgradeId("plugin-update"))
DECIDED = POLICY.replace("[changelog]\n", '[changelog]\nfragments = "changelog.d"\n')


def write_config(root: Path, text: str) -> None:
    path = root / CONFIG_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def propose(root: Path, github: FakeGitHub, capsys: pytest.CaptureFixture[str]) -> list[str]:
    assert main(["--repo", str(root), "upgrade", "--propose"], github=github) == 0
    return capsys.readouterr().out.splitlines()


def by_id(github: FakeGitHub) -> dict[str, Issue]:
    """The open shipmill-upgrade issues by the id their first line names"""
    found = {}
    for issue in github.open_labelled_issues(UPGRADE_LABEL):
        first = issue.body.split("\n", 1)[0]
        found[first.split()[2]] = issue
    return found


# -- proposing (S-014-5) ---------------------------------------------------------------------


def test_s014_5_propose_opens_one_issue_per_pending_upgrade_with_the_marker_first(
    repo: Repo, capsys: pytest.CaptureFixture[str]
) -> None:
    write_config(repo.root, POLICY + AGENTS)
    out = propose(repo.root, repo.github, capsys)
    issues = by_id(repo.github)
    assert sorted(issues) == ["changelog-fragments", "plugin-update"]
    for upgrade in (FRAGMENTS, PLUGIN):
        issue = issues[str(upgrade.id)]
        assert issue.body.split("\n", 1)[0] == marker(upgrade) == f"<!-- shipmill-upgrade: {upgrade.id} 0.37.0 -->"
        assert repo.github.labels[issue.number] == (UPGRADE_LABEL,)
        assert f"opened #{issue.number}: {upgrade.id}" in out
    text = issues["changelog-fragments"].body
    assert '```toml\n[changelog]\nfragments = "changelog.d"\n```' in text
    assert "creates `changelog.d/README.md`" in text
    assert "upgrade --apply changelog-fragments" in text and "close this issue" in text


def test_s014_5_the_next_run_updates_the_same_issue_in_place(repo: Repo, capsys: pytest.CaptureFixture[str]) -> None:
    propose(repo.root, repo.github, capsys)
    (number,) = repo.github.issues
    assert propose(repo.root, repo.github, capsys) == [f"unchanged #{number}: changelog-fragments"]
    assert repo.github.edits == 0
    stale = repo.github.issues[number]
    older = stale.body.replace(" 0.37.0 -->", " 0.36.0 -->").replace("Why:", "Why (old):")
    repo.github.update_issue(number, "an older title", older)
    edits = repo.github.edits
    assert propose(repo.root, repo.github, capsys) == [f"updated #{number}: changelog-fragments"]
    assert list(repo.github.issues) == [number] and repo.github.edits == edits + 1
    assert repo.github.issues[number].body == body(FRAGMENTS)
    assert repo.github.issues[number].title == "shipmill upgrade: turn on changelog-fragments"


def test_s014_5_observe_opens_none_and_act_opens_them(repo: Repo, capsys: pytest.CaptureFixture[str]) -> None:
    write_config(repo.root, POLICY + '\n[autonomy]\nupgrade = "observe"\n')
    out = propose(repo.root, repo.github, capsys)
    assert repo.github.issues == {}
    assert out == ["upgrade autonomy is observe: no upgrade issue opened or updated"]
    write_config(repo.root, POLICY + '\n[autonomy]\nupgrade = "act"\n')
    propose(repo.root, repo.github, capsys)
    assert sorted(by_id(repo.github)) == ["changelog-fragments"]


def test_s014_5_propose_prints_json(repo: Repo, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--repo", str(repo.root), "upgrade", "--propose", "--json"], github=repo.github) == 0
    (number,) = repo.github.issues
    assert json.loads(capsys.readouterr().out) == [{"id": "changelog-fragments", "issue": number, "outcome": "opened"}]


def test_propose_and_apply_go_alone(repo: Repo) -> None:
    with pytest.raises(SystemExit):
        main(["--repo", str(repo.root), "upgrade", "--propose", "--apply", "plugin-update"], github=repo.github)


# -- a declined upgrade (S-014-6) ------------------------------------------------------------


def test_s014_6_an_issue_closed_without_the_key_is_never_proposed_again_nor_listed(
    repo: Repo, capsys: pytest.CaptureFixture[str]
) -> None:
    write_config(repo.root, POLICY + AGENTS)
    propose(repo.root, repo.github, capsys)
    declined = by_id(repo.github)["changelog-fragments"].number
    repo.github.close_issue(declined, "not for us")  # a person closes it, as not planned or completed
    for _ in range(2):
        out = propose(repo.root, repo.github, capsys)
        assert sorted(by_id(repo.github)) == ["plugin-update"]
        assert not any("changelog-fragments" in line for line in out)
    assert main(["--repo", str(repo.root), "upgrade", "--json"], github=repo.github) == 0
    listed = json.loads(capsys.readouterr().out)
    assert [(u["id"], u["issue"]) for u in listed] == [("plugin-update", by_id(repo.github)["plugin-update"].number)]
    assert main(["--repo", str(repo.root), "upgrade"], github=repo.github) == 0
    assert "changelog-fragments" not in capsys.readouterr().out


def test_s014_6_a_reopened_issue_is_proposed_again(repo: Repo, capsys: pytest.CaptureFixture[str]) -> None:
    propose(repo.root, repo.github, capsys)
    (number,) = repo.github.issues
    repo.github.close_issue(number, "")
    reopened = repo.github.closed_issues.pop(number)
    repo.github.issues[number] = Issue(number, reopened.title, reopened.body)
    assert propose(repo.root, repo.github, capsys) == [f"unchanged #{number}: changelog-fragments"]


def test_s014_6_an_issue_without_the_marker_on_its_first_line_is_not_a_proposal(
    repo: Repo, capsys: pytest.CaptureFixture[str]
) -> None:
    number = repo.github.create_issue("about fragments", f"see below\n{marker(FRAGMENTS)}\n", (UPGRADE_LABEL,))
    repo.github.close_issue(number, "")
    out = propose(repo.root, repo.github, capsys)
    assert len(out) == 1 and out[0].startswith("opened #")


# -- a decided upgrade (S-014-7) -------------------------------------------------------------


@pytest.mark.parametrize("level", ["propose", "observe"])
@pytest.mark.parametrize("value", ['"changelog.d"', "false"])  # any value is a decision
def test_s014_7_a_config_that_holds_the_key_closes_its_open_issue_as_completed(
    repo: Repo, capsys: pytest.CaptureFixture[str], level: str, value: str
) -> None:
    propose(repo.root, repo.github, capsys)
    (number,) = repo.github.issues
    decided = POLICY.replace("[changelog]\n", f"[changelog]\nfragments = {value}\n")
    write_config(repo.root, decided + f'\n[autonomy]\nupgrade = "{level}"\n')
    out = propose(repo.root, repo.github, capsys)
    assert out[0] == f"closed #{number}: changelog-fragments"
    assert repo.github.issues == {} and number in repo.github.closed_issues
    assert "sets `fragments`" in repo.github.closed[number]  # closed with gh's default reason: completed
    assert not any(line.startswith("closed") for line in propose(repo.root, repo.github, capsys))


# -- "not now" (S-014-14) ---------------------------------------------------------------------


def later(repo: Repo, capsys: pytest.CaptureFixture[str], recorded: str) -> int:
    """The fragments issue, answered "not now" while its marker recorded the version given"""
    propose(repo.root, repo.github, capsys)
    (number,) = repo.github.issues
    issue = repo.github.issues[number]
    stale = issue.body.replace(" 0.37.0 -->", f" {recorded} -->", 1)
    repo.github.issues[number] = Issue(number, issue.title, stale)
    repo.github.labels[number] = (UPGRADE_LABEL, UPGRADE_LATER_LABEL)
    return number


@pytest.mark.parametrize("recorded", ["0.37.0", "0.100.0", "0.37.1", "1.0.0rc1"])
def test_s014_14_propose_keeps_the_later_label_while_the_upgrade_is_not_newer(
    repo: Repo, capsys: pytest.CaptureFixture[str], recorded: str
) -> None:
    # 0.100.0 sorts before 0.37.0 as text and after it as a version: the order is the version's
    number = later(repo, capsys, recorded)
    outcome = "unchanged" if recorded == "0.37.0" else "updated"
    assert propose(repo.root, repo.github, capsys) == [f"{outcome} #{number}: changelog-fragments"]
    assert repo.github.labels[number] == (UPGRADE_LABEL, UPGRADE_LATER_LABEL)
    # the update recorded the installed version; the label holds on the next run too
    assert propose(repo.root, repo.github, capsys) == [f"unchanged #{number}: changelog-fragments"]
    assert repo.github.labels[number] == (UPGRADE_LABEL, UPGRADE_LATER_LABEL)


@pytest.mark.parametrize("recorded", ["0.36.0", "0.9.0", "0.37.0rc2", "0.37.0.dev1"])
def test_s014_14_propose_takes_the_later_label_off_once_the_upgrade_is_newer(
    repo: Repo, capsys: pytest.CaptureFixture[str], recorded: str
) -> None:
    # 0.9.0 sorts after 0.37.0 as text and before it as a version
    number = later(repo, capsys, recorded)
    assert propose(repo.root, repo.github, capsys) == [f"renewed #{number}: changelog-fragments"]
    assert repo.github.labels[number] == (UPGRADE_LABEL,)
    assert repo.github.issues[number].body == body(FRAGMENTS)  # the marker now records 0.37.0
    assert propose(repo.root, repo.github, capsys) == [f"unchanged #{number}: changelog-fragments"]


def test_s014_14_an_unreadable_recorded_version_keeps_the_label(repo: Repo, capsys: pytest.CaptureFixture[str]) -> None:
    number = later(repo, capsys, "someday")
    assert propose(repo.root, repo.github, capsys) == [f"updated #{number}: changelog-fragments"]
    assert repo.github.labels[number] == (UPGRADE_LABEL, UPGRADE_LATER_LABEL)


def test_s014_14_only_an_issue_with_the_label_renews(repo: Repo, capsys: pytest.CaptureFixture[str]) -> None:
    number = later(repo, capsys, "0.36.0")
    repo.github.labels[number] = (UPGRADE_LABEL,)  # never answered "not now": no label to take off
    assert propose(repo.root, repo.github, capsys) == [f"updated #{number}: changelog-fragments"]
    newer = dataclasses.replace(FRAGMENTS, version=Version.parse("0.38.0"))
    waiting = Issue(number, "t", f"{marker(FRAGMENTS)}\n", labels=(UPGRADE_LABEL, UPGRADE_LATER_LABEL))
    assert asked(waiting) == Version.parse("0.37.0")
    assert renewed(newer, waiting)
    assert not renewed(FRAGMENTS, waiting)
    assert not renewed(newer, dataclasses.replace(waiting, labels=(UPGRADE_LABEL,)))


def test_s014_14_the_labels_are_read_with_the_issues() -> None:
    labels = [{"name": UPGRADE_LABEL}, {"name": UPGRADE_LATER_LABEL}]
    found = [{"number": 7, "title": "t", "body": "b", "state": "OPEN", "closedAt": None, "labels": labels}]
    (issue,) = parse_labelled(json.dumps(found))
    assert issue.labels == (UPGRADE_LABEL, UPGRADE_LATER_LABEL)


# -- the release workflow (S-014-8) ----------------------------------------------------------


def _job(text: str, name: str) -> str:
    start = text.index(f"\n  {name}:\n")
    following = re.search(r"\n  [\w-]+:\n", text[start + 1 :])
    return text[start : start + 1 + following.start()] if following else text[start:]


def test_s014_8_land_yml_proposes_in_its_own_job_after_a_release_landed() -> None:
    text = (WORKFLOWS / "land.yml").read_text(encoding="utf-8")
    job = _job(text, "upgrade")
    assert job.startswith("\n  upgrade:\n    needs: land\n")  # only after a release landed, never a dry run
    assert "    if:" not in job  # the default: land succeeded
    # no permissions: the caller's land grant, `issues: write` included; a nested job asking
    # for more than the caller grants fails the whole run, release included
    assert "permissions:" not in job
    assert 'shipmill upgrade --propose | tee -a "$GITHUB_STEP_SUMMARY"' in job
    assert "shell: bash" in job  # -eo pipefail: a failed propose fails the job despite the tee
    for other in ("land", "close-proposal", "cleanup"):  # no job waits on it: it never stops a release
        assert not re.search(r"^    (?:needs|if):.*upgrade", _job(text, other), re.MULTILINE)
    assert "upgrade" not in (WORKFLOWS / "prepare.yml").read_text(encoding="utf-8").split("\njobs:", 1)[1]


@pytest.mark.parametrize("caller", ["release.yml", "init"])
def test_s014_8_the_caller_needs_no_job_of_its_own_only_its_land_grant(caller: str) -> None:
    if caller == "init":
        text = caller_text(Detected("demo", "main", Style.KEEP_A_CHANGELOG, VersionFiles.NONE), "ci.yml")
    else:
        text = (WORKFLOWS / caller).read_text(encoding="utf-8")
    assert "\n  upgrade:\n" not in text and "upgrade.yml" not in text
    assert re.search(r"^      issues: write\b", _job(text, "land"), re.MULTILINE)
    assert not (WORKFLOWS / "upgrade.yml").exists()


class Refusing(FakeGitHub):
    """GitHub refusing every write, as for a job without `issues: write`"""

    def create_issue(self, title: str, body: str, labels: Sequence[str] = ()) -> int:
        raise Forbidden("gh issue create --title failed: Resource not accessible by integration (HTTP 403)")

    def close_issue(self, number: int, comment: str) -> None:
        raise Forbidden("gh issue close failed: Resource not accessible by integration (HTTP 403)")


def test_s014_8_a_failure_names_the_upgrade_and_the_fix(repo: Repo) -> None:
    with pytest.raises(ReleaseError, match=r"^upgrade changelog-fragments: gh issue create .*HTTP 403.*") as found:
        main(["--repo", str(repo.root), "upgrade", "--propose"], github=Refusing())
    assert f"grant `issues: write` to the job in {CALLER} that calls shipmill's land.yml" in str(found.value)
    refusing = Refusing()
    number = FakeGitHub.create_issue(refusing, "t", f"{marker(FRAGMENTS)}\n", (UPGRADE_LABEL,))
    write_config(repo.root, DECIDED)
    with pytest.raises(ReleaseError, match=r"^upgrade changelog-fragments: gh issue close"):
        main(["--repo", str(repo.root), "upgrade", "--propose"], github=refusing)
    assert number in refusing.issues


class Unreadable(FakeGitHub):
    def labelled_issues(self, label: str) -> list[Issue]:
        raise ReleaseError("gh issue list --label failed: HTTP 401: Bad credentials")


def test_s014_8_a_failed_read_names_the_pending_upgrades(repo: Repo) -> None:
    with pytest.raises(ReleaseError, match=r"^can't read the shipmill-upgrade issues \(changelog-fragments\): "):
        main(["--repo", str(repo.root), "upgrade", "--propose"], github=Unreadable())


# -- doctor (S-014-9) ------------------------------------------------------------------------

LAND_JOB = """\
jobs:
  prepare:
    uses: shipmill/shipmill/.github/workflows/prepare.yml@v0
    permissions:
      contents: write
      issues: write
  land:
    uses: shipmill/shipmill/.github/workflows/land.yml@v0
    permissions:
      contents: write
      actions: write
      issues: {issues}
"""


def upgrade_checks(repo: Repo) -> list[Check]:
    return [c for c in doctor(repo.root, repo.github, repo=REPO) if c.name in ("upgrade", "upgrades", "permissions")]


def test_s014_9_doctor_warns_about_issues_write_only_when_upgrades_propose_and_one_is_pending(repo: Repo) -> None:
    repo.write(str(CALLER), LAND_JOB.split("  land:\n")[0])  # no land job: the workflow check's failure
    assert [c.status for c in upgrade_checks(repo)] == ["PASS"]
    repo.write(str(CALLER), LAND_JOB.format(issues="read"))
    (warn,) = [c for c in upgrade_checks(repo) if c.status == "WARN"]
    assert warn.name == "permissions"
    assert warn.detail == (
        "upgrade autonomy is propose and 1 upgrade(s) pending, but the land job in .github/workflows/release.yml,"
        " whose grant proposes them, grants no `issues: write`: add it"
    )
    write_config(repo.root, POLICY + '\n[autonomy]\nupgrade = "act"\n')
    assert [c.status for c in upgrade_checks(repo)] == ["PASS", "WARN"]
    write_config(repo.root, POLICY + '\n[autonomy]\nupgrade = "observe"\n')  # observe: no issue to open
    assert [c.status for c in upgrade_checks(repo)] == ["PASS"]
    (repo.root / "changelog.d").mkdir()
    write_config(repo.root, DECIDED)  # nothing pending
    assert upgrade_checks(repo) == []
    write_config(repo.root, POLICY)
    repo.github.close_issue(repo.github.create_issue("t", f"{marker(FRAGMENTS)}\n", (UPGRADE_LABEL,)), "")
    assert upgrade_checks(repo) == []  # declined: nothing pending either
    repo.github.closed_issues.clear()
    repo.write(str(CALLER), LAND_JOB.format(issues="write"))
    assert [c.status for c in upgrade_checks(repo)] == ["PASS"]


def test_s014_9_pending_upgrades_alone_never_make_doctor_exit_1(repo: Repo, capsys: pytest.CaptureFixture[str]) -> None:
    repo.write(str(CALLER), LAND_JOB.format(issues="read"))
    (repo.root / "changelog.d").mkdir()
    write_config(repo.root, DECIDED + AGENTS + "plugin_update = false\n")
    decided = {c.name for c in doctor(repo.root, repo.github) if c.status == "FAIL"}
    code = main(["--repo", str(repo.root), "doctor"], github=repo.github)
    assert "PASS upgrade:" not in capsys.readouterr().out
    write_config(repo.root, POLICY + AGENTS)
    assert main(["--repo", str(repo.root), "doctor"], github=repo.github) == code
    assert "PASS upgrade: changelog-fragments pending" in capsys.readouterr().out
    checks = doctor(repo.root, repo.github, repo=REPO)
    assert {c.name for c in checks if c.status == "FAIL"} == decided
    shown = [c for c in checks if c.name == "upgrade"]
    assert [c.status for c in shown] == ["PASS", "PASS"]
    assert shown[0].detail.startswith(f"changelog-fragments pending (shipmill 0.37.0): {FRAGMENTS.changes}; fix: ")
    assert shown[0].detail.endswith("shipmill upgrade --apply changelog-fragments")  # bare or the uvx form
    number = repo.github.create_issue("t", f"{marker(PLUGIN)}\n", (UPGRADE_LABEL,))
    shown = [c for c in doctor(repo.root, repo.github, repo=REPO) if c.name == "upgrade"]
    assert shown[1].detail.endswith(f"fix: https://github.com/{REPO}/issues/{number}")


def test_doctor_says_when_it_cant_read_the_upgrade_issues(repo: Repo) -> None:
    (check,) = [c for c in doctor(repo.root, None) if c.name == "upgrades"]
    assert check == Check("WARN", "upgrades", f"can't read the {UPGRADE_LABEL} issues: gh isn't installed")


# -- status (S-014-15) -----------------------------------------------------------------------


def test_s014_15_status_lists_each_pending_upgrade_with_its_issue_or_the_apply_command() -> None:
    found = lines(facts(upgrades=[Offer(FRAGMENTS, None), Offer(PLUGIN, 42)]))
    at = found.index(f"  upgrade        changelog-fragments (shipmill 0.37.0): {FRAGMENTS.changes}")
    assert found[at + 1] == "                 fix: shipmill upgrade --apply changelog-fragments"
    assert found[at + 2] == f"                 plugin-update (shipmill 0.37.0): {PLUGIN.changes}"
    assert found[at + 3] == f"                 fix: https://github.com/{REPO}/issues/42"
    assert found[0].endswith(": IDLE")  # an offer, not a reason for the verdict


@pytest.mark.parametrize("state", ["NEW", "DECIDED"])
def test_s014_15_status_lists_an_upgrade_issue_on_its_upgrade_line_only(state: str) -> None:
    # triage_state.py reads an upgrade issue as any issue (NEW, or DECIDED once its question is
    # answered), but github-ship-watch owns it: it is no issue to triage
    found = lines(facts(upgrades=[Offer(PLUGIN, 42)], issues=[StatusIssue(42, state), StatusIssue(7, "NEW")]))
    triage = [line for line in found if "/issues/7" in line or "/issues/42" in line]
    assert [line for line in triage if "/issues/42" in line] == [
        f"                 fix: https://github.com/{REPO}/issues/42"
    ]
    assert any(line.startswith("  to triage") and "/issues/7" in line for line in found)


class Gh:
    def __init__(self, answer: list[dict[str, object]]) -> None:
        self.answer = answer
        self.seen: list[list[str]] = []

    def __call__(self, cmd: list[str]) -> subprocess.CompletedProcess[str]:
        self.seen.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, json.dumps(self.answer), "")


def test_s014_15_status_reads_the_issues_and_leaves_out_a_declined_upgrade(tmp_path: Path) -> None:
    write_config(tmp_path, POLICY + AGENTS)
    closed = {"number": 3, "title": "t", "body": f"{marker(FRAGMENTS)}\n", "state": "CLOSED", "labels": []}
    closed["closedAt"] = "2026-10-01T00:00:00Z"
    opened = {"number": 5, "title": "t", "body": f"{marker(PLUGIN)}\n", "state": "OPEN", "closedAt": None}
    opened["labels"] = [{"name": UPGRADE_LABEL}]
    gh = Gh([closed, opened])
    assert read_upgrades(REPO, tmp_path, gh) == (Offer(PLUGIN, 5),)
    (cmd,) = gh.seen
    assert cmd[:3] == ["gh", "issue", "list"] and cmd[-2:] == ["-R", REPO] and UPGRADE_LABEL in cmd
    write_config(tmp_path, DECIDED)  # nothing pending: GitHub isn't asked
    assert read_upgrades(REPO, tmp_path, gh) == () and len(gh.seen) == 1
    assert read_upgrades(REPO, tmp_path / "none", gh) == ()  # no config
