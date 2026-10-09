"""Spec 015: the schedule `shipmill init` derives from the policy, `init --caller`, and
doctor's schedule check"""

import datetime as dt
import re
import subprocess
import sys
import tomllib
from pathlib import Path
from typing import Any

import pytest

from shipmill.cli import main
from shipmill.config import CONFIG_PATH
from shipmill.doctor import CALLER, doctor
from shipmill.errors import ReleaseError
from shipmill.init import init_caller
from shipmill.policy import Policy
from shipmill.schedule import Window
from shipmill.ticks import HOURLY, Tick, Ticks, crons, missing, needed

from .conftest import POLICY, Repo

# a Friday, sixteen days before Europe/Kyiv leaves summer time
TODAY = dt.date(2026, 10, 9)
HEAD = POLICY.split("[lanes.dev]", 1)[0]
GOLDEN = Path(__file__).parent / "golden" / "release.yml"


def _policy(repo: Repo, lanes: str, head: str = HEAD) -> None:
    repo.write(str(CONFIG_PATH), head + lanes)


def _written(repo: Repo) -> str:
    init_caller(repo.root, "ci.yml", force=True, today=TODAY)
    return (repo.root / CALLER).read_text(encoding="utf-8")


def _schedule(repo: Repo) -> list[str]:
    return [f"{c.status} {c.detail}" for c in doctor(repo.root, repo.github, today=TODAY) if c.name == "schedule"]


HOURLY_POLICIES = {
    "milestone on rc": '[lanes.rc]\nschedule = ["Mon-Fri 07:00 UTC"]\nmilestone = true\n',
    "milestone on dev": "[lanes.dev]\nquiet_minutes = 30\nmilestone = true\n",
    "milestone without a schedule": "[lanes.rc]\nmilestone = true\n",
    "a promoting stable lane on quiet_minutes": (
        '[lanes.rc]\nschedule = ["Mon-Fri 07:00 UTC"]\n\n[lanes.stable]\npromote_from = "rc"\nquiet_minutes = 60\n'
    ),
    "a promoting stable lane on quiet_minutes = 0": (
        '[lanes.rc]\nschedule = ["Mon 07:00 UTC"]\n\n[lanes.stable]\npromote_from = "rc"\nquiet_minutes = 0\n'
    ),
}


@pytest.mark.parametrize("lanes", HOURLY_POLICIES.values(), ids=HOURLY_POLICIES.keys())
def test_s015_5_a_trigger_that_needs_polling_gets_the_hourly_tick(repo: Repo, lanes: str) -> None:
    _policy(repo, lanes + "\n[lanes.hotfix]\n")
    text = _written(repo)
    assert crons(text) == ("7 * * * *",)
    # the hourly block is the one init always wrote
    assert (
        '  schedule:\n    - cron: "7 * * * *" # hourly: the policy decides whether a lane\'s window is open\n' in text
    )
    assert needed(repo.policy, TODAY) == Ticks(hourly=True)


def test_s015_5_the_starting_policy_keeps_the_release_workflow_init_wrote(repo: Repo) -> None:
    """init derives the cron from the policy it writes, whose stable lane sets milestone"""
    assert main(["--repo", str(repo.root), "init", "--force"]) == 0
    assert (repo.root / CALLER).read_bytes() == GOLDEN.read_bytes()
    assert main(["--repo", str(repo.root), "init", "--caller", "--force"]) == 0
    assert (repo.root / CALLER).read_bytes() == GOLDEN.read_bytes()


WINDOWS = [
    (['"Mon-Fri 07:00 UTC"'], ["0 7 * * 1,2,3,4,5"]),
    (['"Mon-Fri 07:00 Europe/Kyiv"'], ["0 4 * * 1,2,3,4,5", "0 5 * * 1,2,3,4,5"]),
    (['"Mon 01:00 Asia/Tokyo"'], ["0 16 * * 0"]),
    # America/New_York's evening falls on the next UTC day
    (['"Fri 21:30 America/New_York"'], ["30 1 * * 6", "30 2 * * 6"]),
    (['"Mon 07:00 UTC"', '"Fri 07:00 UTC"'], ["0 7 * * 1,5"]),
    (['"Mon-Fri 07:00 UTC"', '"Mon 07:00 UTC"'], ["0 7 * * 1,2,3,4,5"]),
    (['"daily 09:30 UTC"', '"Wed 04:00 UTC"'], ["0 4 * * 3", "30 9 * * 0,1,2,3,4,5,6"]),
    (['"Mon 07:15 UTC"', '"Mon 07:00 UTC"'], ["0 7 * * 1", "15 7 * * 1"]),
]


