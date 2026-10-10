"""chains.py show: the chain an issue belongs to, its executors and states, and where it
stalls for want of a gate; and github-ship-watch's CHAIN_NO_GATE row from it (spec 017)"""

import base64
import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "github-issue-triage" / "scripts"
WATCH = ROOT / "skills" / "github-ship-watch" / "scripts" / "watch_state.py"
GATED = '[agents]\nprompt = "/github-issue-triage {repo}"\napp_id = 1\n'
UNGATED = "[changelog]\nfragments = true\n"

Key = tuple[str, str, int]


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


@pytest.fixture(scope="module")
def ws() -> ModuleType:
    spec = importlib.util.spec_from_file_location("watch_state_chains", WATCH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses look their module up by name
    spec.loader.exec_module(module)
    return module


def key(ref: str) -> Key:
    repo, _, number = ref.partition("#")
    owner, _, name = repo.partition("/")
    return (owner.lower(), name.lower(), int(number))


class Forge:
    """A fake GitHub behind chains.py's runner and config reader: issues, pull requests, the
    native relations between them, and each repo's config. Every item asked and every config
    read is recorded"""

    def __init__(self, ch: ModuleType) -> None:
        self.ch = ch
        self.items: dict[Key, dict[str, Any]] = {}
        self.configs: dict[str, str | None] = {}
        self.asked: list[list[str]] = []  # the references of each query
        self.read: list[str] = []  # the repos whose config was read
        self.errors: list[dict[str, Any]] = []  # added to every response

    def issue(self, ref: str, state: str = "OPEN", **fields: Any) -> None:
        repo, _, number = ref.partition("#")
        self.items[key(ref)] = {"type": "Issue", "repo": repo, "number": int(number), "state": state, **fields}

    def pull(self, ref: str, state: str = "OPEN") -> None:
        repo, _, number = ref.partition("#")
        self.items[key(ref)] = {"type": "PullRequest", "repo": repo, "number": int(number), "state": state}

    def related(self, ref: str, states: bool = True) -> dict[str, Any]:
        item = self.items[key(ref)]
        node: dict[str, Any] = {"number": item["number"], "repository": {"nameWithOwner": item["repo"]}}
        if states:
            node |= {"state": item["state"], "stateReason": item.get("reason")}
        return node

    def connection(self, refs: list[str], states: bool = True, more: bool = False) -> dict[str, Any]:
        return {"pageInfo": {"hasNextPage": more}, "nodes": [self.related(r, states) for r in refs]}

    def node(self, item: dict[str, Any]) -> dict[str, Any]:
        base = {
            "__typename": item["type"],
            "number": item["number"],
            "title": item.get("title", f"title of {item['repo']}#{item['number']}"),
            "state": item["state"],
            "repository": {"nameWithOwner": item["repo"]},
        }
        if item["type"] == "PullRequest":
            return base
        more = item.get("more", "")
        return base | {
            "stateReason": item.get("reason"),
            "body": item.get("body", ""),
            "labels": {
                "pageInfo": {"hasNextPage": more == "labels"},
                "nodes": [{"name": n} for n in item.get("labels", ())],
            },
            "assignees": {
                "pageInfo": {"hasNextPage": False},
                "nodes": [{"login": n} for n in item.get("assignees", ())],
            },
            "blockedBy": self.connection(item.get("blocked_by", []), more=more == "blockedBy"),
            "subIssues": self.connection(item.get("children", [])),
            "blocking": self.connection(item.get("blocking", []), states=False),
            "parent": self.related(item["parent"], states=False) if item.get("parent") else None,
        }

    def __call__(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        count = 0
        while f"o{count}" in variables:
            count += 1
        assert query == self.ch.items_query(count)
        refs = [f"{variables[f'o{i}']}/{variables[f'n{i}']}#{variables[f'k{i}']}" for i in range(count)]
        self.asked.append(refs)
        data: dict[str, Any] = {}
        errors = list(self.errors)
        for i, ref in enumerate(refs):
            item = self.items.get(key(ref))
            if item is None:
                data[f"i{i}"] = None
                errors.append({"type": "NOT_FOUND", "path": [f"i{i}"], "message": "Could not resolve to a Repository"})
            else:
                data[f"i{i}"] = {"issueOrPullRequest": self.node(item)}
        return {"data": data, **({"errors": errors} if errors else {})}

    def config(self, repo: str) -> str | None:
        self.read.append(repo)
        return self.configs[repo.lower()]


@pytest.fixture
def forge(ch: ModuleType) -> Forge:
    return Forge(ch)


def show(ch: ModuleType, forge: Forge, *argv: str) -> int:
    result: int = ch.main(["show", *argv], run=forge, contents=forge.config)
    return result


def shown(ch: ModuleType, forge: Forge, capsys: pytest.CaptureFixture[str], *refs: str) -> tuple[int, list[Any]]:
    code = show(ch, forge, *refs, "--json")
    rows: list[Any] = json.loads(capsys.readouterr().out)["rows"]
    return code, rows


def cells(rows: list[Any]) -> list[tuple[Any, ...]]:
    return [(r["ref"], r["executor"], r["state"], r["holds"], r["gate"]) for r in rows]


def test_s017_18_show_lists_every_item_it_waits_on_and_that_waits_on_it_each_once(
    ch: ModuleType, forge: Forge, capsys: pytest.CaptureFixture[str]
) -> None:
    # upstream: o/a#1 blocks o/b#2 natively, and names it back on a Depends on line (a cycle)
    forge.issue("o/a#1", state="CLOSED", reason="COMPLETED", body="Depends on o/b#2")
    forge.issue("o/b#2", blocked_by=["o/a#1"], blocking=["o/a#3"])
    # downstream: a person's item waits on o/b#2 both ways, o/c#4 waits on it, and o/c#4's parent
    forge.issue("o/a#3", labels=["human"], assignees=["alice", "bob"], body="Depends on O/B#2", blocking=["o/c#4"])
    forge.issue("o/c#4", blocked_by=["o/a#3"], parent="o/c#5")
    forge.issue("o/c#5", children=["o/c#4"])
    forge.issue("o/c#6", body="Depends on o/a#9")  # in no chain of o/b#2's
    forge.configs = {"o/b": GATED}
    code, rows = shown(ch, forge, capsys, "o/b#2")
    assert cells(rows) == [
        ("o/b#2", "agent", "READY", [], None),
        ("o/a#1", "agent", "CLOSED", [], None),
        ("o/a#3", "person: @alice,@bob", "BLOCKED", ["o/b#2"], None),
        ("o/c#4", "agent", "BLOCKED", ["o/a#3"], None),
        ("o/c#5", "agent", "BLOCKED", ["o/c#4"], None),
    ]
    assert code == 0
    asked = [ref.lower() for refs in forge.asked for ref in refs]
    assert sorted(asked) == sorted(set(asked)), "an item was read twice"
    assert forge.read == ["o/b"]  # the READY item's repo only
    assert rows[2]["waits_on"] == ["o/b#2"] and rows[2]["waited_on_by"] == ["o/c#4"]


def test_s017_18_a_person_item_without_assignees_and_a_pull_request_it_names(
    ch: ModuleType, forge: Forge, capsys: pytest.CaptureFixture[str]
) -> None:
    forge.issue("o/r#1", labels=["human"], body="Depends on #2\nDepends on o/r#3")
    forge.pull("o/r#2")
    forge.issue("o/r#3", state="CLOSED", reason="NOT_PLANNED")
    code, rows = shown(ch, forge, capsys, "o/r#1")
    assert cells(rows) == [
        ("o/r#1", "person: none", "BLOCKED", ["o/r#2"], None),
        ("o/r#2", "pull request", "OPEN", [], None),
        ("o/r#3", "agent", "CLOSED", [], None),
    ]
    assert code == 0 and forge.read == []


def test_s017_19_a_ready_item_in_a_repo_without_a_gate_is_no_gate_and_exits_1(
    ch: ModuleType, forge: Forge, capsys: pytest.CaptureFixture[str]
) -> None:
    forge.issue("o/r#9", body="Depends on x/up#1\nDepends on x/none#2\n- Depends on x/none#3")
    forge.issue("x/up#1")  # its repo's config has no [agents] table
    forge.issue("x/none#2")  # its repo has no config at all
    forge.issue("x/none#3")
    forge.configs = {"o/r": GATED, "x/up": UNGATED, "x/none": None}
    code, rows = shown(ch, forge, capsys, "o/r#9")
    assert cells(rows) == [  # a body's holds in triage_state.py's order: sorted
        ("o/r#9", "agent", "BLOCKED", ["x/none#2", "x/none#3", "x/up#1"], None),
        ("x/none#2", "agent", "READY", [], "NO_GATE"),
        ("x/none#3", "agent", "READY", [], "NO_GATE"),
        ("x/up#1", "agent", "READY", [], "NO_GATE"),
    ]
    assert code == 1
    assert forge.read == ["x/none", "x/up"]  # once per repo, and none for a BLOCKED item's
    # the table prints the same rows, NO_GATE in the last column
    forge.read.clear()
    assert show(ch, forge, "o/r#9") == 1
    lines = capsys.readouterr().out.splitlines()
    assert lines == [ch.show_line(row) for row in rows]
    assert lines[3] == "x/up#1  title of x/up#1  agent  READY  NO_GATE"
    assert lines[0] == "o/r#9  title of o/r#9  agent  BLOCKED (x/none#2 x/none#3 x/up#1)"


def test_s017_19_a_ready_item_in_a_repo_with_a_gate_exits_0(
    ch: ModuleType, forge: Forge, capsys: pytest.CaptureFixture[str]
) -> None:
    forge.issue("o/r#1", body="Depends on x/b#2")
    forge.issue("x/b#2")
    forge.configs = {"x/b": "[agents]\n"}  # an empty [agents] table still is one
    code, rows = shown(ch, forge, capsys, "o/r#1")
    assert [r["gate"] for r in rows] == [None, None] and code == 0


def test_a_config_the_reader_refuses_fails_rather_than_reads_as_no_gate(
    ch: ModuleType, forge: Forge, capsys: pytest.CaptureFixture[str]
) -> None:
    forge.issue("o/r#1")
    forge.configs = {"o/r": "[agents\n"}
    with pytest.raises(SystemExit) as failed:
        show(ch, forge, "o/r#1")
    assert failed.value.code == 2
    assert "o/r:.github/shipmill.toml" in capsys.readouterr().err


def proc(code: int, out: str = "", err: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess([], code, out, err)


def test_the_config_is_read_through_the_contents_api_and_only_a_404_means_none(ch: ModuleType) -> None:
    asked: list[list[str]] = []

    def answer(result: subprocess.CompletedProcess[str]) -> Any:
        def execute(cmd: list[str]) -> subprocess.CompletedProcess[str]:
            asked.append(cmd)
            return result

        return execute

    encoded = base64.b64encode(GATED.encode()).decode()
    file = json.dumps({"type": "file", "encoding": "base64", "content": encoded[:10] + "\n" + encoded[10:]})
    assert ch.gh_contents("x/b", answer(proc(0, file))) == GATED
    assert asked == [["gh", "api", "repos/x/b/contents/.github/shipmill.toml"]]
    assert ch.gh_contents("x/b", answer(proc(1, err="gh: Not Found (HTTP 404)"))) is None
    for failure in (
        proc(1, err="gh: Forbidden (HTTP 403)"),
        proc(1, err="gh: Server Error (HTTP 502)"),
        proc(1, err="error connecting to api.github.com"),
        proc(0, json.dumps({"type": "file", "encoding": "none", "content": ""})),  # over 1 MB
        proc(0, "not json"),
    ):
        with pytest.raises(SystemExit) as failed:
            ch.gh_contents("x/b", answer(failure))
        assert failed.value.code == 2


def test_an_item_it_cannot_read_is_one_row_and_the_chain_goes_on(
    ch: ModuleType, forge: Forge, capsys: pytest.CaptureFixture[str]
) -> None:
    forge.issue("o/r#1", body="Depends on x/secret#4\nDepends on o/r#2")
    forge.issue("o/r#2", state="CLOSED", body="Depends on o/r#5")
    forge.issue("o/r#5", state="CLOSED")
    code, rows = shown(ch, forge, capsys, "o/r#1")
    assert [(r["ref"], r["state"]) for r in rows] == [
        ("o/r#1", "BLOCKED"),  # a hold it can't read may still be open
        ("o/r#2", "CLOSED"),
        ("x/secret#4", "UNREADABLE"),
        ("o/r#5", "CLOSED"),
    ]
    assert rows[0]["holds"] == ["x/secret#4"] and code == 0
    assert ch.show_line(rows[2]) == "x/secret#4 UNREADABLE"


@pytest.mark.parametrize(
    ("setup", "ref", "message"),
    [
        (lambda f: None, "o/r#1", "can't read o/r#1"),
        (lambda f: f.pull("o/r#1"), "o/r#1", "o/r#1 is a pull request"),
        (lambda f: None, "o/r#0", "malformed reference"),
        (lambda f: None, "o/r", "malformed reference"),
        (lambda f: f.issue("o/r#1", more="blockedBy"), "o/r#1", "more than 100 blockedBy"),
        (lambda f: f.issue("o/r#1", more="labels"), "o/r#1", "more than 100 labels"),
    ],
)
def test_show_exits_2_naming_the_reference(
    ch: ModuleType, forge: Forge, capsys: pytest.CaptureFixture[str], setup: Any, ref: str, message: str
) -> None:
    setup(forge)
    with pytest.raises(SystemExit) as failed:
        show(ch, forge, ref)
    assert failed.value.code == 2
    assert message in capsys.readouterr().err


def test_any_other_graphql_error_fails_the_run(
    ch: ModuleType, forge: Forge, capsys: pytest.CaptureFixture[str]
) -> None:
    forge.issue("o/r#1")
    forge.errors = [{"type": "INTERNAL", "path": ["i0"], "message": "Something went wrong"}]
    with pytest.raises(SystemExit) as failed:
        show(ch, forge, "o/r#1")
    assert failed.value.code == 2 and "Something went wrong" in capsys.readouterr().err
    # a relation the token can't read is no unreadable item: falling back would hide a hold
    forge.errors = [{"type": "FORBIDDEN", "path": ["i0", "issueOrPullRequest", "blockedBy"], "message": "no"}]
    with pytest.raises(SystemExit):
        show(ch, forge, "o/r#1")


def test_several_starts_share_their_reads_a_query_holding_twenty(
    ch: ModuleType, forge: Forge, capsys: pytest.CaptureFixture[str]
) -> None:
    for n in range(1, 23):
        forge.issue(f"o/r#{n}", body="Depends on x/b#99")
    forge.issue("x/b#99")
    forge.configs = {"x/b": None}
    code, rows = shown(ch, forge, capsys, *(f"o/r#{n}" for n in range(1, 23)))
    assert [len(refs) for refs in forge.asked] == [20, 2, 1]
    assert [r["ref"] for r in rows if r["gate"]] == ["x/b#99"] and code == 1
    assert forge.read == ["x/b"]


# proves: S-017-20
def test_ship_watch_reports_chain_no_gate_from_what_chains_show_prints(
    ch: ModuleType, ws: ModuleType, forge: Forge, capsys: pytest.CaptureFixture[str]
) -> None:
    forge.issue("o/r#3", body="Depends on x/b#2")
    forge.issue("x/b#2")
    forge.issue("o/r#5")  # ready, linked to nothing: in no chain
    forge.issue("o/r#12", blocked_by=["o/r#3"])
    forge.configs = {"o/r": None, "x/b": None}
    triage = "\n".join(
        json.dumps({"number": n, "state": s, "title": "t", "note": ""})
        for n, s in [(3, "BLOCKED"), (5, "NEW"), (12, "BLOCKED"), (7, "UNTRUSTED"), (40, "SUSPECT_CLOSE")]
    )
    ran: list[list[str]] = []

    def execute(cmd: list[str]) -> subprocess.CompletedProcess[str]:
        ran.append(cmd)
        code = ch.main(cmd[2:], run=forge, contents=forge.config)
        return proc(code, capsys.readouterr().out)

    rows = ws.chain_rows("o/r", triage, execute)
    # every open issue but the UNTRUSTED one starts a chain, in one run
    assert ran == [[sys.executable, str(ws.CHAINS), "show", "--json", "o/r#3", "o/r#5", "o/r#12"]]
    assert [(r.state, r.subject) for r in rows] == [("CHAIN_NO_GATE", "x/b#2")]
    assert "x/b" in rows[0].detail
    assert rows[0].fix == "set up a gate on x/b (/shipmill:shipmill-setup x/b), or do x/b#2 by hand"
    assert rows[0].json()["agent"] is False
    assert "CHAIN_NO_GATE" in ws.ACTION and "CHAIN_NO_GATE" not in ws.AGENT


def test_s017_20_chain_no_gate_rows_fail_fast_and_skip_with_no_open_issue(ws: ModuleType) -> None:
    def never(cmd: list[str]) -> subprocess.CompletedProcess[str]:
        raise AssertionError("chains.py ran with no open issue")

    closed = json.dumps({"number": 4, "state": "SUSPECT_CLOSE", "title": "t", "note": ""})
    assert ws.chain_rows("o/r", closed, never) == []
    with pytest.raises(ws.Refused, match="chains.py show: error: gh: boom"):
        ws.chain_rows("o/r", '{"number": 1, "state": "NEW"}', lambda cmd: proc(2, err="error: gh: boom\n"))
    bad = {"rows": [{"ref": "x/b#2 or; rm -rf", "gate": "NO_GATE", "waits_on": ["o/r#1"], "waited_on_by": []}]}
    with pytest.raises(ws.Refused, match="malformed reference"):
        ws.chain_no_gate_rows(bad)
    with pytest.raises(ws.Refused):
        ws.chain_no_gate_rows([])
