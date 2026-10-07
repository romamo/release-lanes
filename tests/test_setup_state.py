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


ENABLED = {"enabledPlugins": {"shipmill@shipmill": True}}


def test_an_outdated_install_is_one_row_each_and_not_done(ss: ModuleType) -> None:
    # #233: setup called a repo with a 0.31.1 project install healthy while 0.32.1 was out
    found = [
        "plugin project: 0.31.1, latest v0.32.1; in /w/r: claude plugin uninstall ... && claude plugin install ...",
        "plugin project: 0.25.0, latest v0.32.1; in /w/r/tmp/shipmill-gate: claude plugin update ...",
    ]
    rows = ss.plugin_rows(ENABLED, lambda: found)
    assert [(r.state, r.detail) for r in rows] == [("PLUGIN_OUTDATED", d) for d in found]
    assert "PLUGIN_OUTDATED" not in ss.DONE


def test_current_installs_read_as_ok(ss: ModuleType) -> None:
    assert [r.state for r in ss.plugin_rows(ENABLED, lambda: [])] == ["PLUGIN_OK"]


@pytest.mark.parametrize("settings", [{}, {"enabledPlugins": {"shipmill@shipmill": False}}])
def test_installs_are_read_only_with_the_plugin_enabled(ss: ModuleType, settings: dict[str, object]) -> None:
    def unread() -> list[str]:
        raise AssertionError("the installs should not be read")

    assert [r.state for r in ss.plugin_rows(settings, unread)] in (["PLUGIN_MISSING"], ["PLUGIN_DISABLED"])


def test_the_installs_are_read_through_watch_state(ss: ModuleType) -> None:
    assert ss.WATCH_STATE.is_file()
    assert callable(ss.watch_module().shipmill_rows)


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
    agents = '[agents]\nprompt = "/github-issue-triage {repo}"\napp_id = 1\nmode = "interactive"\n'
    write(tmp_path, ".github/shipmill.toml", agents)
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


HEADLESS = '[agents]\nprompt = "/github-issue-triage {repo}"\nmode = "headless"\n'
NO_APP = (
    "no app connected: sessions write as the host's gh login, so you can't approve their PRs"
    " and GitHub won't notify you of their mentions; run shipmill-setup's step 3 (app-create)"
)
BASE_LABELS = {"postponed", "blocked", "shipmill-hold", "release-blocker"}


def test_s005_16_headless_wants_the_needs_decision_label(ss: ModuleType, tmp_path: Path) -> None:
    write(tmp_path, ".github/shipmill.toml", HEADLESS)
    wanted = ss.wanted_labels(tmp_path)
    assert set(wanted) == BASE_LABELS | {"needs-decision"}
    assert wanted["needs-decision"] == ("d876e3", "A shipmill session asked a question here; waits for a reply")
    row = ss.labels_row(["needs-decision"], wanted)
    assert (row.state, row.detail) == ("LABELS_MISSING", "needs-decision")
    assert ss.labels_row([], wanted).detail == (
        "postponed, blocked, shipmill-hold, needs-decision, and the blocker label exist"
    )


@pytest.mark.parametrize(
    "text",
    [
        '[agents]\nprompt = "x"\n',
        '[agents]\nprompt = "x"\nmode = "interactive"\n',
        '[agents]\nprompt = "x"\napp_id = 1\n',
    ],
)
def test_d21_interactive_wants_the_needs_decision_label_too(ss: ModuleType, tmp_path: Path, text: str) -> None:
    # #206, D-21: an interactive gate session posts its questions as needs-decision too
    write(tmp_path, ".github/shipmill.toml", text)
    wanted = ss.wanted_labels(tmp_path)
    assert set(wanted) == BASE_LABELS | {"needs-decision"}
    assert ss.labels_row([], wanted).detail == (
        "postponed, blocked, shipmill-hold, needs-decision, and the blocker label exist"
    )


@pytest.mark.parametrize("text", ["", 'mode = "release"\n', '[lanes.dev]\nmode = "headless"\n'])
def test_s005_16_no_agents_doesnt_want_the_label(ss: ModuleType, tmp_path: Path, text: str) -> None:
    write(tmp_path, ".github/shipmill.toml", text)
    wanted = ss.wanted_labels(tmp_path)
    assert set(wanted) == BASE_LABELS
    assert ss.labels_row([], wanted).detail == "postponed, blocked, shipmill-hold, and the blocker label exist"
    assert ss.agents_row(tmp_path).state == "AGENTS_MISSING"


def test_s005_16_a_headless_mode_outside_agents_doesnt_count(ss: ModuleType, tmp_path: Path) -> None:
    write(tmp_path, ".github/shipmill.toml", '[agents]\nprompt = "x"\napp_id = 1\n[lanes.dev]\nmode = "headless"\n')
    assert set(ss.wanted_labels(tmp_path)) == BASE_LABELS | {"needs-decision"}  # the [agents] section, D-21
    assert ss.agents_row(tmp_path).state == "AGENTS_NO_MODE"  # another table's mode isn't [agents]'s