@pytest.mark.parametrize(("windows", "lines"), WINDOWS)
def test_s015_6_a_schedule_only_policy_gets_a_cron_per_window_time(
    repo: Repo, windows: list[str], lines: list[str]
) -> None:
    _policy(repo, f"[lanes.rc]\nschedule = [{', '.join(windows)}]\n\n[lanes.hotfix]\n")
    text = _written(repo)
    assert list(crons(text)) == lines
    assert "shipmill derived these from the policy's windows" in text
    assert "init --caller --force" in text


def test_s015_6_two_lanes_whose_windows_share_a_utc_time_give_one_line(repo: Repo) -> None:
    """quiet_minutes on a lane that doesn't promote, and a stable lane that promotes on its
    schedule only, still narrow the schedule to the windows'"""
    _policy(
        repo,
        "[lanes.dev]\nquiet_minutes = 30\n\n"
        '[lanes.rc]\nschedule = ["Mon-Fri 07:00 Europe/Kyiv"]\n\n'
        '[lanes.stable]\npromote_from = "rc"\nmin_soak_days = 3\nschedule = ["Mon 04:00 UTC"]\n\n'
        "[lanes.hotfix]\n",
    )
    assert list(crons(_written(repo))) == ["0 4 * * 1,2,3,4,5", "0 5 * * 1,2,3,4,5"]


def test_s015_6_the_year_from_the_given_date_decides_the_offsets() -> None:
    """Asia/Tokyo has no daylight saving time; Europe/Kyiv yields both of its offsets from
    any day of the year"""
    for day in (TODAY, dt.date(2027, 1, 15), dt.date(2027, 6, 1)):
        policy = Policy.parse(_raw('[lanes.rc]\nschedule = ["Mon-Fri 07:00 Europe/Kyiv"]\n'), "x")
        assert needed(policy, day).lines() == ("0 4 * * 1,2,3,4,5", "0 5 * * 1,2,3,4,5")
    assert Window.parse("Mon 01:00 Asia/Tokyo").start_on(dt.date(2026, 10, 12)) == dt.datetime(
        2026, 10, 11, 16, tzinfo=dt.UTC
    )
    assert Window.parse("Mon 01:00 Asia/Tokyo").start_on(dt.date(2026, 10, 13)) is None


def _raw(lanes: str) -> dict[str, Any]:
    return tomllib.loads(HEAD + lanes)


NO_TICK = {
    "quiet_minutes only": "[lanes.dev]\nquiet_minutes = 30\n",
    "by hand only": "[lanes.rc]\n",
    "a stable lane on quiet_minutes that doesn't promote": "[lanes.stable]\nquiet_minutes = 30\n",
    "a promoting stable lane without a trigger": '[lanes.rc]\n\n[lanes.stable]\npromote_from = "rc"\n',
}


@pytest.mark.parametrize("lanes", NO_TICK.values(), ids=NO_TICK.keys())
def test_s015_7_a_push_driven_policy_gets_no_schedule_key(repo: Repo, lanes: str) -> None:
    _policy(repo, lanes + "\n[lanes.hotfix]\n", head=HEAD.replace('branch = "main"', 'branch = "trunk"'))
    text = _written(repo)
    assert "schedule" not in text and "cron" not in text
    assert "on:\n  push:\n    branches: [trunk]\n  workflow_dispatch:\n    inputs:\n      lane:\n" in text
    for name in ("lane", "dry-run", "hotfix-prs", "hotfix-from"):
        assert f"      {name}:\n        description:" in text.split("workflow_dispatch:", 1)[1].split("permissions:")[0]
    # apart from the schedule block and the branch, the file is the one init always wrote
    golden = GOLDEN.read_text(encoding="utf-8").replace("branches: [main]", "branches: [trunk]")
    assert text == re.sub(r"  schedule:\n    - cron: .*\n", "", golden)


