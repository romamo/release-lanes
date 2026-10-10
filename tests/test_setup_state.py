"""shipmill-setup's checklist script, without gh or the network"""

import importlib.util
import json
import shutil
import subprocess
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
AGENT_LABELS = {"needs-decision", "human", "shipmill-upgrade-later"}  # D-21, spec 017, spec 014's "not now"


def test_s005_16_headless_wants_the_needs_decision_label(ss: ModuleType, tmp_path: Path) -> None:
    write(tmp_path, ".github/shipmill.toml", HEADLESS)
    wanted = ss.wanted_labels(tmp_path)
    assert set(wanted) == BASE_LABELS | AGENT_LABELS
    assert wanted["needs-decision"] == ("d876e3", "A shipmill session asked a question here; waits for a reply")
    row = ss.labels_row(["needs-decision"], wanted)
    assert (row.state, row.detail) == ("LABELS_MISSING", "needs-decision")
    assert ss.labels_row([], wanted).detail == (
        "postponed, blocked, shipmill-hold, needs-decision, human, shipmill-upgrade-later, and the blocker label exist"
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
    assert set(wanted) == BASE_LABELS | AGENT_LABELS
    assert ss.labels_row([], wanted).detail == (
        "postponed, blocked, shipmill-hold, needs-decision, human, shipmill-upgrade-later, and the blocker label exist"
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
    assert set(ss.wanted_labels(tmp_path)) == BASE_LABELS | AGENT_LABELS  # the [agents] section, D-21
    assert ss.agents_row(tmp_path).state == "AGENTS_NO_MODE"  # another table's mode isn't [agents]'s


def test_s005_16_fix_creates_the_label_only_when_wanted(ss: ModuleType, tmp_path: Path) -> None:
    gh = FakeGh("")
    write(tmp_path, ".github/shipmill.toml", HEADLESS)
    ss.create_labels("me/demo", ss.wanted_labels(tmp_path), ["needs-decision"], ["gh"], gh)
    description = "A shipmill session asked a question here; waits for a reply"
    create = ["gh", "label", "create", "needs-decision", "-R", "me/demo", "--color", "d876e3"]
    assert gh.calls == [[*create, "--description", description]]

    write(tmp_path, ".github/shipmill.toml", 'mode = "release"\n')
    wanted = ss.wanted_labels(tmp_path)
    gh.calls.clear()
    ss.create_labels("me/demo", wanted, [name for name in wanted if name == "release-blocker"], ["gh"], gh)
    assert [call[3] for call in gh.calls] == ["release-blocker"]


@pytest.mark.parametrize(
    "text", [HEADLESS, '[agents]\nprompt = "x"\n', '[agents]\nprompt = "x"\napp_id = 1\n', "", 'mode = "release"\n']
)
def test_s017_21_human_is_wanted_wherever_needs_decision_is(ss: ModuleType, tmp_path: Path, text: str) -> None:
    write(tmp_path, ".github/shipmill.toml", text)
    wanted = ss.wanted_labels(tmp_path)
    assert ("human" in wanted) is ("needs-decision" in wanted)
    if "human" not in wanted:
        return
    description = "A person's item: its assignees do it, and a shipmill session hands it off"
    assert wanted["human"] == ("0e8a16", description)
    row = ss.labels_row(["needs-decision", "human"], wanted)
    assert (row.state, row.detail) == ("LABELS_MISSING", "needs-decision, human")
    gh = FakeGh("")
    ss.create_labels("me/demo", wanted, ["needs-decision", "human"], ["gh"], gh)
    assert [call[3] for call in gh.calls] == ["needs-decision", "human"]
    assert gh.calls[1] == [
        *["gh", "label", "create", "human", "-R", "me/demo", "--color", "0e8a16"],
        *["--description", description],
    ]


APP_GH = ["uvx", "--from", "git+https://github.com/shipmill/shipmill@v0", "shipmill", "--repo"]


def test_290_fix_creates_labels_through_shipmill_gh_with_app_id(ss: ModuleType, tmp_path: Path) -> None:
    # #290, spec 012: with [agents] app_id set, --fix's labels post as the App, not the person
    write(tmp_path, ".github/shipmill.toml", '[agents]\nprompt = "x"\napp_id = 7\nmode = "interactive"\n')
    write_gh = ss.gh_writer(tmp_path, ss.watch_module())
    assert write_gh == [*APP_GH, str(tmp_path.resolve()), "gh"]
    gh = FakeGh("")
    ss.create_labels("me/demo", ss.wanted_labels(tmp_path), ["needs-decision"], write_gh, gh)
    assert gh.calls[0][: len(write_gh) + 3] == [*write_gh, "label", "create", "needs-decision"]


@pytest.mark.parametrize("text", [None, 'mode = "release"\n', '[agents]\nprompt = "x"\n'])
def test_290_fix_creates_labels_with_plain_gh_without_app_id(ss: ModuleType, tmp_path: Path, text: str | None) -> None:
    if text is not None:
        write(tmp_path, ".github/shipmill.toml", text)
    assert ss.gh_writer(tmp_path, ss.watch_module()) == ["gh"]


def test_290_a_malformed_config_fails_rather_than_writing_as_the_person(ss: ModuleType, tmp_path: Path) -> None:
    write(tmp_path, ".github/shipmill.toml", "[agents\napp_id = 7\n")
    with pytest.raises(SystemExit) as exc:
        ss.gh_writer(tmp_path, ss.watch_module())
    assert exc.value.code == 2


def test_290_a_failing_shipmill_gh_stops_and_is_never_retried(ss: ModuleType, tmp_path: Path) -> None:
    calls: list[list[str]] = []

    def app_gh(cmd: list[str]) -> str:
        calls.append(cmd)
        return str(ss.run(["false"]))  # shipmill gh exits 2: no key, App not installed

    with pytest.raises(SystemExit) as exc:
        ss.create_labels("me/demo", {"a": ("ffffff", "a"), "b": ("ffffff", "b")}, ["a", "b"], APP_GH, app_gh)
    assert exc.value.code == 2
    assert len(calls) == 1 and calls[0][0] == "uvx"


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


ROOT = SCRIPT.parents[3]
SKILLS = ["github-issue-resolve", "github-issue-triage", "github-pr-triage", "github-ship-watch", "product-intake"]


@pytest.mark.parametrize(
    ("prompt", "found"),
    [
        ("/github-issue-triage {repo} triage; then /github-pr-triage", ["github-issue-triage", "github-pr-triage"]),
        ("/github-ship-watch {repo}. Then /github-ship-watch again", ["github-ship-watch"]),
        ("/shipmill:github-issue-triage {repo} merge when green", []),
        ("run skills/github-issue-triage/scripts/triage_state.py, see /github-issue-triage/x", []),
        ("/github-issue-triager {repo} or /my-skill or github-issue-triage", []),
        ("/other:github-issue-triage {repo}", []),
    ],
)
def test_a_prompt_calls_a_shipmill_skill_only_by_its_slash_name(ss: ModuleType, prompt: str, found: list[str]) -> None:
    assert ss.unprefixed(prompt, SKILLS) == found


@pytest.mark.parametrize("quote", ['"', "'"])
def test_an_unprefixed_prompt_is_unfinished_with_the_prefixed_fix(ss: ModuleType, tmp_path: Path, quote: str) -> None:
    # #236: every gate prompt named /github-issue-triage, so a link in ~/.claude/skills ran in place of the plugin
    prompt = f"{quote}/github-issue-triage {{repo}} triage the new issues; merge when green{quote}"
    write(tmp_path, ".github/shipmill.toml", f"[agents]\nprompt = {prompt}\napp_id = 1\nmode = 'headless'\n")
    (row,) = ss.prompt_rows(tmp_path, ss.watch_module())
    assert (row.state, row.detail) == (
        "AGENTS_UNPREFIXED",
        "[agents] prompt calls /github-issue-triage: a skill of that name in ~/.claude/skills or ~/.agents/skills"
        " runs in place of the plugin's; write /shipmill:github-issue-triage",
    )
    assert row.state not in ss.DONE  # so setup_state.py exits 1 on it
    assert ss.agents_row(tmp_path).state == "AGENTS_OK"  # its own row, beside the section's


@pytest.mark.parametrize(
    "text", ["", 'mode = "release"\n', "[agents]\nprs = true\n", '[agents]\nprompt = "/shipmill:github-pr-triage"\n']
)
def test_no_unprefixed_row_without_a_bare_call(ss: ModuleType, tmp_path: Path, text: str) -> None:
    write(tmp_path, ".github/shipmill.toml", text)
    assert ss.prompt_rows(tmp_path, ss.watch_module()) == []


def test_a_malformed_config_fails_the_prompt_check(ss: ModuleType, tmp_path: Path) -> None:
    write(tmp_path, ".github/shipmill.toml", '[agents]\nprompt = "unclosed\n')
    with pytest.raises(SystemExit) as exc:
        ss.prompt_rows(tmp_path, ss.watch_module())
    assert exc.value.code == 2


@pytest.mark.parametrize(
    "path",
    [
        ".github/shipmill.toml",
        "README.md",
        "docs/install.md",
        "docs/flow.md",
        "docs/design/agent-modes.md",
        "skills/shipmill-setup/SKILL.md",
        "skills/github-ship-watch/SKILL.md",
        "src/shipmill/init.py",
    ],
)
def test_every_prompt_shipmill_writes_or_recommends_names_the_plugins_skills(ss: ModuleType, path: str) -> None:
    # #236: the prompts in shipmill's config, setup, and docs call /shipmill:<skill>
    text = (ROOT / path).read_text(encoding="utf-8")
    assert ss.unprefixed(text, ss.watch_module().skill_names()) == []


def test_shipmills_own_prompt_is_prefixed(ss: ModuleType) -> None:
    assert ss.prompt_rows(ROOT, ss.watch_module()) == []


def test_a_shadowing_link_in_the_home_folder_is_unfinished(ss: ModuleType, tmp_path: Path) -> None:
    # #236: the checklist names each link with its fix, and never touches the real home folder
    home, checkout = tmp_path / "home", tmp_path / "work" / "github-issue-triage"
    checkout.mkdir(parents=True)
    (checkout / "SKILL.md").write_text("---\nname: github-issue-triage\n---\n", encoding="utf-8")
    (home / ".claude" / "skills").mkdir(parents=True)
    link = home / ".claude" / "skills" / "github-issue-triage"
    link.symlink_to(checkout)
    (row,) = ss.skill_rows(home, ss.watch_module())
    assert row.state == "SKILL_SHADOWED"
    assert row.detail.startswith(f"github-issue-triage: {link} is a link to {checkout.resolve()}: ")
    assert row.detail.endswith(f"remove it (rm {link}) or call the skill as /shipmill:github-issue-triage")
    assert row.state not in ss.DONE
    assert link.is_symlink()  # reported, never removed


def test_a_home_folder_without_shadowing_skills_is_done(ss: ModuleType, tmp_path: Path) -> None:
    (tmp_path / ".agents" / "skills" / "unrelated").mkdir(parents=True)
    (row,) = ss.skill_rows(tmp_path, ss.watch_module())
    assert row.state == "SKILLS_OK"
    assert row.state in ss.DONE


# -- spec 015: RUNNER_SHARED ----------------------------------------------------------------

RUNNER_CALLER = """name: Release
on:
  push:
    branches: [main]
jobs:
  prepare:
    uses: shipmill/shipmill/.github/workflows/prepare.yml@v0
    with:
      lane: ${{ inputs.lane != 'policy' && inputs.lane || '' }}
      runs-on: RUNNER
  ci:
    needs: prepare
    uses: ./.github/workflows/ci.yml
    with:
      ref: ${{ needs.prepare.outputs.sha }}
  land:
    needs: [prepare, ci]
    uses: shipmill/shipmill/.github/workflows/land.yml@v0
    with:
      sha: ${{ needs.prepare.outputs.sha }}
      runs-on: RUNNER
"""
RUNNER_CONFIG = """name = "demo"
mode = "release"

[lanes.stable]
QUIET
dispatch = [
  "publish.yml",  # PyPI
]

[environments.production]
lane = "stable"
workflow = "deploy.yml"
"""
DEPLOY = """on:
  workflow_dispatch:
jobs:
  ref:
    runs-on: ubuntu-latest
    steps:
      - run: echo checked
  deploy:
    needs: ref
    runs-on: LABELS
    environment: ${{ inputs.environment }}
    steps:
      - run: |
          echo deploy
"""


def runner_repo(root: Path, runner: str | None, labels: str, quiet: str = "quiet_minutes = 30") -> Path:
    caller = RUNNER_CALLER.replace("RUNNER", runner) if runner else CALLER
    write(root, ".github/workflows/release.yml", caller)
    write(root, ".github/shipmill.toml", RUNNER_CONFIG.replace("QUIET", quiet))
    write(root, ".github/workflows/deploy.yml", DEPLOY.replace("LABELS", labels))
    return root


@pytest.mark.parametrize(
    ("runner", "labels"),
    [
        ("self-hosted", "self-hosted"),  # the same set
        ('\'["self-hosted","linux"]\'', "self-hosted"),  # the deploy's set is a subset
        ("self-hosted", "[self-hosted, linux]"),  # a superset, as an inline list
        ('\'["self-hosted","linux"]\'', "\n      - linux\n      - Self-Hosted  # labels ignore case"),
        ("'self-hosted' # quoted, with a comment", "'self-hosted'"),
    ],
)
def test_s015_12_a_deploy_on_shipmills_runner_reads_runner_shared(
    ss: ModuleType, tmp_path: Path, runner: str, labels: str
) -> None:
    runner_repo(tmp_path, runner, labels)
    (row,) = ss.runner_rows(tmp_path, ss.watch_module())
    assert row.state == "RUNNER_SHARED"
    assert row.detail.startswith("deploy.yml's job deploy runs on ")
    assert "the settle wait holds it for up to 30 minutes after each push" in row.detail
    assert "give shipmill a runner of its own (another label or instance), or drop quiet_minutes" in row.detail


def test_s015_12_a_dispatch_workflow_on_shipmills_runner_is_named(ss: ModuleType, tmp_path: Path) -> None:
    runner_repo(tmp_path, "self-hosted", "deploy-box")
    write(tmp_path, ".github/workflows/publish.yml", DEPLOY.replace("LABELS", "[self-hosted]"))
    (row,) = ss.runner_rows(tmp_path, ss.watch_module())
    assert row.detail.startswith("publish.yml's job deploy runs on self-hosted, as shipmill's release jobs do")


@pytest.mark.parametrize(
    ("runner", "labels", "quiet"),
    [
        ("self-hosted", "self-hosted", ""),  # quiet_minutes unset
        ("self-hosted", "self-hosted", "quiet_minutes = 0"),
        ("self-hosted", "deploy-box", "quiet_minutes = 30"),  # disjoint labels
        ('\'["self-hosted","build"]\'', "[self-hosted, deploy]", "quiet_minutes = 30"),  # overlapping only
        (None, "ubuntu-latest", "quiet_minutes = 30"),  # no runs-on in release.yml
        ("ubuntu-latest", "ubuntu-latest", "quiet_minutes = 30"),  # the default, written out
        ("${{ vars.RUNNER }}", "self-hosted", "quiet_minutes = 30"),  # only the run resolves it
        ("self-hosted", "${{ matrix.runner }}", "quiet_minutes = 30"),  # an expression isn't compared
        ("self-hosted", "\n      group: deployers\n      labels: [self-hosted]", "quiet_minutes = 30"),  # a group
    ],
)
def test_s015_12_no_runner_shared_row_otherwise(
    ss: ModuleType, tmp_path: Path, runner: str | None, labels: str, quiet: str
) -> None:
    runner_repo(tmp_path, runner, labels, quiet)
    assert ss.runner_rows(tmp_path, ss.watch_module()) == []


def test_s015_12_a_yaml_list_passed_to_the_string_input_fails(ss: ModuleType, tmp_path: Path) -> None:
    runner_repo(tmp_path, "[self-hosted, linux]", "self-hosted")
    with pytest.raises(SystemExit) as exc:
        ss.runner_rows(tmp_path, ss.watch_module())
    assert exc.value.code == 2


def test_s015_12_runner_shared_is_a_warning_not_a_done_state(ss: ModuleType) -> None:
    done = [ss.Row(state, "") for state in sorted(ss.DONE)]
    assert ss.exit_code([*done, ss.Row("RUNNER_SHARED", "deploy.yml")]) == 0
    assert "RUNNER_SHARED" not in ss.DONE
    assert ss.exit_code([*done, ss.Row("LANDING_OFF", "#1")]) == 1


def test_s015_12_the_310_reader_reads_lanes_and_environments_as_tomllib_does(ss: ModuleType) -> None:
    ws, where = ss.watch_module(), Path(".github/shipmill.toml")
    text = RUNNER_CONFIG.replace("QUIET", "quiet_minutes = 30") + '\n[lanes.dev]\ndispatch = ["dev.yml"]\n'
    want = (30, ["publish.yml", "dev.yml", "deploy.yml"])
    assert ss.lane_runner_config(ws.tomllib.loads(text), where) == want
    assert ss.lane_runner_config_310(text, where, ws) == want
    with pytest.raises(SystemExit):
        ss.lane_runner_config_310("[lanes]\nstable = { quiet_minutes = 30 }\n", where, ws)


def test_s015_12_runner_shared_runs_under_python_310(tmp_path: Path) -> None:
    runner_repo(tmp_path, '\'["self-hosted","linux"]\'', "[self-hosted]")
    uv = shutil.which("uv")
    assert uv is not None, "uv runs the skill scripts"
    code = (
        "import importlib.util, sys; from pathlib import Path\n"
        "spec = importlib.util.spec_from_file_location('setup_state', sys.argv[1])\n"
        "ss = importlib.util.module_from_spec(spec); sys.modules['setup_state'] = ss; spec.loader.exec_module(ss)\n"
        "ws = ss.watch_module(); assert ws.tomllib is None, 'tomllib on 3.10'\n"
        "print(sys.version_info[:2], [r.state for r in ss.runner_rows(Path(sys.argv[2]), ws)])\n"
    )
    command = [uv, "run", "--no-project", "--python", "3.10", "python", "-c", code, str(SCRIPT), str(tmp_path)]
    done = subprocess.run(command, capture_output=True, text=True, check=False)
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == "(3, 10) ['RUNNER_SHARED']"