def test_s005_16_fix_creates_the_label_only_when_wanted(ss: ModuleType, tmp_path: Path) -> None:
    gh = FakeGh("")
    write(tmp_path, ".github/shipmill.toml", HEADLESS)
    ss.create_labels("me/demo", ss.wanted_labels(tmp_path), ["needs-decision"], gh)
    description = "A shipmill session asked a question here; waits for a reply"
    create = ["gh", "label", "create", "needs-decision", "-R", "me/demo", "--color", "d876e3"]
    assert gh.calls == [[*create, "--description", description]]

    write(tmp_path, ".github/shipmill.toml", 'mode = "release"\n')
    wanted = ss.wanted_labels(tmp_path)
    gh.calls.clear()
    ss.create_labels("me/demo", wanted, [name for name in wanted if name == "release-blocker"], gh)
    assert [call[3] for call in gh.calls] == ["release-blocker"]


def test_s005_16_headless_without_app_id_reads_agents_no_app(ss: ModuleType, tmp_path: Path) -> None:
    # D-19 supersedes S-005-16's "leaves the exit code as AGENTS_OK would": it is an action now
    write(tmp_path, ".github/shipmill.toml", HEADLESS)
    row = ss.agents_row(tmp_path)
    assert (row.state, row.detail) == ("AGENTS_NO_APP", NO_APP)
    assert "AGENTS_NO_APP" not in ss.DONE and "AGENTS_OK" in ss.DONE
    write(tmp_path, ".github/shipmill.toml", HEADLESS + "app_id = 123   # the App\n")
    assert ss.agents_row(tmp_path).state == "AGENTS_OK"
    write(tmp_path, ".github/shipmill.toml", HEADLESS + "# app_id = 123\n")
    assert ss.agents_row(tmp_path).state == "AGENTS_NO_APP"


def test_s005_16_the_agents_mode_never_reads_as_the_release_mode(ss: ModuleType, tmp_path: Path) -> None:
    write(tmp_path, ".github/shipmill.toml", HEADLESS)
    assert bot(ss, tmp_path) == "RELEASE_MISSING"
    write(tmp_path, ".github/shipmill.toml", 'mode = "release"\n' + HEADLESS)
    write(tmp_path, ".github/workflows/release.yml", CALLER)
    assert bot(ss, tmp_path) == "RELEASE_READY"


@pytest.mark.parametrize("mode", ["", 'mode = "interactive"\n', 'mode = "headless"\n'])
def test_a_gate_without_an_app_is_unfinished_in_either_mode(ss: ModuleType, tmp_path: Path, mode: str) -> None:
    # #204, D-19: an interactive gate with no app_id read AGENTS_OK and went live posting as the maintainer
    agents = '[agents]\nprompt = "/github-issue-triage {repo}"\n' + mode
    write(tmp_path, ".github/shipmill.toml", agents)
    row = ss.agents_row(tmp_path)
    assert (row.state, row.detail) == ("AGENTS_NO_APP", NO_APP)
    assert row.state not in ss.DONE  # so setup_state.py exits 1 on it
    write(tmp_path, ".github/shipmill.toml", agents + "app_id = 7\n")
    assert ss.agents_row(tmp_path).state == ("AGENTS_OK" if mode else "AGENTS_NO_MODE")


NO_MODE = (
    'no mode in [agents]: the gate runs interactive by default, which nobody chose; set mode = "interactive"'
    ' or mode = "headless" (shipmill-setup\'s step 4)'
)


@pytest.mark.parametrize("mode", ['mode = "interactive"\n', 'mode = "headless"\n', "mode = 'headless'  # unattended\n"])
def test_a_gate_with_an_explicit_mode_is_done(ss: ModuleType, tmp_path: Path, mode: str) -> None:
    write(tmp_path, ".github/shipmill.toml", '[agents]\nprompt = "x"\napp_id = 7\n' + mode)
    assert ss.agents_row(tmp_path).state == "AGENTS_OK"


@pytest.mark.parametrize("extra", ["", '# mode = "headless"\n', '[operate]\nmode = "interactive"\n'])
def test_a_gate_without_a_mode_key_is_unfinished(ss: ModuleType, tmp_path: Path, extra: str) -> None:
    # #205, D-20: setup skipped the mode question and an unattended gate ran interactive
    write(tmp_path, ".github/shipmill.toml", '[agents]\nprompt = "x"\napp_id = 7\n' + extra)
    row = ss.agents_row(tmp_path)
    assert (row.state, row.detail) == ("AGENTS_NO_MODE", NO_MODE)
    assert row.state not in ss.DONE  # so setup_state.py exits 1 on it


def test_a_missing_app_reads_before_a_missing_mode(ss: ModuleType, tmp_path: Path) -> None:
    # one row for the section: AGENTS_NO_APP first, then AGENTS_NO_MODE once app_id is set
    write(tmp_path, ".github/shipmill.toml", '[agents]\nprompt = "x"\n')
    assert ss.agents_row(tmp_path).state == "AGENTS_NO_APP"


