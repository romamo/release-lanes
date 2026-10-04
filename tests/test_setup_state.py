"""shipyard-setup's checklist script, without gh or the network"""

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "skills" / "shipyard-setup" / "scripts" / "setup_state.py"
CALLER = "jobs:\n  prepare:\n    uses: romamo/shipyard/.github/workflows/prepare.yml@v0\n"


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
    write(tmp_path, ".github/shipyard.toml", f'name = "demo"\nmode = "{mode}"  # comment\n')
    write(tmp_path, ".github/workflows/release.yml", CALLER)
    assert bot(ss, tmp_path) == state


def test_a_release_workflow_from_another_tool_is_foreign(ss: ModuleType, tmp_path: Path) -> None:
    write(tmp_path, ".github/shipyard.toml", 'mode = "release"\n')
    write(tmp_path, ".github/workflows/release.yml", "jobs:\n  release:\n    uses: someone/else/x.yml@v1\n")
    assert bot(ss, tmp_path) == "RELEASE_FOREIGN"


def test_an_unknown_mode_fails(ss: ModuleType, tmp_path: Path) -> None:
    write(tmp_path, ".github/shipyard.toml", 'mode = "yolo"\n')
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
    assert set(merged["extraKnownMarketplaces"]) == {"mine", "shipyard"}
    assert merged["enabledPlugins"] == {"other@mine": True, "shipyard@shipyard": True}
    assert settings["extraKnownMarketplaces"] == {"mine": {"source": {"source": "github", "repo": "me/mine"}}}


def test_an_existing_shipyard_marketplace_is_kept(ss: ModuleType) -> None:
    fork = {"source": {"source": "github", "repo": "me/shipyard-fork"}}
    merged = ss.enable_plugin({"extraKnownMarketplaces": {"shipyard": fork}})
    assert merged["extraKnownMarketplaces"]["shipyard"] == fork


def test_a_plugin_turned_off_on_purpose_is_reported_not_missing(ss: ModuleType) -> None:
    assert ss.plugin_row({"enabledPlugins": {"shipyard@shipyard": False}}).state == "PLUGIN_DISABLED"


def test_a_missing_settings_file_reads_as_empty(ss: ModuleType, tmp_path: Path) -> None:
    assert ss.load_settings(tmp_path / ".claude" / "settings.json") == {}


@pytest.mark.parametrize("text", ["{not json", "[]", json.dumps({"enabledPlugins": []})])
def test_malformed_settings_fail(ss: ModuleType, tmp_path: Path, text: str) -> None:
    write(tmp_path, ".claude/settings.json", text)
    with pytest.raises(SystemExit) as exc:
        ss.load_settings(tmp_path / ".claude" / "settings.json")
    assert exc.value.code == 2


def test_the_blocker_label_follows_the_policy(ss: ModuleType, tmp_path: Path) -> None:
    assert set(ss.wanted_labels(tmp_path)) == {"postponed", "blocked", "shipyard-hold", "release-blocker"}
    write(tmp_path, ".github/shipyard.toml", '[gates]\nblocker_label = "hold"\n')
    assert set(ss.wanted_labels(tmp_path)) == {"postponed", "blocked", "shipyard-hold", "hold"}


def test_a_local_copy_of_shipyards_workflows_counts(ss: ModuleType, tmp_path: Path) -> None:
    write(tmp_path, ".github/shipyard.toml", 'mode = "release"\n')
    write(tmp_path, ".github/workflows/release.yml", "jobs:\n  p:\n    uses: ./.github/workflows/prepare.yml\n")
    write(tmp_path, ".github/workflows/prepare.yml", "name: other prepare\n")
    assert bot(ss, tmp_path) == "RELEASE_FOREIGN"
    write(tmp_path, ".github/workflows/prepare.yml", "name: shipyard prepare\n")
    assert bot(ss, tmp_path) == "RELEASE_READY"


def test_the_alias_config_counts_and_both_files_fail(ss: ModuleType, tmp_path: Path) -> None:
    write(tmp_path, ".github/release-policy.toml", 'mode = "dry-run"\n')
    write(tmp_path, ".github/workflows/release.yml", CALLER)
    assert bot(ss, tmp_path) == "RELEASE_DRY_RUN"
    write(tmp_path, ".github/shipyard.toml", 'mode = "dry-run"\n')
    with pytest.raises(SystemExit) as exc:
        ss.release_row(tmp_path)
    assert exc.value.code == 2


def test_a_gate_only_config_has_no_release(ss: ModuleType, tmp_path: Path) -> None:
    write(tmp_path, ".github/shipyard.toml", '[agents]\nprompt = "/github-issue-triage {repo}"\n')
    assert bot(ss, tmp_path) == "RELEASE_MISSING"
    assert ss.agents_row(tmp_path).state == "AGENTS_OK"


@pytest.mark.parametrize(
    "text",
    ["", 'mode = "release"\n', "[agents]\nprs = true\n", '[agents]\n[lanes]\nprompt = "x"\n'],
)
def test_agents_without_a_prompt_are_missing(ss: ModuleType, tmp_path: Path, text: str) -> None:
    write(tmp_path, ".github/shipyard.toml", text)
    assert ss.agents_row(tmp_path).state == "AGENTS_MISSING"