def _ours(path: Path) -> bool:
    """A file of the checkout, outside .git"""
    return path.is_file() and ".git" not in path.parts


def test_s015_8_init_caller_writes_only_the_release_workflow_from_the_policy(repo: Repo) -> None:
    _policy(repo, '[lanes.rc]\nschedule = ["Mon-Fri 07:00 UTC"]\n\n[lanes.hotfix]\n')
    before = {p.relative_to(repo.root): p.read_bytes() for p in repo.root.rglob("*") if _ours(p)}
    argv = ["--repo", str(repo.root), "init", "--caller", "--ci", "checks.yml", "--runs-on", "self-hosted"]
    assert main(argv) == 0
    after = {p.relative_to(repo.root): p.read_bytes() for p in repo.root.rglob("*") if _ours(p)}
    assert after.keys() - before.keys() == {CALLER}
    assert {path: after[path] for path in before} == before
    text = repo.read(str(CALLER))
    assert "uses: ./.github/workflows/checks.yml" in text
    assert text.count("      runs-on: self-hosted\n") == 2
    assert crons(text) == ("0 7 * * 1,2,3,4,5",)


def test_s015_8_init_caller_refuses_an_existing_release_workflow_without_force(repo: Repo) -> None:
    repo.write(str(CALLER), "hand-written\n")
    with pytest.raises(ReleaseError, match=re.escape(f"{CALLER} exists; pass --force to overwrite")):
        main(["--repo", str(repo.root), "init", "--caller"])
    assert repo.read(str(CALLER)) == "hand-written\n"
    assert main(["--repo", str(repo.root), "init", "--caller", "--force"]) == 0
    assert repo.read(str(CALLER)).startswith("name: Release\n")


def test_s015_8_init_caller_exits_2_without_a_policy(repo: Repo) -> None:
    (repo.root / CONFIG_PATH).unlink()
    argv = [sys.executable, "-m", "shipmill", "--repo", str(repo.root), "init", "--caller"]
    out = subprocess.run(argv, capture_output=True, text=True, check=False)
    assert out.returncode == 2
    assert "no release policy at" in out.stderr
    assert not (repo.root / CALLER).exists() and not (repo.root / CONFIG_PATH).exists()


def test_s015_8_init_caller_refuses_a_policy_that_does_not_load_with_its_error(repo: Repo) -> None:
    _policy(repo, '[lanes.rc]\nschedule = ["Mon-Fri 7am UTC"]\n')
    with pytest.raises(ReleaseError, match="a window is '<days> HH:MM <zone>'"):
        main(["--repo", str(repo.root), "init", "--caller"])
    assert not (repo.root / CALLER).exists()


@pytest.mark.parametrize("flag", ["--operate", "--no-fragments"])
def test_s015_8_init_caller_exits_2_with_operate_or_no_fragments(repo: Repo, flag: str) -> None:
    argv = [sys.executable, "-m", "shipmill", "--repo", str(repo.root), "init", "--caller", flag]
    out = subprocess.run(argv, capture_output=True, text=True, check=False)
    assert out.returncode == 2
    assert f"drop {flag}" in out.stderr
    assert sorted(p.name for p in (repo.root / ".github").iterdir()) == ["shipmill.toml"]


def _release(*lines: str) -> str:
    schedule = "".join(f'    - cron: "{line}"\n' for line in lines)
    golden = GOLDEN.read_text(encoding="utf-8")
    return re.sub(r"  schedule:\n    - cron: .*\n", f"  schedule:\n{schedule}" if lines else "", golden)


def test_s015_9_doctor_warns_naming_each_missing_cron_and_the_fix(repo: Repo) -> None:
    _policy(repo, '[lanes.rc]\nschedule = ["Mon-Fri 07:00 Europe/Kyiv", "Sat 12:00 UTC"]\n\n[lanes.hotfix]\n')
    repo.write(str(CALLER), _release("0 5 * * 1,2,3,4,5"))
    [check] = _schedule(repo)
    assert check.startswith("WARN ")
    assert '"0 4 * * 1,2,3,4,5", "0 12 * * 6"' in check
    assert '"0 5 * * 1,2,3,4,5"' not in check
    assert "init --caller --force" in check


