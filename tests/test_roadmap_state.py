"""The product-intake skill's roadmap script (#70), without gh or the network"""

import datetime as dt
import importlib.util
import re
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "skills" / "product-intake" / "scripts"
TODAY = dt.date(2026, 10, 4)


@pytest.fixture(scope="module")
def rs() -> ModuleType:
    sys.path.insert(0, str(SCRIPTS))  # roadmap_state imports intake_state, as when run from its folder
    try:
        spec = importlib.util.spec_from_file_location("roadmap_state", SCRIPTS / "roadmap_state.py")
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
    finally:
        sys.path.remove(str(SCRIPTS))
    return module


def issue(
    number: int,
    body: str = "",
    *,
    labels: tuple[str, ...] = ("opportunity", "planned"),
    thumbs: int = 0,
    state: str = "OPEN",
    reason: str | None = None,
    milestone: str | None = None,
    comments: tuple[str, ...] = (),
) -> dict[str, Any]:
    return {
        "number": number,
        "title": f"Issue {number}",
        "state": state,
        "stateReason": reason,
        "closedAt": None,
        "body": body,
        "milestone": {"title": milestone} if milestone else None,
        "labels": {"pageInfo": {"hasNextPage": False}, "nodes": [{"name": n} for n in labels]},
        "reactions": {"totalCount": thumbs},
        "comments": {
            "pageInfo": {"hasPreviousPage": False, "startCursor": None},
            "nodes": [{"body": c, "createdAt": "2026-10-01T10:00:00Z"} for c in comments],
        },
    }


def opportunity(number: int, requests: int, thumbs: int = 0, spec: str = "", **kwargs: Any) -> dict[str, Any]:
    """An accepted opportunity with `requests` members (numbered from number * 100), whose
    thumbs-up are all on itself"""
    members = "\n".join(f"- #{number * 100 + i}" for i in range(requests))
    link = f"\n\nSpec: `docs/specs/{spec}`" if spec else ""
    return issue(number, f"## Evidence\n\n{members}{link}\n", thumbs=thumbs, **kwargs)


def proposal(number: int, listed: str, *, marker: bool = True, **kwargs: Any) -> dict[str, Any]:
    head = '<!-- shipyard:milestone title="0.12.0" due="2026-10-18" -->\n\n' if marker else ""
    return issue(number, f"{head}## Opportunities\n\n{listed}\n", labels=("milestone-proposal",), **kwargs)


def milestone(
    number: int, title: str, opened: int, closed: int = 0, due: str | None = None, state: str = "OPEN"
) -> dict[str, Any]:
    return {
        "number": number,
        "title": title,
        "state": state,
        "dueOn": f"{due}T07:00:00Z" if due else None,
        "open": {"totalCount": opened},
        "closed": {"totalCount": closed},
    }


