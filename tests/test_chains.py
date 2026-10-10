"""chains.py link: a dependency written as GitHub's native relation and as a Depends on line"""

import importlib.util
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "skills" / "github-issue-triage" / "scripts"


@pytest.fixture(scope="module")
def ch() -> ModuleType:
    sys.path.insert(0, str(SCRIPTS))  # chains imports triage_state, as when run from its folder
    try:
        spec = importlib.util.spec_from_file_location("chains", SCRIPTS / "chains.py")
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
    finally:
        sys.path.remove(str(SCRIPTS))
    return module


def error(message: str, kind: str | None = None) -> dict[str, Any]:
    return {"message": message, **({"type": kind} if kind else {})}


class Forge:
    """A fake GitHub behind the GraphQL runner: issues, the native relations between them,
    and what it refuses. Every call is recorded"""

    def __init__(self, ch: ModuleType) -> None:
        self.ch = ch
        self.issues: dict[tuple[str, str, int], dict[str, str]] = {}
        self.blocked: set[tuple[str, str]] = set()  # (issue id, blocking issue id)
        self.parents: dict[str, str] = {}  # child id: parent id
        self.refuse_read: str | None = None  # the forge has no relation fields
        self.refuse_write: str | None = None  # the forge refuses the mutation
        self.rate_limited = False
        # ("read" or "write", the error) any relation query or mutation gets
        self.relation_error: tuple[str, dict[str, Any]] | None = None
        self.page = 100
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def add(self, ref: str, body: str = "", kind: str = "Issue") -> str:
        repo, _, number = ref.partition("#")
        owner, _, name = repo.partition("/")
        node_id = f"I_{ref}"
        self.issues[(owner.lower(), name.lower(), int(number))] = {"id": node_id, "body": body, "type": kind}
        return node_id

    def body(self, ref: str) -> str:
        return next(i["body"] for i in self.issues.values() if i["id"] == f"I_{ref}")

    def writes(self) -> list[str]:
        mutations = {self.ch.ADD_BLOCKED_BY: "addBlockedBy", self.ch.ADD_SUB_ISSUE: "addSubIssue"}
        mutations[self.ch.UPDATE_BODY] = "updateIssue"
        return [mutations[q] for q, _ in self.calls if q in mutations]

    def __call__(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((query, variables))
        ch = self.ch
        if self.rate_limited:
            return {"errors": [error("API rate limit exceeded", "RATE_LIMITED")]}
        if query == ch.RESOLVE:
            key = (str(variables["owner"]).lower(), str(variables["name"]).lower(), variables["number"])
            found = self.issues.get(key)
            if found is None:
                missing = f"Could not resolve to an issue or pull request with the number of {variables['number']}."
                return {"data": {"repository": {"issueOrPullRequest": None}}, "errors": [error(missing, "NOT_FOUND")]}
            node = {"__typename": found["type"]}
            if found["type"] == "Issue":
                node |= {"id": found["id"], "body": found["body"]}
            return {"data": {"repository": {"issueOrPullRequest": node}}}
        if query == ch.UPDATE_BODY:
            issue = next(i for i in self.issues.values() if i["id"] == variables["id"])
            issue["body"] = variables["body"]
            return {"data": {"updateIssue": {"issue": {"id": variables["id"]}}}}
        reads, writes = (ch.BLOCKED_BY, ch.PARENT), (ch.ADD_BLOCKED_BY, ch.ADD_SUB_ISSUE)
        if self.relation_error and query in {"read": reads, "write": writes}[self.relation_error[0]]:
            return {"data": None, "errors": [self.relation_error[1]]}
        if query in reads and self.refuse_read:
            # GitHub's shape for a field its schema doesn't have
            field = "blockedBy" if query == ch.BLOCKED_BY else "parent"
            undefined = {"code": "undefinedField", "typeName": "Issue", "fieldName": field}
            return {"errors": [{**error(self.refuse_read), "extensions": undefined}]}
        if query == ch.BLOCKED_BY:
            ids = sorted(b for a, b in self.blocked if a == variables["id"])
            start = int(variables.get("cursor") or 0)
            end = start + self.page
            more = end < len(ids)
            info = {"hasNextPage": more, "endCursor": str(end) if more else None}
            nodes = [{"id": i} for i in ids[start:end]]
            return {"data": {"node": {"blockedBy": {"pageInfo": info, "nodes": nodes}}}}
        if query == ch.PARENT:
            parent = self.parents.get(str(variables["id"]))
            return {"data": {"node": {"parent": {"id": parent} if parent else None}}}
        if query in (ch.ADD_BLOCKED_BY, ch.ADD_SUB_ISSUE) and self.refuse_write:
            return {"data": None, "errors": [error(self.refuse_write, "UNPROCESSABLE")]}
        if query == ch.ADD_BLOCKED_BY:
            self.blocked.add((str(variables["issue"]), str(variables["blocking"])))
            return {"data": {"addBlockedBy": {"issue": {"id": variables["issue"]}}}}
        if query == ch.ADD_SUB_ISSUE:
            self.parents[str(variables["sub"])] = str(variables["issue"])
            return {"data": {"addSubIssue": {"issue": {"id": variables["issue"]}}}}
        raise AssertionError(f"unexpected query {query}")


@pytest.fixture
def forge(ch: ModuleType) -> Forge:
    return Forge(ch)


def link(ch: ModuleType, forge: Forge, *argv: str) -> int:
    result: int = ch.main(["link", *argv], run=forge)
    return result


def test_s017_6_blocked_by_writes_both_forms_and_again_changes_nothing(
    ch: ModuleType, forge: Forge, capsys: pytest.CaptureFixture[str]
) -> None:
    a = forge.add("o/r#5", "Build the page.\n")
    b = forge.add("o/r#4")
    assert link(ch, forge, "o/r#5", "--blocked-by", "o/r#4") == 0
    assert (a, b) in forge.blocked
    assert forge.body("o/r#5") == "Build the page.\n\nDepends on o/r#4"
    out = capsys.readouterr().out
    assert "linked: o/r#5 is blocked by o/r#4" in out and "added to o/r#5: Depends on o/r#4" in out

    forge.calls.clear()
    assert link(ch, forge, "o/r#5", "--blocked-by", "o/r#4") == 0
    assert forge.writes() == []
    assert capsys.readouterr().out.strip() == "already linked"
    assert forge.body("o/r#5") == "Build the page.\n\nDepends on o/r#4"


def test_s017_6_the_body_is_kept_byte_for_byte(ch: ModuleType, forge: Forge) -> None:
    for body, expected in [
        ("", "Depends on o/r#4"),
        ("Text without a newline", "Text without a newline\n\nDepends on o/r#4"),
        ("Windows\r\nlines  \r\n", "Windows\r\nlines  \r\n\r\nDepends on o/r#4"),
        ("Steps\n\n- Depends on o/r#3\n", "Steps\n\n- Depends on o/r#3\nDepends on o/r#4"),
        ("Ends blank\n\n", "Ends blank\n\nDepends on o/r#4"),
    ]:
        forge = Forge(ch)
        forge.add("o/r#5", body)
        forge.add("o/r#4")
        forge.add("o/r#3")
        assert link(ch, forge, "o/r#5", "--blocked-by", "o/r#4") == 0
        assert forge.body("o/r#5") == expected
        assert forge.body("o/r#5").startswith(body)


def test_s017_6_only_the_missing_half_is_written(
    ch: ModuleType, forge: Forge, capsys: pytest.CaptureFixture[str]
) -> None:
    # The relation is there (set in the UI), the line isn't: only the line is added
    a = forge.add("o/r#5", "Body")
    b = forge.add("o/r#4")
    forge.blocked.add((a, b))
    assert link(ch, forge, "o/r#5", "--blocked-by", "o/r#4") == 0
    assert forge.writes() == ["updateIssue"]
    assert "already linked" not in capsys.readouterr().out

    # The line names it as a plain #4 of the same repo, the relation isn't there: only it is added
    forge = Forge(ch)
    a = forge.add("o/r#5", "Depends on #4 and #9\n")
    b = forge.add("O/R#4")
    assert link(ch, forge, "o/r#5", "--blocked-by", "o/r#4") == 0
    assert forge.writes() == ["addBlockedBy"]
    assert forge.body("o/r#5") == "Depends on #4 and #9\n"
    out = capsys.readouterr().out
    assert "linked: o/r#5 is blocked by o/r#4" in out and "added to" not in out


def test_s017_6_a_plain_number_of_another_repo_or_a_mention_elsewhere_does_not_count(
    ch: ModuleType, forge: Forge
) -> None:
    forge.add("o/r#5", "Depends on #4\nSee other/r#4 too\n")
    forge.add("other/r#4")
    assert link(ch, forge, "o/r#5", "--blocked-by", "other/r#4") == 0
    assert forge.body("o/r#5") == "Depends on #4\nSee other/r#4 too\n\nDepends on other/r#4"


def test_s017_6_blocked_by_is_paged(ch: ModuleType, forge: Forge, capsys: pytest.CaptureFixture[str]) -> None:
    forge.page = 2
    a = forge.add("o/r#5", "Depends on o/r#4")
    b = forge.add("o/r#4")
    for n in range(10, 15):
        forge.blocked.add((a, forge.add(f"o/r#{n}")))
    forge.blocked.add((a, b))
    assert link(ch, forge, "o/r#5", "--blocked-by", "o/r#4") == 0
    assert forge.writes() == []
    assert capsys.readouterr().out.strip() == "already linked"


def test_s017_7_child_adds_a_sub_issue_and_a_depends_on_line(
    ch: ModuleType, forge: Forge, capsys: pytest.CaptureFixture[str]
) -> None:
    p = forge.add("o/r#1", "The feature\n")
    c = forge.add("o/other#2")
    assert link(ch, forge, "o/r#1", "--child", "o/other#2") == 0
    assert forge.parents[c] == p
    assert forge.body("o/r#1") == "The feature\n\nDepends on o/other#2"
    assert "linked: o/other#2 is a sub-issue of o/r#1" in capsys.readouterr().out

    assert link(ch, forge, "o/r#1", "--child", "o/other#2") == 0
    assert capsys.readouterr().out.strip() == "already linked"


def test_s017_8_a_forge_without_the_relation_still_gets_the_text_line(
    ch: ModuleType, forge: Forge, capsys: pytest.CaptureFixture[str]
) -> None:
    forge.refuse_read = "Field 'blockedBy' doesn't exist on type 'Issue'\nmore detail"
    forge.add("o/r#5", "Body\n")
    forge.add("o/r#4")
    assert link(ch, forge, "o/r#5", "--blocked-by", "o/r#4") == 0
    assert forge.writes() == ["updateIssue"]
    assert forge.body("o/r#5") == "Body\n\nDepends on o/r#4"
    out = capsys.readouterr().out
    assert "text only: Field 'blockedBy' doesn't exist on type 'Issue'\n" in out + "\n"
    assert "more detail" not in out and "already linked" not in out


def test_s017_8_a_refused_cross_owner_link_still_gets_the_text_line(
    ch: ModuleType, forge: Forge, capsys: pytest.CaptureFixture[str]
) -> None:
    forge.refuse_write = "Issues must belong to the same owner"
    forge.add("o/r#1")
    forge.add("someone/else#2")
    assert link(ch, forge, "o/r#1", "--child", "someone/else#2") == 0
    assert forge.parents == {}
    assert forge.body("o/r#1") == "Depends on someone/else#2"
    assert "text only: Issues must belong to the same owner" in capsys.readouterr().out

    # Again: the line is there, the forge still refuses; nothing is written twice
    forge.calls.clear()
    assert link(ch, forge, "o/r#1", "--child", "someone/else#2") == 0
    assert forge.writes() == ["addSubIssue"]
    assert forge.body("o/r#1") == "Depends on someone/else#2"
    assert capsys.readouterr().out.strip() == "text only: Issues must belong to the same owner"


@pytest.mark.parametrize(
    ("argv", "named"),
    [
        (["o/r5", "--blocked-by", "o/r#4"], "'o/r5'"),
        (["o/r#5", "--blocked-by", "#4"], "'#4'"),
        (["o/r#5", "--child", "o/r#0"], "'o/r#0'"),
        (["o/r#5", "--blocked-by", "o/r#404"], "no issue o/r#404"),
        (["o/r#5", "--blocked-by", "o/r#7"], "o/r#7 is a pull request"),
        (["o/r#7", "--child", "o/r#4"], "o/r#7 is a pull request"),
        (["o/r#5", "--blocked-by", "O/R#5"], "o/r#5 can't be linked to itself"),
        (["o/r#5", "--child", "o/r#5"], "o/r#5 can't be linked to itself"),
    ],
)
def test_s017_9_bad_references_exit_2_naming_them_before_any_write(
    ch: ModuleType, forge: Forge, capsys: pytest.CaptureFixture[str], argv: list[str], named: str
) -> None:
    forge.add("o/r#5", "Body")
    forge.add("o/r#4")
    forge.add("o/r#7", kind="PullRequest")
    with pytest.raises(SystemExit) as exit_:
        link(ch, forge, *argv)
    assert exit_.value.code == 2
    assert named in capsys.readouterr().err
    assert forge.writes() == []
    assert forge.body("o/r#5") == "Body"


def test_a_rate_limit_is_a_gh_failure_not_a_refusal(
    ch: ModuleType, forge: Forge, capsys: pytest.CaptureFixture[str]
) -> None:
    forge.add("o/r#5")
    forge.add("o/r#4")
    forge.rate_limited = True
    with pytest.raises(SystemExit) as exit_:
        link(ch, forge, "o/r#5", "--blocked-by", "o/r#4")
    assert exit_.value.code == 2
    assert "rate limit" in capsys.readouterr().err
    assert forge.writes() == []


@pytest.mark.parametrize(
    "failure",
    [
        error("Something went wrong while executing your query. This may be the result of a timeout"),
        error("Could not resolve to a node with the global id of 'I_o/r#5'", "NOT_FOUND"),
        error("Internal server error", "INTERNAL"),
    ],
)
@pytest.mark.parametrize("on", ["read", "write"])
def test_a_transient_error_on_the_relation_is_a_gh_failure_not_a_refusal(
    ch: ModuleType, forge: Forge, capsys: pytest.CaptureFixture[str], failure: dict[str, Any], on: str
) -> None:
    # Only the forge refusing the relation goes text only; a timeout or an internal error
    # exits 2 with nothing written, so a rerun adds both halves
    forge.add("o/r#5", "Body")
    forge.add("o/r#4")
    forge.relation_error = (on, failure)
    with pytest.raises(SystemExit) as exit_:
        link(ch, forge, "o/r#5", "--blocked-by", "o/r#4")
    assert exit_.value.code == 2
    assert failure["message"] in capsys.readouterr().err
    assert forge.writes() == ([] if on == "read" else ["addBlockedBy"])
    assert forge.body("o/r#5") == "Body"


def test_a_forbidden_relation_is_a_refusal(ch: ModuleType, forge: Forge, capsys: pytest.CaptureFixture[str]) -> None:
    forge.add("o/r#1")
    forge.add("someone/else#2")
    forge.relation_error = ("write", error("Resource not accessible by integration", "FORBIDDEN"))
    assert link(ch, forge, "o/r#1", "--child", "someone/else#2") == 0
    assert forge.body("o/r#1") == "Depends on someone/else#2"
    assert "text only: Resource not accessible by integration" in capsys.readouterr().out


def test_the_script_runs_from_its_folder_and_refuses_a_malformed_reference_before_gh(tmp_path: Path) -> None:
    proc = subprocess.run(
        [sys.executable, str(SCRIPTS / "chains.py"), "link", "nope", "--blocked-by", "o/r#4"],
        capture_output=True,
        text=True,
        check=False,
        env={"PATH": str(tmp_path)},  # no gh at all: it must not be reached
    )
    assert proc.returncode == 2
    assert "malformed reference 'nope'" in proc.stderr
