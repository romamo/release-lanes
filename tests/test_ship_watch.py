"""The github-ship-watch skill's classification, without git, gh, or the network"""

import datetime as dt
import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "skills" / "github-ship-watch" / "scripts" / "watch_state.py"
NOW = dt.datetime(2026, 10, 3, 12, 0, tzinfo=dt.UTC)
GRACE = dt.timedelta(minutes=20)


@pytest.fixture(scope="module")
def ws() -> ModuleType:
    spec = importlib.util.spec_from_file_location("watch_state", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses look their module up by name
    spec.loader.exec_module(module)
    return module


def run(ws: ModuleType, status: str, conclusion: str, minutes_ago: int) -> object:
    return ws.Run(status, conclusion, NOW - dt.timedelta(minutes=minutes_ago), f"run-{minutes_ago}")


def states(rows: list[object]) -> list[str]:
    return [r.state for r in rows]  # type: ignore[attr-defined]


def test_a_failed_latest_run_is_reported(ws: ModuleType) -> None:
    runs = [run(ws, "completed", "failure", 5), run(ws, "completed", "success", 60)]
    assert states(ws.bot_rows(runs, None, NOW, GRACE, "release.yml")) == ["BOT_FAILED"]


def test_a_cancelled_settle_run_is_not_a_failure(ws: ModuleType) -> None:
    runs = [run(ws, "completed", "cancelled", 5), run(ws, "completed", "success", 60)]
    assert states(ws.bot_rows(runs, None, NOW, GRACE, "release.yml")) == ["BOT_OK"]


def test_a_success_after_a_failure_clears_it(ws: ModuleType) -> None:
    runs = [run(ws, "completed", "success", 5), run(ws, "completed", "failure", 60)]
    assert states(ws.bot_rows(runs, None, NOW, GRACE, "release.yml")) == ["BOT_OK"]


def test_a_due_release_with_no_run_is_stalled(ws: ModuleType) -> None:
    # the #6 case: a hand-started policy run cancelled the push run's wait, then skipped
    runs = [run(ws, "completed", "success", 40), run(ws, "completed", "cancelled", 41)]
    rows = ws.bot_rows(runs, "stable 0.3.0: main quiet for 40 min", NOW, GRACE, "release.yml")
    assert states(rows) == ["BOT_STALLED"]


@pytest.mark.parametrize(("status", "minutes_ago"), [("in_progress", 40), ("queued", 40), ("completed", 5)])
def test_a_due_release_with_a_live_or_recent_run_waits(ws: ModuleType, status: str, minutes_ago: int) -> None:
    runs = [run(ws, status, "success" if status == "completed" else "", minutes_ago)]
    assert states(ws.bot_rows(runs, "stable 0.3.0", NOW, GRACE, "release.yml")) == ["BOT_OK"]


def test_publish_states(ws: ModuleType) -> None:
    old = ws.Tag("v1.2.0", NOW - dt.timedelta(hours=1))
    new = ws.Tag("v1.2.1", NOW - dt.timedelta(minutes=5))
    assert ws.publish_row(old, "pkg", True, NOW, GRACE).state == "PUBLISHED"
    assert ws.publish_row(old, "pkg", False, NOW, GRACE).state == "NOT_PUBLISHED"
    assert ws.publish_row(new, "pkg", False, NOW, GRACE).state == "PUBLISHING"
    assert ws.publish_row(old, None, None, NOW, GRACE).state == "NO_REGISTRY"


def test_bot_detection(ws: ModuleType, tmp_path: Path) -> None:
    assert ws.bot_workflow(tmp_path) is None
    (tmp_path / ".github" / "workflows").mkdir(parents=True)
    (tmp_path / ".github" / "release-policy.toml").write_text('mode = "release"\n')
    with pytest.raises(SystemExit):
        ws.bot_workflow(tmp_path)
    (tmp_path / ".github" / "workflows" / "release-bot.yml").write_text("name: Release bot\n")
    assert ws.bot_workflow(tmp_path) == ("release-bot.yml", False)
    caller = "jobs:\n  prepare:\n    uses: romamo/shipyard/.github/workflows/prepare.yml@v0\n"
    (tmp_path / ".github" / "workflows" / "release.yml").write_text(caller)
    assert ws.bot_workflow(tmp_path) == ("release.yml", True)


def test_package_name_reads_the_project_table(ws: ModuleType, tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text('[tool.x]\nname = "not-it"\n\n[project]\nname = "pkg"\nversion = "1"\n')
    assert ws.package_name(tmp_path) == "pkg"