def test_s015_9_doctor_warns_when_the_hourly_tick_is_needed_and_missing(repo: Repo) -> None:
    _policy(repo, '[lanes.rc]\nschedule = ["Mon 07:00 UTC"]\nmilestone = true\n')
    repo.write(str(CALLER), _release("0 7 * * 1"))
    [check] = _schedule(repo)
    assert check.startswith("WARN ") and f'"{HOURLY}"' in check and "init --caller --force" in check
    repo.write(str(CALLER), _release())
    [check] = _schedule(repo)
    assert check.startswith("WARN ") and f'"{HOURLY}"' in check


@pytest.mark.parametrize(
    ("lines", "milestone"),
    [
        (["13 * * * *"], False),  # any minute counts as the hourly tick
        (["13 * * * *"], True),
        (["7 * * * *", "0 3 * * 2"], False),
        (["7 * * * *", "0 3 * * 2"], True),
        (["0 4 * * 1,2,3,4,5", "0 5 * * 1,2,3,4,5", "0 12 * * 6", "30 22 * * 0"], False),  # extras included
        (["0 4 * * 1-5", "0 5 * * 1,2,3", "0 5 * * 4,5", "0 12 * * *"], False),  # the same days in other forms
    ],
)
def test_s015_9_doctor_gives_no_warn_for_an_hourly_tick_or_every_needed_line(
    repo: Repo, lines: list[str], milestone: bool
) -> None:
    flag = "milestone = true\n" if milestone else ""
    _policy(repo, f'[lanes.rc]\nschedule = ["Mon-Fri 07:00 Europe/Kyiv", "Sat 12:00 UTC"]\n{flag}\n[lanes.hotfix]\n')
    repo.write(str(CALLER), _release(*lines))
    [check] = _schedule(repo)
    assert check.startswith("PASS "), check


def test_s015_9_doctor_gives_no_warn_for_a_tick_a_push_driven_policy_does_not_need(repo: Repo) -> None:
    """D-9: an extra tick costs minutes, not releases"""
    _policy(repo, "[lanes.dev]\nquiet_minutes = 30\n")
    for lines in ((), ("7 * * * *",), ("0 4 * * 1",)):
        repo.write(str(CALLER), _release(*lines))
        assert _schedule(repo) == ["PASS the policy needs no scheduled tick: a push or a person starts each run"]


def test_s015_9_init_caller_writes_what_doctor_passes(repo: Repo) -> None:
    for lanes in (*HOURLY_POLICIES.values(), '[lanes.rc]\nschedule = ["Mon-Fri 07:00 Europe/Kyiv"]\n'):
        _policy(repo, lanes)
        _written(repo)
        [check] = _schedule(repo)
        assert check.startswith("PASS "), check


def test_doctor_leaves_a_missing_release_workflow_to_the_workflow_check(repo: Repo) -> None:
    checks = doctor(repo.root, repo.github, today=TODAY)
    assert [c.status for c in checks if c.name == "workflow"] == ["FAIL"]
    assert not [c for c in checks if c.name == "schedule"]


def test_crons_reads_quoted_and_bare_lines_with_comments() -> None:
    text = "on:\n  schedule:\n    - cron: \"7 * * * *\" # hourly\n    - cron: '0  4 * * 1'\n    - cron: 30 2 * * 6\n"
    assert crons(text) == ("7 * * * *", "0 4 * * 1", "30 2 * * 6")


def test_a_cron_of_another_form_covers_no_window() -> None:
    needs = Ticks(hourly=False, windows=(Tick(0, 7, (1,)),))
    assert missing(needs, ["0 7 1 * 1", "0 7 * * MON", "*/5 7 * * 1", "0 7 * * 2"]) == ("0 7 * * 1",)
    assert missing(needs, ["0 7 * * 7-7", "0 8 * * 1"]) == ("0 7 * * 1",)
    assert missing(needs, ["0 7 * * 0-1"]) == ()
    assert missing(needs, ["*/10 * * * *"]) == ()
