"""Spec 017, #340: GitHub's own relations (blockedBy, sub-issues) hold an issue like its text
holds do, without gh or the network"""

import importlib.util
import json
import os
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from .test_triage_state import FakeGitHub, comment, first_page, forward, open_issue

SCRIPT = Path(__file__).resolve().parents[1] / "skills" / "github-issue-triage" / "scripts" / "triage_state.py"
REPO = ("o", "r")
TRIAGED = comment("Triage: implement")


@pytest.fixture(scope="module")
def ts() -> ModuleType:
    spec = importlib.util.spec_from_file_location("triage_state_native", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def node(ref: str, state: str = "OPEN", reason: str | None = None) -> dict[str, Any]:
    """A blockedBy or subIssues node as GitHub returns it, from owner/repo#N"""
    repo, _, number = ref.partition("#")
    return {"number": int(number), "state": state, "stateReason": reason, "repository": {"nameWithOwner": repo}}


def held(
    blocked_by: Sequence[dict[str, Any]] = (),
    children: Sequence[dict[str, Any]] = (),
    triaged: bool = True,
    body: str = "",
    labels: tuple[str, ...] = (),
) -> dict[str, Any]:
    item = open_issue(7, [TRIAGED] if triaged else [], labels=labels)
    item["body"] = body
    item["blockedBy"] = forward(list(blocked_by))
    item["subIssues"] = forward(list(children))
    return item


def classify(ts: ModuleType, item: dict[str, Any], states: dict[tuple[str, str, int], str] | None = None) -> Any:
    return ts.classify_open(item, "Triage:", "postponed", states or {}, None, REPO)


def test_s017_1_a_native_blocked_by_issue_holds_until_it_closes(ts: ModuleType) -> None:
    assert classify(ts, held([node("o/r#3")])) == ("BLOCKED", "o/r#3:open(native)")
    assert classify(ts, held([node("o/r#3", "CLOSED", "COMPLETED")])) == ("UNBLOCKED", "o/r#3:closed(native)")


def test_s017_1_blocked_by_past_50_is_paged(ts: ModuleType) -> None:
    item = open_issue(7, [TRIAGED])
    item["blockedBy"] = forward([node(f"o/r#{100 + i}", "CLOSED") for i in range(50)], "b1")
    item["subIssues"] = forward([])
    gh = FakeGitHub(
        ts,
        {
            ("QUERY", None, None): first_page([item]),
            ("BLOCKED_BY_PAGE", 7, "b1"): {"issue": {"blockedBy": forward([node("other/lib#5")])}},
        },
    )
    gh.names[ts.NATIVE_PAGES["blockedBy"]] = "BLOCKED_BY_PAGE"
    fetched = ts.fetch("o/r", 20, run=gh)["open"]["nodes"][0]
    assert len(fetched["blockedBy"]["nodes"]) == 51
    state, note = classify(ts, fetched)
    assert state == "BLOCKED"
    assert note.endswith("other/lib#5:open(native)")


def test_s017_2_a_parent_holds_until_its_last_child_closes(ts: ModuleType) -> None:
    # Children across repos, one closed: the note starts with the parent's progress
    item = held(children=[node("o/r#4", "CLOSED", "COMPLETED"), node("other/lib#5")])
    assert classify(ts, item) == ("BLOCKED", "children 1/2 closed o/r#4:closed(child) other/lib#5:open(child)")
    done = held(children=[node("o/r#4", "CLOSED", "COMPLETED"), node("other/lib#5", "CLOSED", "NOT_PLANNED")])
    assert classify(ts, done) == (
        "UNBLOCKED",
        "children 2/2 closed o/r#4:closed(child) other/lib#5:not_planned(child)",
    )


def test_s017_2_sub_issues_past_50_are_paged(ts: ModuleType) -> None:
    item = open_issue(7, [TRIAGED])
    item["blockedBy"] = forward([])
    item["subIssues"] = forward([node(f"o/r#{100 + i}", "CLOSED") for i in range(50)], "s1")
    gh = FakeGitHub(
        ts,
        {
            ("QUERY", None, None): first_page([item]),
            ("SUB_ISSUES_PAGE", 7, "s1"): {"issue": {"subIssues": forward([node("o/r#200")])}},
        },
    )
    gh.names[ts.NATIVE_PAGES["subIssues"]] = "SUB_ISSUES_PAGE"
    fetched = ts.fetch("o/r", 20, run=gh)["open"]["nodes"][0]
    state, note = classify(ts, fetched)
    assert (state, note.split(" ", 3)[:3]) == ("BLOCKED", ["children", "50/51", "closed"])


def test_s017_3_text_and_native_are_one_set(ts: ModuleType) -> None:
    # Named by a "Depends on" line and natively: listed once, as text, and its state from the forge
    both = held([node("o/r#3")], body="Depends on #3\n")
    assert classify(ts, both, {("o", "r", 3): "OPEN"}) == ("BLOCKED", "o/r#3:open")
    # Owner and repo names ignore case: the text's spelling stays, the issue counts once
    spelled = held([node("o/r#3", "CLOSED")], body="Depends on O/R#3\n")
    assert classify(ts, spelled, {("O", "R", 3): "OPEN"}) == ("UNBLOCKED", "O/R#3:closed")
    # A hold comment naming a native relation is one hold too
    comments = held([node("other/lib#5")])
    comments["comments"]["nodes"].append(comment("On hold: waits on other/lib#5"))
    assert classify(ts, comments, {("other", "lib", 5): "OPEN"}) == ("BLOCKED", "other/lib#5:open")


def test_s017_3_the_note_marks_native_child_and_not_planned(ts: ModuleType) -> None:
    item = held(
        [node("o/r#3", "CLOSED", "NOT_PLANNED"), node("o/r#9")],
        [node("o/r#4")],
        body="Depends on #4\nDepends on #2\n",
    )
    state, note = classify(ts, item, {("o", "r", 2): "CLOSED"})
    assert state == "BLOCKED"
    assert note == "children 0/1 closed o/r#2:closed o/r#3:not_planned(native) o/r#4:open(child) o/r#9:open(native)"
    # A sub-issue that is also blocking reads as a child, once
    twice = held([node("o/r#4")], [node("o/r#4")])
    assert classify(ts, twice) == ("BLOCKED", "children 0/1 closed o/r#4:open(child)")


def test_s017_4_an_untriaged_issue_reads_new_whatever_holds_it_natively(ts: ModuleType) -> None:
    for state in ("OPEN", "CLOSED"):
        item = held([node("o/r#3", state)], [node("other/lib#5", state)], triaged=False)
        assert classify(ts, item)[0] == "NEW", state


def test_native_holds_leave_in_progress_and_postponed_alone(ts: ModuleType) -> None:
    item = held([node("o/r#3")])
    item["timelineItems"] = {"nodes": [{"willCloseTarget": True, "source": {"number": 8, "state": "OPEN"}}]}
    assert classify(ts, item)[0] == "IN_PROGRESS"
    # With no native field at all (the forge refused them), nothing changes
    plain = open_issue(7, [TRIAGED])
    assert classify(ts, plain) == ("NEEDS_PR", "")


@pytest.mark.parametrize(
    ("error", "native"),
    [
        ({"message": "Field 'blockedBy' doesn't exist on type 'Issue'"}, True),
        ({"extensions": {"fieldName": "subIssues"}, "message": "undefined"}, True),
        ({"path": ["query", "repository", "open", "nodes", 0, "subIssues"], "message": "Forbidden"}, True),
        ({"message": "Field 'blockedByNope' doesn't exist on type 'Issue'"}, False),
        ({"type": "NOT_FOUND", "message": "Could not resolve to a Repository"}, False),
    ],
)
def test_s017_5_only_an_error_naming_the_fields_counts(ts: ModuleType, error: dict[str, Any], native: bool) -> None:
    assert (ts.native_refused({"errors": [error]}) is not None) is native
    # every error must name one: another error alongside is real
    other = {"type": "NOT_FOUND", "message": "Could not resolve to a Repository"}
    assert ts.native_refused({"errors": [error, other]}) is None


def test_s017_5_a_refusal_falls_back_to_text_holds_through_fetch(
    ts: ModuleType, capsys: pytest.CaptureFixture[str]
) -> None:
    refusal = {"errors": [{"message": "Field 'blockedBy' doesn't exist on type 'Issue'\nmore"}]}
    calls: list[str] = []

    def run(query: str, variables: dict[str, str | int]) -> dict[str, Any]:
        calls.append(query)
        if query == ts.QUERY:
            return refusal
        assert query == ts.TEXT_QUERY
        return {"data": {"repository": first_page([open_issue(7, [TRIAGED])])}}

    data = ts.fetch("o/r", 20, run=run)
    assert calls == [ts.QUERY, ts.TEXT_QUERY]
    assert "blockedBy" not in ts.TEXT_QUERY and "subIssues" not in ts.TEXT_QUERY
    assert classify(ts, data["open"]["nodes"][0])[0] == "NEEDS_PR"
    assert capsys.readouterr().err.splitlines() == [
        "note: native relations unavailable: Field 'blockedBy' doesn't exist on type 'Issue'"
    ]


# The script end to end with a stand-in gh on PATH: it answers the native query with the
# configured errors and exit 1 (as gh does for a GraphQL error), the text query with one
# triaged issue held by a comment, and the upstream query with that hold still open
FAKE_GH = """#!{python}
import json, os, sys
query = next(a[len("query="):] for a in sys.argv[1:] if a.startswith("query="))
fake = json.loads(open(os.environ["FAKE_GH_DATA"]).read())
if "blockedBy" in query:
    print(json.dumps({{"errors": fake["errors"]}}))
    sys.stderr.write("gh: " + fake["errors"][0]["message"] + "\\n")
    sys.exit(1)
if "issueOrPullRequest" in query:
    print(json.dumps({{"data": {{"r0": {{"issueOrPullRequest": {{"__typename": "Issue", "state": "OPEN"}}}}}}}}))
    sys.exit(0)
print(json.dumps({{"data": {{"repository": fake["first"]}}}}))
"""


def run_script(tmp_path: Path, errors: list[dict[str, Any]]) -> subprocess.CompletedProcess[str]:
    fakes = tmp_path / "fakes"
    fakes.mkdir()
    gh = fakes / "gh"
    gh.write_text(FAKE_GH.format(python=sys.executable), encoding="utf-8")
    gh.chmod(0o700)
    hold = open_issue(7, [comment("Triage: implement\n\nOn hold: waits on other/lib#5")])
    data = tmp_path / "gh.json"
    data.write_text(json.dumps({"errors": errors, "first": first_page([hold])}), encoding="utf-8")
    env = {"PATH": os.pathsep.join([str(fakes), "/usr/bin", "/bin"]), "FAKE_GH_DATA": str(data)}
    cmd = [sys.executable, str(SCRIPT), "o/r", "--json"]
    return subprocess.run(cmd, capture_output=True, text=True, check=False, env=env)


def test_s017_5_the_script_classifies_on_text_holds_and_notes_it_once(tmp_path: Path) -> None:
    errors = [
        {"path": ["query", "repository", "open", "nodes", "blockedBy"], "message": "Field 'blockedBy' doesn't exist"},
        {"path": ["query", "repository", "open", "nodes", "subIssues"], "message": "Field 'subIssues' doesn't exist"},
    ]
    proc = run_script(tmp_path, errors)
    assert proc.returncode == 0, proc.stderr
    assert [json.loads(line) for line in proc.stdout.splitlines()] == [
        {"number": 7, "state": "BLOCKED", "title": "issue 7", "note": "other/lib#5:open"}
    ]
    assert proc.stderr.splitlines() == ["note: native relations unavailable: Field 'blockedBy' doesn't exist"]


def test_s017_5_any_other_gh_error_still_exits_2(tmp_path: Path) -> None:
    proc = run_script(tmp_path, [{"type": "NOT_FOUND", "message": "Could not resolve to a Repository"}])
    assert proc.returncode == 2
    assert "native relations unavailable" not in proc.stderr
