"""github-pr-triage's shipped.py: who its notices post as (#290, spec 012), without gh"""

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "skills" / "github-pr-triage" / "scripts" / "shipped.py"
APP_GH = ["uvx", "--from", "git+https://github.com/shipmill/shipmill@v0", "shipmill", "--repo"]
CLOSED = json.dumps({"state": "CLOSED", "title": "a fix", "comments": [], "url": "https://github.com/me/demo/issues/5"})


@pytest.fixture(scope="module")
def sp() -> ModuleType:
    spec = importlib.util.spec_from_file_location("shipped", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def config(root: Path, text: str) -> None:
    (root / ".github").mkdir(parents=True, exist_ok=True)
    (root / ".github" / "shipmill.toml").write_text(text, encoding="utf-8")


class FakeGh:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def __call__(self, cmd: list[str]) -> str:
        self.calls.append(cmd)
        return CLOSED if cmd[:3] == ["gh", "issue", "view"] else ""


def test_290_with_app_id_the_notice_posts_through_shipmill_gh(sp: ModuleType, tmp_path: Path) -> None:
    config(tmp_path, '[agents]\nprompt = "x"\napp_id = 7\n')
    write = sp.writer(tmp_path, sp.watch_module())
    assert write == [*APP_GH, str(tmp_path.resolve()), "gh"]
    gh = FakeGh()
    sp.tell("me/demo", {5: {"abc1234"}}, "v1.2.0", "", write, gh)
    view, comment = gh.calls
    assert view[:3] == ["gh", "issue", "view"]  # reads stay on plain gh
    assert comment == [*write, "issue", "comment", "5", "-R", "me/demo", "--body", "Released in v1.2.0."]


def test_290_run_from_a_subfolder_the_notice_still_posts_as_the_app(sp: ModuleType, tmp_path: Path) -> None:
    # --repo-dir defaults to the cwd: a subfolder of the checkout must not read as "no App"
    (tmp_path / ".git").mkdir()
    (tmp_path / "docs").mkdir()
    config(tmp_path, '[agents]\nprompt = "x"\napp_id = 7\n')
    assert sp.writer(tmp_path / "docs", sp.watch_module()) == [*APP_GH, str(tmp_path.resolve()), "gh"]


@pytest.mark.parametrize("text", [None, 'mode = "release"\n', '[agents]\nprompt = "x"\n'])
def test_290_without_app_id_the_notice_posts_with_plain_gh(sp: ModuleType, tmp_path: Path, text: str | None) -> None:
    if text is not None:
        config(tmp_path, text)
    write = sp.writer(tmp_path, sp.watch_module())
    assert write == ["gh"]
    gh = FakeGh()
    sp.tell("me/demo", {5: {"abc1234"}}, "v1.2.0", "`uv add x`", write, gh)
    assert gh.calls[1] == ["gh", "issue", "comment", "5", "-R", "me/demo", "--body", "Released in v1.2.0. `uv add x`"]


@pytest.mark.parametrize(
    "text",
    [
        "[agents\napp_id = 7\n",  # TOML's syntax error
        "[release]\napp_id = 7\n",  # an app_id the [agents] reader never sees, as 3.10's fallback can miss it
    ],
)
def test_290_a_malformed_config_fails_rather_than_posting_as_the_person(
    sp: ModuleType, tmp_path: Path, text: str
) -> None:
    config(tmp_path, text)
    with pytest.raises(SystemExit) as exc:
        sp.writer(tmp_path, sp.watch_module())
    assert exc.value.code == 2


def test_290_a_failing_shipmill_gh_stops_and_is_never_retried(sp: ModuleType) -> None:
    calls: list[list[str]] = []

    def gh(cmd: list[str]) -> str:
        calls.append(cmd)
        if cmd[0] == "uvx":
            return str(sp.run(["false"]))  # shipmill gh exits 2: no key, App not installed
        return CLOSED

    write = [*APP_GH, "/repo", "gh"]
    with pytest.raises(SystemExit) as exc:
        sp.tell("me/demo", {5: {"a"}, 6: {"b"}}, "v1.2.0", "", write, gh)
    assert exc.value.code == 2
    assert [c[0] for c in calls] == ["gh", "uvx"]  # the first write stops the run


def test_without_post_nothing_is_written(sp: ModuleType) -> None:
    gh = FakeGh()
    sp.tell("me/demo", {5: {"abc1234"}}, "v1.2.0", "", None, gh)
    assert [c[:3] for c in gh.calls] == [["gh", "issue", "view"]]