class Fake:
    def __init__(
        self,
        milestones: list[dict[str, Any]],
        opportunities: list[dict[str, Any]],
        proposals: list[dict[str, Any]] | None = None,
    ) -> None:
        self.milestones = milestones
        self.opportunities = opportunities
        self.proposals = proposals or []

    @staticmethod
    def page(nodes: list[dict[str, Any]]) -> dict[str, Any]:
        return {"pageInfo": {"hasNextPage": False, "endCursor": None}, "nodes": nodes}

    def __call__(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        if "milestones(" in query:
            repo: dict[str, Any] = {"hasDiscussionsEnabled": False, "milestones": self.page(self.milestones)}
        elif "labels: [$label]" in query:
            found = self.opportunities if variables["label"] == "opportunity" else self.proposals
            repo = {"issues": self.page(found)}
        elif "issueOrPullRequest" in query:
            repo = {f"m{n}": {"reactions": {"totalCount": 0}} for n in re.findall(r"m(\d+):", query)}
        else:
            raise AssertionError(f"unexpected query: {query}")
        return {"data": {"repository": repo}}


def plan(rs: ModuleType, fake: Fake, specs: Path, wip: int = 5, cadence: int = 2) -> list[Any]:
    rows: list[Any] = rs.plan(
        "o/r",
        config=rs.Roadmap(wip=wip, cadence=cadence),
        today=TODAY,
        specs=specs,
        label="opportunity",
        accepted="planned",
        proposal_label="milestone-proposal",
        run=fake,
    )
    return rows


def states(rows: list[Any]) -> list[tuple[str, int | None]]:
    return [(r.state, r.number) for r in rows]


@pytest.fixture
def specs(tmp_path: Path) -> Path:
    folder = tmp_path / "docs" / "specs"
    folder.mkdir(parents=True)
    issues = "\n".join(f"- o/r#{n}" for n in (50, 51, 52))
    (folder / "007-export.md").write_text(f"# S-007: Export\n\n## Issues\n\n{issues}\n\n## Verification\n\n- #9\n")
    return folder


def test_load_is_the_opportunity_plus_its_specs_build_issues(rs: ModuleType, specs: Path) -> None:
    assert rs.load("Spec: docs/specs/007-export.md", specs) == (4, "S-007")
    assert rs.load("Spec PR not merged: docs/specs/008-import.md", specs) == (2, "")
    assert rs.load("no spec yet", specs) == (2, "")


def test_unplanned_opportunities_rank_by_evidence_per_load(rs: ModuleType, specs: Path) -> None:
    fake = Fake(
        [],
        [
            opportunity(1, requests=4, thumbs=8, spec="007-export.md"),  # evidence 12, load 4: 3 per load
            opportunity(2, requests=2, thumbs=2),  # 4, load 2: 2
            opportunity(3, requests=3, thumbs=5),  # 8, load 2: 4
            opportunity(4, requests=1, labels=("opportunity",)),  # not accepted
            opportunity(5, requests=9, milestone="0.11.0"),  # planned already
        ],
    )
    rows = plan(rs, fake, specs, wip=8)
    unplanned = [r for r in rows if r.state == "UNPLANNED"]
    assert [r.number for r in unplanned] == [3, 1, 2]
    assert unplanned[1].note == "rank 2: 4 requests, +1 8, load 4 (S-007)"
    assert unplanned[2].note == "rank 3: 2 requests, +1 2, load 2 (no spec yet)"
    (nxt,) = [r for r in rows if r.state == "NEXT"]
    assert nxt.opportunities == [3, 1, 2] and nxt.note == "capacity 8 of wip 8, load 8, due 2026-10-18"


def test_the_next_milestone_fills_the_free_capacity_only(rs: ModuleType, specs: Path) -> None:
    opportunities = [
        opportunity(1, requests=4, thumbs=8, spec="007-export.md"),
        opportunity(2, requests=2, thumbs=2),
        opportunity(3, requests=3, thumbs=5),
    ]
    busy = [milestone(1, "0.11.0", opened=2, closed=3, due="2026-10-11")]
    rows = plan(rs, Fake(busy, opportunities), specs, wip=6, cadence=1)
    (nxt,) = [r for r in rows if r.state == "NEXT"]
    # 4 free: #3 (load 2) fits, #1 (load 4) doesn't, #2 (load 2) does; due a week after 0.11.0
    assert nxt.opportunities == [3, 2] and nxt.note == "capacity 4 of wip 6, load 4, due 2026-10-18"
    full = [milestone(1, "0.11.0", opened=5, due="2026-10-11")]
    assert "NEXT" not in {r.state for r in plan(rs, Fake(full, opportunities), specs, wip=6)}


def test_more_open_issues_than_wip_is_flagged(rs: ModuleType, specs: Path) -> None:
    fake = Fake([milestone(1, "0.11.0", opened=4), milestone(2, "0.12.0", opened=3)], [])
    rows = plan(rs, fake, specs, wip=5)
    assert ("WIP_OVER", None) in states(rows)
    assert [r.note for r in rows if r.state == "WIP_OVER"] == ["7 open issues in open milestones, wip 5"]
    assert "WIP_OVER" not in {r.state for r in plan(rs, fake, specs, wip=7)}


def test_milestone_progress_and_overdue(rs: ModuleType, specs: Path) -> None:
    fake = Fake(
        [
            milestone(1, "0.10.0", opened=1, closed=4, due="2026-10-01"),
            milestone(2, "0.11.0", opened=0, closed=3, due="2026-10-01"),  # done, only not closed yet
            milestone(3, "0.12.0", opened=2, due="2026-10-20"),
            milestone(4, "0.9.0", opened=0, closed=9, due="2026-09-01", state="CLOSED"),
        ],
        [],
    )
    rows = {r.number: (r.state, r.note) for r in plan(rs, fake, specs, wip=10)}
    assert rows == {
        1: ("OVERDUE", "4/5 closed, due 2026-10-01"),
        2: ("MILESTONE", "3/3 closed, due 2026-10-01"),
        3: ("MILESTONE", "0/2 closed, due 2026-10-20"),
    }


def test_an_open_proposal_holds_its_opportunities_and_the_next_one(rs: ModuleType, specs: Path) -> None:
    fake = Fake([], [opportunity(1, 2), opportunity(2, 1)], [proposal(30, "- #1")])
    assert states(plan(rs, fake, specs)) == [("UNPLANNED", 2), ("PROPOSAL_OPEN", 30)]


def test_an_approved_proposal_is_applied_once(rs: ModuleType, specs: Path) -> None:
    approve: dict[str, Any] = {"state": "CLOSED", "reason": "COMPLETED", "comments": ("Approved, thanks",)}
    listed = "- #1\n- #2"
    opportunities = [opportunity(1, 2), opportunity(2, 1)]
    rows = plan(rs, Fake([], opportunities, [proposal(30, listed, **approve)]), specs)
    assert [(r.state, r.note) for r in rows if r.number == 30] == [
        ("APPROVED", "create milestone 0.12.0 due 2026-10-18")
    ]
    half = [opportunity(1, 2, milestone="0.12.0"), opportunity(2, 1)]
    rows = plan(rs, Fake([milestone(1, "0.12.0", opened=1)], half, [proposal(30, listed, **approve)]), specs)
    assert [(r.state, r.note) for r in rows if r.number == 30] == [("APPROVED", "add #2 to 0.12.0")]
    done = [opportunity(1, 2, milestone="0.12.0"), opportunity(2, 1, milestone="0.12.0")]
    rows = plan(rs, Fake([milestone(1, "0.12.0", opened=2)], done, [proposal(30, listed, **approve)]), specs)
    assert 30 not in {r.number for r in rows}


def test_an_approved_proposal_holds_the_next_plan_until_applied(rs: ModuleType, specs: Path) -> None:
    """Approved, the milestone not created yet: its opportunities aren't proposed a second time"""
    approve: dict[str, Any] = {"state": "CLOSED", "reason": "COMPLETED", "comments": ("approve",)}
    opportunities = [opportunity(1, 2), opportunity(2, 1), opportunity(3, 1)]
    rows = plan(rs, Fake([], opportunities, [proposal(30, "- #1\n- #2", **approve)]), specs)
    assert states(rows) == [("APPROVED", 30), ("UNPLANNED", 3)]
    half = [opportunity(1, 2, milestone="0.12.0"), opportunity(2, 1), opportunity(3, 1)]
    rows = plan(rs, Fake([milestone(1, "0.12.0", opened=1)], half, [proposal(30, "- #1\n- #2", **approve)]), specs)
    assert states(rows) == [("APPROVED", 30), ("UNPLANNED", 3), ("MILESTONE", 1)]
    unreadable = proposal(31, "- #1\n- #2", marker=False, **approve)
    assert states(plan(rs, Fake([], opportunities, [unreadable]), specs)) == [("UNREADABLE", 31), ("UNPLANNED", 3)]


def test_a_proposal_closed_without_approval_or_unreadable(rs: ModuleType, specs: Path) -> None:
    declined = proposal(30, "- #1", state="CLOSED", reason="NOT_PLANNED", comments=("Not now",))
    unapproved = proposal(31, "- #1", state="CLOSED", reason="COMPLETED", comments=("I disapprove",))
    unreadable = proposal(32, "- #1", marker=False)
    rows = plan(rs, Fake([], [opportunity(1, 1)], [declined, unapproved, unreadable]), specs)
    assert states(rows) == [("UNREADABLE", 32)]  # still an open proposal: #1 waits on it


def test_the_roadmap_config_defaults_and_limits(rs: ModuleType, tmp_path: Path) -> None:
    config = tmp_path / "shipyard.toml"
    assert rs.roadmap(None) == rs.Roadmap(wip=5, cadence=2)
    config.write_text('name = "x"\n')
    assert rs.roadmap(config) == rs.Roadmap(wip=5, cadence=2)
    config.write_text("[roadmap]\nwip = 3\ncadence = 1\n")
    assert rs.roadmap(config) == rs.Roadmap(wip=3, cadence=1)
    for bad in ("wip = 0", "wip = 101", "cadence = 27", 'cadence = "2w"', "wip = true"):
        config.write_text(f"[roadmap]\n{bad}\n")
        with pytest.raises(SystemExit):
            rs.roadmap(config)
