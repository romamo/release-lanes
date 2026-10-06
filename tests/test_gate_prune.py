"""`shipmill gate` prunes the repository's landed worktrees on each tick (spec 002, S-002-15
and S-002-16); each test runs the gate from a detached linked gate checkout, as
tmp/shipmill-gate is in shipmill-setup, with the real judge and prune"""

import datetime as dt
import json
import os
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

import pytest

from shipmill.autonomy import Hold
from shipmill.errors import ReleaseError
from shipmill.gate import Action, Decision, Finding, Waiting, gate, pruner, tick_lines, tick_record
from shipmill.worktrees import Judged, LiveSession

from .test_gate import ISSUES, FakeClaude, FakeNotifier, cfg, on_hold
from .test_worktrees import CHANGELOG, Checkout, branches, fake_tool, run

REPO = "romamo/demo"
DONE = ".claude/worktrees/done"
GATE = "tmp/shipmill-gate"
KEYS = {"action", "reason", "work", "stopped", "launched", "identity", "waiting", "pruned"}

Tick = tuple[Decision, str | None, tuple[Waiting, ...], tuple[Judged, ...]]


@pytest.fixture
def repo(tmp_path: Path) -> Checkout:
    """A main checkout whose origin's path ends in romamo/demo.git, so the gate's origin
    check passes, with the gate's detached checkout and one landed worktree beside it"""
    origin = tmp_path / "romamo" / "demo.git"
    run("git", "init", "-q", "--bare", "-b", "main", str(origin))
    root = tmp_path / "work"
    run("git", "clone", "-q", str(origin), str(root))
    c = Checkout(root)
    c.git().run("checkout", "-q", "-b", "main")
    c.commit(root, {"CHANGELOG.md": CHANGELOG, "app.py": "VALUE = 1\n"}, "Start")
    c.push_main()
    c.add(GATE, "--detach")
    c.add(DONE, "-b", "feat/done")
    return c


def tick(
    c: Checkout,
    claude: FakeClaude,
    hold: Callable[[], Hold] = Hold,
    findings: tuple[Finding, ...] = (ISSUES,),
    dry_run: bool = False,
) -> Tick:
    git = c.git(c.root / GATE)
    now = dt.datetime.now(dt.UTC)  # the worktrees' ages count from the real clock
    return gate(
        git,
        REPO,
        lambda: cfg(),
        claude,
        lambda _: list(findings),
        now,
        hold,
        FakeNotifier(),
        pruner(git, c.github, c.sessions),
        dry_run=dry_run,
    )


def test_s002_15_a_tick_prunes_the_landed_worktrees_and_reports_them(repo: Checkout) -> None:
    young = repo.add("tmp/wt-young", "-b", "feat/young", hours=5)
    claude = FakeClaude()
    decision, launched, waiting, pruned = tick(repo, claude)
    assert (decision.action, launched) == (Action.LAUNCH, "s1")
    assert [(j.path, j.worktree.branch) for j in pruned] == [(DONE, "feat/done")]
    assert not (repo.root / DONE).exists()
    assert (repo.root / GATE).is_dir() and young.is_dir()  # the gate's own checkout is never a candidate
    assert branches(repo) == {"main", "feat/young"}
    lines = tick_lines(decision, launched, waiting, pruned, dry_run=False)
    assert f"  pruned {DONE} (feat/done)" in lines
    record = json.loads(json.dumps(tick_record(decision, launched, waiting, pruned, dry_run=False)))
    assert set(record) == KEYS  # every key the gate printed before, and pruned
    assert record["pruned"] == [DONE]


def test_s002_15_a_held_tick_still_prunes(repo: Checkout) -> None:
    claude = FakeClaude()
    decision, launched, waiting, pruned = tick(repo, claude, hold=on_hold)
    assert (decision.action, launched, claude.launched) == (Action.HELD, None, [])
    assert [j.path for j in pruned] == [DONE] and not (repo.root / DONE).exists()
    assert tick_lines(decision, launched, waiting, pruned, dry_run=False)[1:] == [f"  pruned {DONE} (feat/done)"]
    assert tick_record(decision, launched, waiting, pruned, dry_run=False)["pruned"] == [DONE]


