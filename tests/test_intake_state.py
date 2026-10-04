"""The product-intake skill's state script (#69), without gh or the network"""

import importlib.util
import re
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "skills" / "product-intake" / "scripts" / "intake_state.py"


@pytest.fixture(scope="module")
def ist() -> ModuleType:
    spec = importlib.util.spec_from_file_location("intake_state", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def issue(
    number: int,
    *comments: str,
    labels: tuple[str, ...] = (),
    body: str = "",
    thumbs: int = 0,
    state: str = "OPEN",
    reason: str | None = None,
    closed_at: str | None = None,
    commented_at: str = "2026-10-01T10:00:00Z",
) -> dict[str, Any]:
    return {
        "number": number,
        "title": f"Issue {number}",
        "url": f"https://github.com/o/r/issues/{number}",
        "state": state,
        "stateReason": reason,
        "closedAt": closed_at,
        "body": body,
        "labels": {"pageInfo": {"hasNextPage": False}, "nodes": [{"name": n} for n in labels]},
        "reactions": {"totalCount": thumbs},
        "comments": {
            "pageInfo": {"hasPreviousPage": False, "startCursor": None},
            "nodes": [{"body": c, "createdAt": commented_at} for c in comments],
        },
    }


def opportunity(number: int, members: str, *comments: str, **kwargs: Any) -> dict[str, Any]:
    body = f"## Problem\n\nSlow.\n\n## Evidence\n\n{members}\n\n## Success\n\nFast, unlike #999.\n"
    labels = ("opportunity", *kwargs.pop("labels", ()))
    return issue(number, *comments, body=body, labels=labels, **kwargs)


def discussion(number: int, *, closed: bool = False, category: str = "Ideas", thumbs: int = 0) -> dict[str, Any]:
    return {
        "number": number,
        "title": f"Discussion {number}",
        "url": f"https://github.com/o/r/discussions/{number}",
        "closed": closed,
        "category": {"name": category},
        "reactions": {"totalCount": thumbs},
    }


class Fake:
    """GitHub, answering each query from fixtures; records the queries it was asked"""

    def __init__(
        self,
        opened: list[dict[str, Any]],
        opportunities: list[dict[str, Any]],
        discussions: list[dict[str, Any]] | None = None,
        closed_thumbs: dict[int, int] | None = None,
    ) -> None:
        self.opened = opened
        self.opportunities = opportunities
        self.discussions = discussions
        self.closed_thumbs = closed_thumbs or {}
        self.asked: list[str] = []

    @staticmethod
    def page(nodes: list[dict[str, Any]]) -> dict[str, Any]:
        return {"pageInfo": {"hasNextPage": False, "endCursor": None}, "nodes": nodes}

    def __call__(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        self.asked.append(query)
        if "labels: [$label]" in query:
            repo: dict[str, Any] = {"issues": self.page(self.opportunities)}
        elif "issues(states: OPEN" in query:
            repo = {"hasDiscussionsEnabled": self.discussions is not None, "issues": self.page(self.opened)}
        elif "discussions(" in query:
            repo = {"discussions": self.page(self.discussions or [])}
        elif "issueOrPullRequest" in query:
            numbers = [int(n) for n in re.findall(r"m(\d+):", query)]
            repo = {f"m{n}": {"reactions": {"totalCount": self.closed_thumbs.get(n, 0)}} for n in numbers}
        else:
            raise AssertionError(f"unexpected query: {query}")
        return {"data": {"repository": repo}}


def states(ist: ModuleType, fake: Fake, **kwargs: Any) -> dict[int, tuple[str, str]]:
    options: dict[str, Any] = {
        "label": "opportunity",
        "accepted": "planned",
        "marker": "Triage:",
        "skip": set(ist.SKIP_LABELS),
        "categories": None,
        "autonomy": "propose",
        **kwargs,
    }
    intake = ist.fetch("o/r", run=fake, **options)
    return {r.number: (r.state, r.note) for r in ist.rows(intake)}


def test_evidence_reads_only_the_evidence_section_of_this_repo(ist: ModuleType) -> None:
    body = (
        "Fixes nothing, see #1.\n\n## Evidence\n\n- #12 (+1 3)\n- o/r#13\n- other/repo#14\n"
        "- https://github.com/O/R/discussions/15\n- https://github.com/o/r/issues/16#issuecomment-1\n"
        "- https://github.com/x/y/issues/17\n\n## Success\n\n- #18\n"
    )
    assert ist.evidence(body, "o", "r") == {12, 13, 15, 16}
    assert ist.evidence("", "o", "r") == set()


def test_the_verdict_is_the_first_bold_word_of_a_triage_comment(ist: ModuleType) -> None:
    assert ist.verdict("Triage: **implement**, phase 3", "Triage:") == "implement"
    assert ist.verdict("  Triage: **postponed to 1.1**. A feature later", "Triage:") == "postponed to 1.1"
    assert ist.verdict("Triage: clarify which flag", "Triage:") == "clarify"
    assert ist.verdict("Thanks!", "Triage:") is None


def test_feedback_is_what_triage_left_to_intake(ist: ModuleType) -> None:
    fake = Fake(
        [
            issue(1),  # untriaged
            issue(2, "Triage: **implement**"),  # a bug or a contract change: triage's
            issue(3, "Triage: **feature**. Spec in #9"),  # already in the spec gate
            issue(4, "Triage: **postponed to 1.1**"),  # postponed requests are demand
            issue(5, labels=("bug",)),
            issue(6, "Triage: **won't fix**"),
            issue(7, "Triage: **implement**", "Triage: **needs a decision**"),  # the newest verdict counts
        ],
        [],
    )
    found = states(ist, fake)
    assert {n for n, (s, _) in found.items() if s == "NEW_FEEDBACK"} == {1, 4, 7}


def test_an_opportunity_groups_its_members_instead_of_a_second_one(ist: ModuleType) -> None:
    fake = Fake(
        [issue(1, thumbs=2), issue(2, thumbs=1), issue(3)],
        [opportunity(10, "- #1\n- #2\n- #4", thumbs=1)],
        closed_thumbs={4: 5},
    )
    found = states(ist, fake)
    assert found == {10: ("OPPORTUNITY_OPEN", ""), 3: ("NEW_FEEDBACK", "issue")}
    intake = ist.fetch(
        "o/r",
        label="opportunity",
        accepted="planned",
        marker="Triage:",
        skip=set(),
        categories=None,
        autonomy="propose",
        run=fake,
    )
    (row,) = [r for r in ist.rows(intake) if r.number == 10]
    assert row.members == [1, 2, 4] and row.thumbs == 1 + 2 + 1 + 5  # its own, and each member's, closed too


def test_a_request_in_two_live_opportunities_overlaps(ist: ModuleType) -> None:
    fake = Fake(
        [issue(1)],
        [
            opportunity(10, "- #1"),
            opportunity(11, "- #1", labels=("planned",)),
            opportunity(12, "- #1", state="CLOSED", reason="DUPLICATE", closed_at="2026-10-02T10:00:00Z"),
        ],
    )
    found = states(ist, fake)
    assert found[1] == ("OVERLAP", "#10:opportunity_open, #11:accepted")
    assert found[12] == ("MERGED", "closed as a duplicate")


def test_the_maintainers_call_sets_each_opportunity_state(ist: ModuleType) -> None:
    closed = "2026-10-02T10:00:00Z"
    fake = Fake(
        [],
        [
            opportunity(10, "- #1", "Triage: **opportunity**, waiting for the maintainer"),
            opportunity(11, "- #2", "Triage: **opportunity**", labels=("planned",)),
            opportunity(
                12, "- #3", "Triage: **opportunity**", "Triage: **feature**. On hold: o/r#40", labels=("planned",)
            ),
            opportunity(
                13,
                "- #4",
                "Triage: **opportunity**",
                "Out of scope: shipyard stays GitHub-only (D-6).\nMore detail.",
                state="CLOSED",
                reason="NOT_PLANNED",
                closed_at=closed,
                commented_at="2026-10-02T09:59:58Z",
            ),
            opportunity(14, "- #5", "Triage: **opportunity**", state="CLOSED", reason="NOT_PLANNED", closed_at=closed),
            opportunity(15, "- #6", state="CLOSED", reason="COMPLETED", closed_at=closed),
        ],
    )
    found = states(ist, fake)
    assert found[10] == ("OPPORTUNITY_OPEN", "")
    assert found[11] == ("ACCEPTED", "hand it to triage's spec gate")
    assert found[12] == ("HANDED_OFF", "triage: feature")
    assert found[13] == ("DECLINED", "Out of scope: shipyard stays GitHub-only (D-6).")
    assert found[14] == ("NO_REASON", "closed as not planned with no comment")
    assert found[15] == ("DONE", "")


def test_an_old_comment_is_not_a_decline_reason(ist: ModuleType) -> None:
    old = opportunity(
        13,
        "- #4",
        "I want this",
        state="CLOSED",
        reason="NOT_PLANNED",
        closed_at="2026-10-05T10:00:00Z",
        commented_at="2026-10-01T10:00:00Z",
    )
    assert states(ist, Fake([], [old]))[13][0] == "NO_REASON"


def test_a_later_reply_does_not_replace_the_decline_reason(ist: ModuleType) -> None:
    declined = opportunity(13, "- #4", state="CLOSED", reason="NOT_PLANNED", closed_at="2026-10-02T10:00:00Z")
    declined["comments"]["nodes"] = [
        {"body": "Out of scope (D-6).", "createdAt": "2026-10-02T09:59:58Z"},
        {"body": "Triage: **opportunity**", "createdAt": "2026-10-03T10:00:00Z"},
        {"body": "Please reconsider, we need this", "createdAt": "2026-10-04T10:00:00Z"},
    ]
    assert states(ist, Fake([], [declined]))[13] == ("DECLINED", "Out of scope (D-6).")


def test_a_roadmap_tracking_issue_is_not_feedback(ist: ModuleType) -> None:
    """The default skip labels include roadmap (#26 read as NEW_FEEDBACK); --skip-label adds to them"""
    fake = Fake([issue(1, labels=("roadmap",)), issue(2, labels=("epic",)), issue(3)], [])
    assert states(ist, fake) == {2: ("NEW_FEEDBACK", "issue"), 3: ("NEW_FEEDBACK", "issue")}
    skip = ist.skip_labels(["epic"])
    assert skip == {*ist.SKIP_LABELS, "epic"}
    assert states(ist, fake, skip=skip) == {3: ("NEW_FEEDBACK", "issue")}


def test_a_declined_groups_members_are_never_proposed_again(ist: ModuleType) -> None:
    """A new request the skill links into the declined opportunity's evidence reads as declined"""
    fake = Fake(
        [issue(1), issue(2)],
        [
            opportunity(
                13,
                "- #1\n- #2",
                "No: out of scope.",
                state="CLOSED",
                reason="NOT_PLANNED",
                closed_at="2026-10-01T10:00:00Z",
            )
        ],
    )
    assert states(ist, fake) == {13: ("DECLINED", "No: out of scope.")}


def test_discussions_count_when_enabled_outside_announcements(ist: ModuleType) -> None:
    fake = Fake(
        [],
        [opportunity(10, "- https://github.com/o/r/discussions/21")],
        discussions=[
            discussion(20, thumbs=4),
            discussion(21),
            discussion(22, closed=True),
            discussion(23, category="Announcements"),
        ],
    )
    assert states(ist, fake) == {10: ("OPPORTUNITY_OPEN", ""), 20: ("NEW_FEEDBACK", "discussion")}
    assert states(ist, fake, categories={"Announcements"}) == {
        10: ("OPPORTUNITY_OPEN", ""),
        23: ("NEW_FEEDBACK", "discussion"),
    }


def test_no_discussions_query_when_they_are_off(ist: ModuleType) -> None:
    fake = Fake([issue(1)], [])
    states(ist, fake)
    assert not any("discussions(" in q for q in fake.asked)


def test_more_than_100_labels_is_refused(ist: ModuleType) -> None:
    capped = issue(1)
    capped["labels"]["pageInfo"]["hasNextPage"] = True
    with pytest.raises(SystemExit) as caught:
        states(ist, Fake([capped], []))
    assert caught.value.code == 2


def test_intake_autonomy_defaults_to_propose_and_never_acts(ist: ModuleType, tmp_path: Path) -> None:
    config = tmp_path / "shipyard.toml"
    assert ist.intake_autonomy(None) == "propose"
    config.write_text('name = "x"\n[autonomy]\nrelease = "act"\ndeploy.production = "act"\n')
    assert ist.intake_autonomy(config) == "propose"
    config.write_text('[autonomy]\nintake = "observe"  # report only\n')
    assert ist.intake_autonomy(config) == "observe"
    for bad in ('"act"', '"auto"', "1"):
        config.write_text(f"[autonomy]\nintake = {bad}\n")
        with pytest.raises(SystemExit):
            ist.intake_autonomy(config)


def test_the_310_reader_reads_plain_lines_and_refuses_the_rest(ist: ModuleType, tmp_path: Path) -> None:
    path = tmp_path / "shipyard.toml"
    text = "[lanes.dev]\nintake = \"no\"\n[autonomy]\nintake = 'observe'\nwip = 3\non = true\n[roadmap]\nwip = 4\n"
    assert ist.config_values_310(text, path, "autonomy", ("intake", "wip", "on")) == {
        "intake": "observe",
        "wip": 3,
        "on": True,
    }
    assert ist.config_values_310(text, path, "roadmap", ("wip", "cadence")) == {"wip": 4}
    for unreadable in ('autonomy.intake = "observe"\n', '[autonomy]\nintake = ["observe"]\n'):
        with pytest.raises(SystemExit):
            ist.config_values_310(unreadable, path, "autonomy", ("intake",))


COMMENTS = SCRIPT.parents[2] / "github-issue-triage" / "references" / "comments.md"


def triage_template(section: str, index: int = 0) -> str:
    """The ``index``-th markdown template under a ``## section`` heading of triage's comments.md"""
    text = COMMENTS.read_text(encoding="utf-8").split(f"\n## {section}\n", 1)[1].split("\n## ", 1)[0]
    return text.split("```markdown\n")[index + 1].split("\n```", 1)[0]


def test_a_request_triage_hands_over_is_feedback_until_an_opportunity_lists_it(ist: ModuleType) -> None:
    handed = triage_template("Opportunity")
    assert ist.verdict(handed, "Triage:") == "opportunity"
    covered = triage_template("Feature", 1)  # an accepted opportunity covers it: triage's
    fake = Fake([issue(1, handed), issue(2, handed), issue(3, covered)], [opportunity(10, "- #2")])
    found = states(ist, fake)
    assert found[1][0] == "NEW_FEEDBACK"  # handed over, not yet grouped
    assert found[10][0] == "OPPORTUNITY_OPEN"
    assert 2 not in found  # grouped: a member of #10, no row of its own
    assert 3 not in found  # not feedback: triage builds it under the accepted opportunity
