"""The github-ship-watch retro's comparison, PR classification, and proposal dedupe, from fixtures"""

import datetime as dt
import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "skills" / "github-ship-watch" / "scripts" / "retro.py"
END = dt.datetime(2026, 10, 4, tzinfo=dt.timezone.utc)  # noqa: UP017 (runs under 3.10 too, as the script does)
START = END - dt.timedelta(days=7)


@pytest.fixture(scope="module")
def rt() -> ModuleType:
    spec = importlib.util.spec_from_file_location("retro", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses look their module up by name
    spec.loader.exec_module(module)
    return module


def day(n: float) -> str:
    return (END - dt.timedelta(days=n)).isoformat().replace("+00:00", "Z")


def pull(number: int, merged: str | None, closed: str, *reviews: tuple[str, str]) -> dict[str, Any]:
    return {
        "number": number,
        "title": f"PR {number}",
        "url": f"https://github.com/o/r/pull/{number}",
        "mergedAt": merged,
        "closedAt": closed,
        "reviews": [{"author": {"login": login}, "state": state} for login, state in reviews],
    }


def test_proposals_matching_an_open_issue_are_duplicates(rt: ModuleType) -> None:
    issues = [{"number": 12, "title": "Add a check: CHANGELOG entries end with (#N)"}, {"number": 3, "title": "Other"}]
    titles = ["add a check  changelog entries end with #N.", "Teach the watch about stuck lanes"]
    verdicts = rt.dedupe(titles, issues)
    assert [(v.duplicate, v.existing) for v in verdicts] == [
        (12, "Add a check: CHANGELOG entries end with (#N)"),
        (None, ""),
    ]


def test_an_empty_proposal_title_is_refused(rt: ModuleType) -> None:
    with pytest.raises(SystemExit) as refused:
        rt.dedupe(["  ...  "], [])
    assert refused.value.code == 2


def test_refused_and_reworked_pulls_are_the_windows(rt: ModuleType) -> None:
    pulls = [
        pull(1, None, day(2)),  # refused
        pull(2, None, day(9)),  # refused before the window
        pull(3, day(1), day(1), ("bob", "CHANGES_REQUESTED"), ("bob", "APPROVED"), ("amy", "CHANGES_REQUESTED")),
        pull(4, day(1), day(1), ("bob", "APPROVED")),  # merged as first reviewed
        pull(5, day(8), day(8), ("bob", "CHANGES_REQUESTED")),  # merged before the window
    ]
    refused, reworked = rt.classify(pulls, START, END)
    assert [(p.number, p.why) for p in refused] == [(1, "closed without a merge")]
    assert [(p.number, p.why) for p in reworked] == [(3, "changes requested by @amy, @bob")]


def measures(*pairs: tuple[str, str]) -> dict[str, Any]:
    return {"measures": [{"measure": name, "text": text} for name, text in pairs]}


def test_the_windows_compare_measure_by_measure(rt: ModuleType) -> None:
    previous = measures(("Lead time for changes", "median 2.0 d"), ("Deploy frequency", "no data"))
    current = measures(("Deploy frequency", "1.0 per week"), ("Lead time for changes", "median 1.0 d"))
    assert [(c.measure, c.previous, c.current) for c in rt.compare(previous, current)] == [
        ("Deploy frequency", "no data", "1.0 per week"),
        ("Lead time for changes", "median 2.0 d", "median 1.0 d"),
    ]
    with pytest.raises(SystemExit):
        rt.compare(measures(), current)