def test_shipmills_own_config_passes_its_agents_check(ss: ModuleType) -> None:
    # #205: the repo runs its own gate interactive, and says so
    assert ss.agents_row(SCRIPT.parents[3]).state == "AGENTS_OK"


def test_an_indented_app_id_counts(ss: ModuleType, tmp_path: Path) -> None:
    # TOML allows an indented key, and the gate and watch_state.py read it: the checklist must too
    write(tmp_path, ".github/shipmill.toml", '[agents]\nprompt = "x"\n  app_id = 1\nmode = "interactive"\n')
    assert ss.agents_row(tmp_path).state == "AGENTS_OK"


def test_an_indented_mode_counts(ss: ModuleType, tmp_path: Path) -> None:
    # TOML allows an indented key, as for app_id: the gate reads it, so the checklist must too
    write(tmp_path, ".github/shipmill.toml", '[agents]\nprompt = "x"\napp_id = 1\n  mode = "headless"\n')
    assert ss.agents_row(tmp_path).state == "AGENTS_OK"


GATE = '[agents]\nprompt = "x"\napp_id = 1\nmode = "headless"\n'
PR_LIST = ["gh", "pr", "list", "-R", "me/demo", "--state", "open", "--json", "number,isDraft", "--limit", "500"]


def pulls(*found: tuple[int, bool]) -> str:
    return json.dumps([{"number": n, "isDraft": draft} for n, draft in found])


def test_landing_off_with_open_prs_is_an_action(ss: ModuleType, tmp_path: Path) -> None:
    # #234: prs = false and 11 PRs waiting, and setup called the factory healthy
    write(tmp_path, ".github/shipmill.toml", GATE + "prs = false  # the gate opens PRs only\n")
    gh = FakeGh(pulls((210, False), (233, False), (220, True), (231, False)))
    row = ss.landing_row("me/demo", tmp_path, gh)
    assert row.state == "LANDING_OFF"
    assert row.detail == (
        "#233 #231 #210 wait to land: the gate doesn't land pull requests ([agents] prs = false);"
        " set [agents] prs = true with a prompt that merges when green, or land them by hand"
    )
    assert row.state not in ss.DONE  # so setup_state.py exits 1 on it
    assert gh.calls == [PR_LIST]


def test_landing_without_a_prs_key_defaults_off_like_the_config_loader(ss: ModuleType, tmp_path: Path) -> None:
    write(tmp_path, ".github/shipmill.toml", GATE)
    row = ss.landing_row("me/demo", tmp_path, FakeGh(pulls((5, False))))
    assert row.state == "LANDING_OFF"
    assert row.detail.startswith("#5 wait to land: the gate doesn't land pull requests ([agents] no prs key, so prs")


@pytest.mark.parametrize("text", [GATE + "prs = true\n", GATE + "  prs=true # lands\n", 'mode = "release"\n', ""])
def test_landing_on_or_no_gate_is_ok_without_reading_prs(ss: ModuleType, tmp_path: Path, text: str) -> None:
    write(tmp_path, ".github/shipmill.toml", text)
    gh = FakeGh(pulls((5, False)))
    row = ss.landing_row("me/demo", tmp_path, gh)
    assert row.state == "LANDING_OK"
    assert row.state in ss.DONE
    assert gh.calls == []


@pytest.mark.parametrize("found", [pulls(), pulls((7, True), (6, True))])
def test_landing_off_with_only_drafts_or_nothing_open_is_ok(ss: ModuleType, tmp_path: Path, found: str) -> None:
    write(tmp_path, ".github/shipmill.toml", GATE + "prs = false\n")
    gh = FakeGh(found)
    assert ss.landing_row("me/demo", tmp_path, gh).state == "LANDING_OK"
    assert gh.calls == [PR_LIST]


@pytest.mark.parametrize("value", ['"true"', "yes", "1", "False", "", "true false"])
def test_a_prs_value_that_isnt_a_toml_boolean_fails(ss: ModuleType, tmp_path: Path, value: str) -> None:
    write(tmp_path, ".github/shipmill.toml", GATE + f"prs = {value}\n")
    with pytest.raises(SystemExit) as exc:
        ss.landing_row("me/demo", tmp_path, FakeGh(pulls()))
    assert exc.value.code == 2


@pytest.mark.parametrize(
    "out", ["", "{}", json.dumps([{"number": "5", "isDraft": False}]), json.dumps([{"number": 5}])]
)
def test_an_unreadable_pr_list_fails(ss: ModuleType, tmp_path: Path, out: str) -> None:
    write(tmp_path, ".github/shipmill.toml", GATE + "prs = false\n")
    with pytest.raises(SystemExit) as exc:
        ss.landing_row("me/demo", tmp_path, FakeGh(out))
    assert exc.value.code == 2


def test_shipmills_own_config_lands_its_prs(ss: ModuleType) -> None:
    gh = FakeGh(pulls((1, False)))
    assert ss.landing_row("shipmill/shipmill", SCRIPT.parents[3], gh).state == "LANDING_OK"
    assert gh.calls == []
