"""shipmill-setup's checklist script, without gh or the network"""

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "skills" / "shipmill-setup" / "scripts" / "setup_state.py"
CALLER = "jobs:\n  prepare:\n    uses: shipmill/shipmill/.github/workflows/prepare.yml@v0\n"


@pytest.fixture(scope="module")
def ss() -> ModuleType:
    spec = importlib.util.spec_from_file_location("setup_state", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses look their module up by name
    spec.loader.exec_module(module)
    return module


def write(root: Path, path: str, text: str) -> None:
    (root / path).parent.mkdir(parents=True, exist_ok=True)
    (root / path).write_text(text, encoding="utf-8")


def bot(ss: ModuleType, root: Path) -> str:
    state: str = ss.release_row(root).state
    return state


def test_a_repo_without_a_config_needs_releases_set_up(ss: ModuleType, tmp_path: Path) -> None:
    assert bot(ss, tmp_path) == "RELEASE_MISSING"


@pytest.mark.parametrize(
    ("mode", "state"), [("release", "RELEASE_READY"), ("dry-run", "RELEASE_DRY_RUN"), ("off", "RELEASE_OFF")]
)
def test_the_config_mode_sets_the_release_state(ss: ModuleType, tmp_path: Path, mode: str, state: str) -> None:
    write(tmp_path, ".github/shipmill.toml", f'name = "demo"\nmode = "{mode}"  # comment\n')
    write(tmp_path, ".github/workflows/release.yml", CALLER)
    assert bot(ss, tmp_path) == state


def test_a_release_workflow_from_shipmills_old_path_counts(ss: ModuleType, tmp_path: Path) -> None:
    # a repo set up before shipmill moved from romamo/shipmill to the shipmill org
    write(tmp_path, ".github/shipmill.toml", 'mode = "release"\n')
    write(tmp_path, ".github/workflows/release.yml", CALLER.replace("shipmill/shipmill/", "romamo/shipmill/"))
    assert bot(ss, tmp_path) == "RELEASE_READY"


def test_a_release_workflow_from_another_tool_is_foreign(ss: ModuleType, tmp_path: Path) -> None:
    write(tmp_path, ".github/shipmill.toml", 'mode = "release"\n')
    write(tmp_path, ".github/workflows/release.yml", "jobs:\n  release:\n    uses: someone/else/x.yml@v1\n")
    assert bot(ss, tmp_path) == "RELEASE_FOREIGN"


def test_an_unknown_mode_fails(ss: ModuleType, tmp_path: Path) -> None:
    write(tmp_path, ".github/shipmill.toml", 'mode = "yolo"\n')
    write(tmp_path, ".github/workflows/release.yml", CALLER)
    with pytest.raises(SystemExit) as exc:
        ss.release_row(tmp_path)
    assert exc.value.code == 2


def test_enabling_the_plugin_keeps_other_settings(ss: ModuleType) -> None:
    settings = {
        "permissions": {"allow": ["Bash(uv run:*)"]},
        "extraKnownMarketplaces": {"mine": {"source": {"source": "github", "repo": "me/mine"}}},
        "enabledPlugins": {"other@mine": True},
    }
    assert ss.plugin_row(settings).state == "PLUGIN_MISSING"
    merged = ss.enable_plugin(settings)
    assert ss.plugin_row(merged).state == "PLUGIN_OK"
    assert merged["permissions"] == settings["permissions"]
    assert set(merged["extraKnownMarketplaces"]) == {"mine", "shipmill"}
    assert merged["enabledPlugins"] == {"other@mine": True, "shipmill@shipmill": True}
    assert settings["extraKnownMarketplaces"] == {"mine": {"source": {"source": "github", "repo": "me/mine"}}}


def test_an_existing_shipmill_marketplace_is_kept(ss: ModuleType) -> None:
    fork = {"source": {"source": "github", "repo": "me/shipmill-fork"}}
    merged = ss.enable_plugin({"extraKnownMarketplaces": {"shipmill": fork}})
    assert merged["extraKnownMarketplaces"]["shipmill"] == fork


def test_a_plugin_turned_off_on_purpose_is_reported_not_missing(ss: ModuleType) -> None:
    assert ss.plugin_row({"enabledPlugins": {"shipmill@shipmill": False}}).state == "PLUGIN_DISABLED"


def test_a_missing_settings_file_reads_as_empty(ss: ModuleType, tmp_path: Path) -> None:
    assert ss.load_settings(tmp_path / ".claude" / "settings.json") == {}


@pytest.mark.parametrize("text", ["{not json", "[]", json.dumps({"enabledPlugins": []})])
def test_malformed_settings_fail(ss: ModuleType, tmp_path: Path, text: str) -> None:
    write(tmp_path, ".claude/settings.json", text)
    with pytest.raises(SystemExit) as exc:
        ss.load_settings(tmp_path / ".claude" / "settings.json")
    assert exc.value.code == 2


def test_the_blocker_label_follows_the_policy(ss: ModuleType, tmp_path: Path) -> None:
    assert set(ss.wanted_labels(tmp_path)) == {"postponed", "blocked", "shipmill-hold", "release-blocker"}
    write(tmp_path, ".github/shipmill.toml", '[gates]\nblocker_label = "hold"\n')
    assert set(ss.wanted_labels(tmp_path)) == {"postponed", "blocked", "shipmill-hold", "hold"}


def test_a_local_copy_of_shipmills_workflows_counts(ss: ModuleType, tmp_path: Path) -> None:
    write(tmp_path, ".github/shipmill.toml", 'mode = "release"\n')
    write(tmp_path, ".github/workflows/release.yml", "jobs:\n  p:\n    uses: ./.github/workflows/prepare.yml\n")
    write(tmp_path, ".github/workflows/prepare.yml", "name: other prepare\n")
    assert bot(ss, tmp_path) == "RELEASE_FOREIGN"
    write(tmp_path, ".github/workflows/prepare.yml", "name: shipmill prepare\n")
    assert bot(ss, tmp_path) == "RELEASE_READY"


def test_a_gate_only_config_has_no_release(ss: ModuleType, tmp_path: Path) -> None:
    write(tmp_path, ".github/shipmill.toml", '[agents]\nprompt = "/github-issue-triage {repo}"\n')
    assert bot(ss, tmp_path) == "RELEASE_MISSING"
    assert ss.agents_row(tmp_path).state == "AGENTS_OK"


@pytest.mark.parametrize(
    "text",
    ["", 'mode = "release"\n', "[agents]\nprs = true\n", '[agents]\n[lanes]\nprompt = "x"\n'],
)
def test_agents_without_a_prompt_are_missing(ss: ModuleType, tmp_path: Path, text: str) -> None:
    write(tmp_path, ".github/shipmill.toml", text)
    assert ss.agents_row(tmp_path).state == "AGENTS_MISSING"


class FakeGh:
    """Records each gh call and answers the repo-setting read and the PATCH with canned output"""

    def __init__(self, read: str, patched: str = "true\n") -> None:
        self.read = read
        self.patched = patched
        self.calls: list[list[str]] = []

    def __call__(self, cmd: list[str]) -> str:
        self.calls.append(cmd)
        return self.patched if "PATCH" in cmd else self.read


READ = ["gh", "api", "repos/me/demo", "-q", ".delete_branch_on_merge"]
PATCH = [
    "gh",
    "api",
    "-X",
    "PATCH",
    "repos/me/demo",
    "-F",
    "delete_branch_on_merge=true",
    "-q",
    ".delete_branch_on_merge",
]


def test_branch_delete_on_counts_as_done(ss: ModuleType) -> None:
    gh = FakeGh("true\n")
    row = ss.branch_delete_row("me/demo", False, gh)
    assert row.state == "BRANCH_DELETE_ON"
    assert row.state in ss.DONE
    assert gh.calls == [READ]


def test_branch_delete_off_without_fix_is_reported_and_never_patched(ss: ModuleType) -> None:
    gh = FakeGh("false\n")
    row = ss.branch_delete_row("me/demo", False, gh)
    assert row.state == "BRANCH_DELETE_OFF"
    assert row.state not in ss.DONE
    assert gh.calls == [READ]


def test_fix_turns_branch_delete_on(ss: ModuleType) -> None:
    gh = FakeGh("false\n")
    assert ss.branch_delete_row("me/demo", True, gh).state == "BRANCH_DELETE_ON"
    assert gh.calls == [READ, PATCH]


def test_fix_leaves_branch_delete_alone_when_already_on(ss: ModuleType) -> None:
    gh = FakeGh("true\n")
    assert ss.branch_delete_row("me/demo", True, gh).state == "BRANCH_DELETE_ON"
    assert gh.calls == [READ]


@pytest.mark.parametrize(("read", "patched"), [("", "true"), ("null\n", "true"), ("false", "false")])
def test_an_unreadable_or_unchanged_setting_fails_rather_than_reading_as_off(
    ss: ModuleType, read: str, patched: str
) -> None:
    with pytest.raises(SystemExit) as exc:
        ss.branch_delete_row("me/demo", True, FakeGh(read, patched))
    assert exc.value.code == 2


def test_a_gh_api_failure_fails_loudly(ss: ModuleType) -> None:
    def broken(cmd: list[str]) -> str:
        return str(ss.run(["false"]))  # a nonzero exit, the way gh ends on a 403 or a network error

    with pytest.raises(SystemExit) as exc:
        ss.branch_delete_row("me/demo", False, broken)
    assert exc.value.code == 2
