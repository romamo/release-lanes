"""The github-issue-triage skill's classification, without gh or the network"""

import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "skills" / "github-issue-triage" / "scripts" / "triage_state.py"


@pytest.fixture(scope="module")
def ts() -> ModuleType:
    spec = importlib.util.spec_from_file_location("triage_state", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def issue(*comments: tuple[str, str], labels: tuple[str, ...] = ()) -> dict[str, Any]:
    """Comments as (createdAt, body), oldest first, as GitHub returns them"""
    return {
        "number": 7,
        "labels": {"nodes": [{"name": n} for n in labels]},
        "comments": {"nodes": [{"createdAt": at, "body": body} for at, body in comments]},
        "timelineItems": {"nodes": []},
    }


def tag(name: str, date: str) -> dict[str, Any]:
    return {"name": name, "target": {"tagger": {"date": date}}}


def classify(ts: ModuleType, item: dict[str, Any], stable: tuple[Any, str] | None = None) -> str:
    state: str = ts.classify_open(item, "Triage:", "postponed", {}, stable)[0]
    return state


def test_the_latest_triage_verdict_wins(ts: ModuleType) -> None:
    item = issue(
        ("2026-09-01T10:00:00Z", "Triage: clarify, which flag?"),
        ("2026-09-02T10:00:00Z", "Triage: implement"),
    )
    assert classify(ts, item) == "NEEDS_PR"


def test_an_old_implement_verdict_does_not_outlive_a_newer_one(ts: ModuleType) -> None:
    item = issue(
        ("2026-09-01T10:00:00Z", "Triage: implement"),
        ("2026-09-02T10:00:00Z", "Triage: clarify, the reporter's repro fails on main"),
    )
    assert classify(ts, item) == "TRIAGED"


def test_postponed_before_the_stable_tag_is_revisited(ts: ModuleType) -> None:
    item = issue(("2026-09-01T10:00:00Z", "Triage: postpone"), labels=("postponed",))
    stable = ts.latest_stable([tag("v1.2.0", "2026-09-10T10:00:00Z")])
    assert classify(ts, item, stable) == "REVISIT"


def test_a_re_decision_after_the_stable_tag_clears_revisit(ts: ModuleType) -> None:
    item = issue(
        ("2026-09-01T10:00:00Z", "Triage: postpone"),
        ("2026-09-20T10:00:00Z", "Triage: postpone again, still out of scope"),
        labels=("postponed",),
    )
    stable = ts.latest_stable([tag("v1.2.0", "2026-09-10T10:00:00Z")])
    assert classify(ts, item, stable) == "POSTPONED"


def test_a_tag_with_an_offset_compares_by_instant_not_by_text(ts: ModuleType) -> None:
    # 12:00+03:00 is 09:00Z, before the 10:00Z verdict; as text "12:00" sorts after "10:00"
    item = issue(("2026-09-10T10:00:00Z", "Triage: postpone"), labels=("postponed",))
    stable = ts.latest_stable([tag("v1.2.0", "2026-09-10T12:00:00+03:00")])
    assert classify(ts, item, stable) == "POSTPONED"


def test_the_latest_stable_tag_is_picked_by_instant(ts: ModuleType) -> None:
    tags = [tag("v1.2.0", "2026-09-10T12:00:00+03:00"), tag("v1.1.0", "2026-09-10T10:00:00Z")]
    assert ts.latest_stable(tags)[1] == "v1.1.0"


def test_a_time_without_a_timezone_fails(ts: ModuleType) -> None:
    with pytest.raises(SystemExit) as exc:
        ts.timestamp("2026-09-10T10:00:00")
    assert exc.value.code == 2