def test_s002_15_a_tick_with_nothing_landed_prunes_nothing(repo: Checkout) -> None:
    """A worktree a live session works in stays, as does one with work not landed"""
    open_ = repo.add("tmp/wt-open", "-b", "feat/open")
    repo.commit(open_, {"feature.py": "A = 1\n"}, "Unlanded")
    repo.sessions.rows = [LiveSession("shipmill romamo/demo 2026-10-06 09:00", repo.root / DONE)]
    decision, launched, waiting, pruned = tick(repo, FakeClaude(), findings=())
    assert (decision.action, pruned) == (Action.QUIET, ())
    assert (repo.root / DONE).is_dir() and open_.is_dir()
    assert tick_record(decision, launched, waiting, pruned, dry_run=False)["pruned"] == []
    assert tick_lines(decision, launched, waiting, pruned, dry_run=False) == ["QUIET: nothing needs an agent"]


def test_s002_16_a_dry_run_lists_what_it_would_prune_and_removes_nothing(repo: Checkout) -> None:
    before = repo.state()
    claude = FakeClaude()
    decision, launched, waiting, pruned = tick(repo, claude, dry_run=True)
    assert (decision.action, launched, claude.launched) == (Action.LAUNCH, None, [])
    assert repo.state() == before and (repo.root / DONE).is_dir()
    assert f"  would prune {DONE} (feat/done)" in tick_lines(decision, launched, waiting, pruned, dry_run=True)
    assert tick_record(decision, launched, waiting, pruned, dry_run=True)["pruned"] == [DONE]


def test_s002_16_a_failing_gh_pr_list_fails_the_tick_before_any_launch(repo: Checkout) -> None:
    before = repo.state()
    repo.github.pulls_error = "HTTP 502"
    claude = FakeClaude()
    with pytest.raises(ReleaseError, match="gh pr list failed: HTTP 502"):
        tick(repo, claude)
    assert (claude.launched, claude.stopped) == ([], [])
    assert repo.state() == before


def test_s002_16_a_failing_claude_agents_fails_the_tick_before_any_launch(repo: Checkout) -> None:
    before = repo.state()
    git = repo.git(repo.root / GATE)
    claude = FakeClaude()

    class Broken:
        def live(self) -> list[LiveSession]:
            raise ReleaseError("claude agents --json failed with exit 1: boom")

    with pytest.raises(ReleaseError, match="claude agents --json failed"):
        gate(
            git,
            REPO,
            lambda: cfg(),
            claude,
            lambda _: [ISSUES],
            dt.datetime.now(dt.UTC),
            Hold,
            FakeNotifier(),
            pruner(git, repo.github, Broken()),
        )
    assert claude.launched == []
    assert repo.state() == before


def test_s002_16_a_removal_git_refuses_fails_the_tick_before_any_launch(repo: Checkout) -> None:
    common = Path(repo.git().run("rev-parse", "--path-format=absolute", "--git-common-dir").strip())
    (common / "refs/heads/feat/done.lock").write_text("", encoding="utf-8")  # another git holds the ref
    claude = FakeClaude()
    with pytest.raises(ReleaseError, match=rf"prune stopped at {DONE} \(feat/done\): git branch -D feat/done failed"):
        tick(repo, claude)
    assert (claude.launched, claude.stopped) == ([], [])


def test_s002_16_shipmill_gate_exits_2_on_a_prune_error_and_launches_nothing(repo: Checkout, tmp_path: Path) -> None:
    """A claude whose gate-session listing works but whose `claude agents --json` (every
    session, which the prune reads) fails; a launch would leave launched.txt behind"""
    launched = tmp_path / "launched.txt"
    script = (
        'if [ "$3" = "--cwd" ]; then echo "[]"; exit 0; fi\n'
        f'if [ "$1" = "--bg" ]; then echo x > {launched}; echo "claude attach s1"; exit 0; fi\n'
        "echo 'not logged in' >&2\nexit 3\n"
    )
    bin_dir = fake_tool(tmp_path, "claude", script)
    before = repo.state()
    env = {**os.environ, "PATH": str(bin_dir)}
    cmd = [sys.executable, "-c", "from shipmill.cli import run; run()", "--repo", str(repo.root / GATE), "gate", REPO]
    proc = subprocess.run(cmd, capture_output=True, text=True, env=env)
    assert proc.returncode == 2, proc.stderr
    assert proc.stderr.startswith("shipmill: claude agents --json failed with exit 3: not logged in")
    assert proc.stdout == ""
    assert not launched.exists()
    assert repo.state() == before
