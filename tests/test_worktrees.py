"""`shipmill worktrees` judges every worktree of a repository (spec 002); each test builds
its own throwaway repository under tmp_path"""

import datetime as dt
import json
import os
import shutil
import stat
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from shipmill.cli import main
from shipmill.errors import ReleaseError
from shipmill.github import GhCli, PullRequest
from shipmill.gitrepo import Git
from shipmill.worktrees import (
    ClaudeSessions,
    Judged,
    LiveSession,
    Verdict,
    judge,
    parse_live_sessions,
    parse_worktrees,
    prune,
)

from .conftest import FakeGitHub

CHANGELOG = "# Changelog\n\n## [Unreleased]\n\n### Added\n\n"
OLD = 72  # hours: past the 24-hour floor


@dataclass
class FakeSessions:
    rows: list[LiveSession] = field(default_factory=list)

    def live(self) -> list[LiveSession]:
        return list(self.rows)


@dataclass
class Checkout:
    """A main checkout of a repository whose origin is a bare repository beside it"""

    root: Path
    github: FakeGitHub = field(default_factory=FakeGitHub)
    sessions: FakeSessions = field(default_factory=FakeSessions)

    def git(self, where: Path | None = None) -> Git:
        return Git(where or self.root, "t", "t@example.com")

    def add(self, rel: str, *flags: str, hours: float = OLD, base: str = "origin/main") -> Path:
        """`git worktree add` at rel under the main checkout, created hours ago"""
        path = self.root / rel
        self.git().run("worktree", "add", "-q", *flags, str(path), base)
        self.age(path, hours)
        return path

    def age(self, path: Path, hours: float) -> None:
        admin = Path(self.git(path).run("rev-parse", "--path-format=absolute", "--git-dir").strip())
        when = (dt.datetime.now(dt.UTC) - dt.timedelta(hours=hours)).timestamp()
        os.utime(admin / "commondir", (when, when))

    def commit(self, where: Path, files: dict[str, str], message: str) -> str:
        for name, text in files.items():
            (where / name).write_text(text, encoding="utf-8")
        git = self.git(where)
        if files:
            git.run("add", "--", *files)  # never -A: the main checkout holds the worktrees under it
        git.run("commit", "-q", "-m", message)
        return git.sha()

    def push_main(self) -> None:
        self.git().run("push", "-q", "origin", "main")

    def judge(self, where: Path | None = None) -> dict[str, Judged]:
        return {j.path: j for j in judge(self.git(where), self.github, self.sessions, dt.datetime.now(dt.UTC))}

    def reasons(self, where: Path | None = None) -> dict[str, str | None]:
        return {path: j.reason for path, j in self.judge(where).items()}

    def state(self) -> tuple[str, str]:
        """The worktrees and the local branches, to show nothing was removed"""
        git = self.git()
        return git.run("worktree", "list", "--porcelain"), git.run("branch", "--list")


def run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, check=True, capture_output=True, text=True)


@pytest.fixture
def repo(tmp_path: Path) -> Checkout:
    origin = tmp_path / "origin.git"
    run("git", "init", "-q", "--bare", "-b", "main", str(origin))
    root = tmp_path / "work"
    run("git", "clone", "-q", str(origin), str(root))
    c = Checkout(root)
    c.git().run("checkout", "-q", "-b", "main")
    c.commit(root, {"CHANGELOG.md": CHANGELOG, "app.py": "VALUE = 1\n"}, "Start")
    c.push_main()
    return c


def changelog(*entries: str) -> str:
    return CHANGELOG + "".join(f"- {e}\n" for e in entries)


def test_s002_1_the_table_lists_every_worktree_and_removes_nothing(
    repo: Checkout, capsys: pytest.CaptureFixture[str]
) -> None:
    repo.add(".claude/worktrees/done", "-b", "feat/done")
    repo.add("tmp/wt-young", "-b", "feat/young", hours=5)
    before = repo.state()
    assert main(["--repo", str(repo.root), "worktrees"], repo.github, sessions=repo.sessions) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[0].split() == ["verdict", "path", "branch", "age", "reason"]
    rows = [line.split(None, 4) for line in lines[1:]]
    assert rows == [
        ["KEPT", ".", "main", "-", "main checkout"],
        ["REMOVABLE", ".claude/worktrees/done", "feat/done", "3d"],
        ["KEPT", "tmp/wt-young", "feat/young", "5h", "created 5h ago"],
    ]
    assert repo.state() == before


def test_s002_1_age_is_whole_hours_under_two_days_then_days(repo: Checkout) -> None:
    repo.add("tmp/wt-a", "-b", "a", hours=30.5)
    repo.add("tmp/wt-b", "-b", "b", hours=47.9)
    repo.add("tmp/wt-c", "-b", "c", hours=49)
    judged = repo.judge()
    assert [judged[p].age for p in ("tmp/wt-a", "tmp/wt-b", "tmp/wt-c")] == ["30h", "47h", "2d"]


def test_s002_2_json_holds_each_worktrees_fields(repo: Checkout, capsys: pytest.CaptureFixture[str]) -> None:
    done = repo.add(".claude/worktrees/done", "-b", "feat/done")
    repo.add("tmp/wt-detached", "--detach")
    assert main(["--repo", str(repo.root), "worktrees", "--json"], repo.github, sessions=repo.sessions) == 0
    found = json.loads(capsys.readouterr().out)
    assert list(found) == ["worktrees"]
    rows = {row["path"]: row for row in found["worktrees"]}
    assert set(rows) == {".", ".claude/worktrees/done", "tmp/wt-detached"}
    for row in rows.values():
        assert list(row) == ["path", "branch", "head", "verdict", "reason", "created", "age_hours"]
    row = rows[".claude/worktrees/done"]
    assert (row["branch"], row["verdict"], row["reason"]) == ("feat/done", "REMOVABLE", None)
    assert row["head"] == repo.git(done).sha()
    created = dt.datetime.fromisoformat(row["created"])
    assert created.utcoffset() is not None
    assert row["age_hours"] == OLD
    assert abs((dt.datetime.now(dt.UTC) - created) - dt.timedelta(hours=OLD)) < dt.timedelta(minutes=5)
    detached = rows["tmp/wt-detached"]
    assert (detached["branch"], detached["verdict"], detached["reason"]) == (None, "KEPT", "detached HEAD")
    main_row = rows["."]
    assert (main_row["branch"], main_row["reason"], main_row["created"], main_row["age_hours"]) == (
        "main",
        "main checkout",
        None,
        None,
    )


def test_s002_3_main_current_and_foreign_worktrees_are_kept(repo: Checkout, tmp_path: Path) -> None:
    gate = repo.add("tmp/shipmill-gate", "--detach")
    nested = repo.root / "tmp/shipmill-gate/tmp/wt-nested"
    repo.git().run("worktree", "add", "-q", "-b", "nested", str(nested), "origin/main")
    repo.age(nested, OLD)
    current = repo.add(".claude/worktrees/me", "-b", "me")
    elsewhere = tmp_path / "other/.claude/worktrees/x"
    repo.git().run("worktree", "add", "-q", "-b", "x", str(elsewhere), "origin/main")
    repo.age(elsewhere, OLD)
    repo.add("tmp/own", "-b", "own")
    repo.add(".claude/worktrees/me/deeper", "-b", "deeper")
    reasons = repo.reasons(current)
    assert reasons["."] == "main checkout"
    assert reasons[".claude/worktrees/me"] == "current checkout"
    assert reasons["tmp/shipmill-gate"] == "not a shipmill worktree"
    assert reasons["../other/.claude/worktrees/x"] == "not a shipmill worktree"
    assert reasons["tmp/own"] == "not a shipmill worktree"
    assert reasons[".claude/worktrees/me/deeper"] == "not a shipmill worktree"
    assert reasons["tmp/shipmill-gate/tmp/wt-nested"] is None  # under another worktree, so a candidate
    assert gate.is_dir()


def test_s002_4_missing_locked_detached_and_default_branch_are_kept(repo: Checkout) -> None:
    gone = repo.add("tmp/wt-gone", "-b", "gone")
    shutil.rmtree(gone)
    locked = repo.add("tmp/wt-locked", "-b", "locked")
    repo.git().run("worktree", "lock", "--reason", "claude agent a1 (pid 7)", str(locked))
    bare_lock = repo.add("tmp/wt-bare-lock", "-b", "bare-lock")
    repo.git().run("worktree", "lock", str(bare_lock))
    repo.add("tmp/wt-detached", "--detach")
    repo.git().run("switch", "-q", "-c", "elsewhere")
    repo.add(".claude/worktrees/main", hours=OLD, base="main")
    reasons = repo.reasons()
    assert reasons["tmp/wt-gone"] == "directory missing"
    assert reasons["tmp/wt-locked"] == "locked: claude agent a1 (pid 7)"
    assert reasons["tmp/wt-bare-lock"] == "locked"
    assert reasons["tmp/wt-detached"] == "detached HEAD"
    assert reasons[".claude/worktrees/main"] == "holds the default branch"


def test_s002_5_a_worktree_younger_than_a_day_is_kept(repo: Checkout) -> None:
    repo.add("tmp/wt-new", "-b", "new", hours=0.2)
    repo.add("tmp/wt-day", "-b", "day", hours=23.5)
    repo.add("tmp/wt-older", "-b", "older", hours=24.1)
    reasons = repo.reasons()
    assert reasons["tmp/wt-new"] == "created 0h ago"
    assert reasons["tmp/wt-day"] == "created 23h ago"
    assert reasons["tmp/wt-older"] is None


def test_s002_6_a_live_sessions_cwd_in_the_worktree_keeps_it(repo: Checkout, tmp_path: Path) -> None:
    inside = repo.add("tmp/wt-inside", "-b", "inside")
    (inside / "sub").mkdir()
    at = repo.add("tmp/wt-at", "-b", "at")
    link = tmp_path / "link"
    link.symlink_to(at)  # a session may name the worktree through a symlink, as /var for /private/var
    repo.add("tmp/wt-free", "-b", "free")
    repo.add("tmp/wt-free-x", "-b", "free-x")
    repo.sessions.rows = [
        LiveSession("deep", inside / "sub"),
        LiveSession("linked", link),
        LiveSession("parent", repo.root),
        LiveSession("prefix", repo.root / "tmp/wt-free-x-not"),
    ]
    reasons = repo.reasons()
    assert reasons["tmp/wt-inside"] == "live session deep"
    assert reasons["tmp/wt-at"] == "live session linked"
    assert reasons["tmp/wt-free"] is None
    assert reasons["tmp/wt-free-x"] is None


def test_s002_6_sessions_parse_from_claude_agents_json() -> None:
    text = json.dumps(
        [
            {
                "pid": 1,
                "id": "29a3cf82",
                "cwd": "/r/a",
                "kind": "background",
                "name": "shipmill o/r",
                "state": "blocked",
            },
            {"pid": 2, "cwd": "/r/b", "kind": "interactive", "sessionId": "7332", "name": "py-98", "status": "idle"},
            {"pid": 3, "cwd": "/r/c", "kind": "interactive", "sessionId": "abc"},
        ]
    )
    assert parse_live_sessions(text) == [
        LiveSession("shipmill o/r", Path("/r/a")),
        LiveSession("py-98", Path("/r/b")),
        LiveSession("abc", Path("/r/c")),
    ]


def test_s002_7_uncommitted_changes_keep_a_worktree(repo: Checkout) -> None:
    modified = repo.add("tmp/wt-modified", "-b", "modified")
    (modified / "app.py").write_text("VALUE = 2\n", encoding="utf-8")
    staged = repo.add("tmp/wt-staged", "-b", "staged")
    (staged / "new.py").write_text("X = 1\n", encoding="utf-8")
    repo.git(staged).run("add", "new.py")
    untracked = repo.add("tmp/wt-untracked", "-b", "untracked")
    (untracked / "deep").mkdir()
    (untracked / "deep" / "note.txt").write_text("x\n", encoding="utf-8")
    repo.git(untracked).run("config", "status.showUntrackedFiles", "no")
    reasons = repo.reasons()
    for path in ("tmp/wt-modified", "tmp/wt-staged", "tmp/wt-untracked"):
        assert reasons[path] == "uncommitted changes", path


def test_s002_19_a_worktree_holding_another_worktree_is_kept(repo: Checkout) -> None:
    """git worktree remove deletes a worktree nested under an ignored path with its work,
    and the outer one's git status never shows it"""
    (repo.root / ".gitignore").write_text("tmp/\n", encoding="utf-8")
    repo.commit(repo.root, {".gitignore": "tmp/\n"}, "Ignore tmp")
    repo.push_main()
    outer = repo.add("tmp/wt-outer", "-b", "outer")
    inner = outer / "tmp/wt-inner"
    repo.git().run("worktree", "add", "-q", "-b", "inner", str(inner), "origin/main")
    repo.age(inner, OLD)
    (inner / "work.txt").write_text("unsaved\n", encoding="utf-8")
    reasons = repo.reasons()
    assert reasons["tmp/wt-outer"] == "holds worktree tmp/wt-outer/tmp/wt-inner"
    assert reasons["tmp/wt-outer/tmp/wt-inner"] == "uncommitted changes"


def test_s002_8_commits_not_on_the_default_branch_keep_a_worktree(repo: Checkout) -> None:
    open_ = repo.add("tmp/wt-open", "-b", "open")
    repo.commit(open_, {"feature.py": "A = 1\n"}, "Feature")
    repo.commit(open_, {"more.py": "B = 1\n"}, "More")
    half = repo.add("tmp/wt-half", "-b", "half")
    landed = repo.commit(half, {"half.py": "H = 1\n"}, "Half one")
    repo.commit(half, {"other.py": "O = 1\n"}, "Half two")
    notes = repo.add("tmp/wt-notes", "-b", "notes")
    repo.commit(notes, {"CHANGELOG.md": changelog("Notes (#3)")}, "Notes only")
    repo.commit(repo.root, {"CHANGELOG.md": changelog("Other (#4)")}, "Other")
    repo.git().run("cherry-pick", landed)
    repo.push_main()
    reasons = repo.reasons()
    assert reasons["tmp/wt-open"] == "2 commit(s) not landed"
    assert reasons["tmp/wt-half"] == "1 commit(s) not landed"  # its whole diff matches no commit either
    assert reasons["tmp/wt-notes"] == "1 commit(s) not landed"  # CHANGELOG.md only: no id without it


def test_s002_8_a_repo_flag_naming_a_subdirectory_still_compares_whole_commits(repo: Checkout) -> None:
    """--repo defaults to the working directory, which may be a subdirectory of the checkout;
    the patch ids without CHANGELOG.md must still cover the whole tree, not that subdirectory"""
    (repo.root / "sub").mkdir()
    repo.commit(repo.root, {"sub/x.py": "X = 1\n"}, "Sub")
    repo.push_main()
    feat = repo.add("tmp/wt-feat", "-b", "feat")
    repo.commit(feat, {"sub/x.py": "X = 2\n", "top.py": "T = 1\n"}, "Both")
    repo.commit(repo.root, {"sub/x.py": "X = 2\n"}, "Only the subdirectory's half")
    repo.push_main()
    assert repo.reasons(repo.root / "sub")["tmp/wt-feat"] == "1 commit(s) not landed"


def test_s002_8_a_commit_only_on_the_local_default_branch_has_not_landed(repo: Checkout) -> None:
    feat = repo.add("tmp/wt-feat", "-b", "feat")
    sha = repo.commit(feat, {"feature.py": "A = 1\n"}, "Feature")
    repo.git().run("merge", "-q", "--ff-only", sha)  # on local main, never pushed to origin
    assert repo.reasons()["tmp/wt-feat"] == "1 commit(s) not landed"


def test_s002_9_a_rebase_merged_branch_is_removable(repo: Checkout) -> None:
    feat = repo.add("tmp/wt-rebased", "-b", "rebased")
    one = repo.commit(feat, {"a.py": "A = 1\n"}, "One")
    two = repo.commit(feat, {"b.py": "B = 1\n", "CHANGELOG.md": changelog("Rebased (#1)")}, "Two")
    resolved = repo.add("tmp/wt-resolved", "-b", "resolved")
    repo.commit(resolved, {"c.py": "C = 1\n", "CHANGELOG.md": changelog("Resolved (#2)")}, "Three")
    repo.commit(repo.root, {"other.py": "O = 1\n"}, "Someone else first")
    repo.git().run("cherry-pick", one, two)
    repo.commit(repo.root, {"c.py": "C = 1\n", "CHANGELOG.md": changelog("Rebased (#1)", "Resolved (#2)")}, "Three")
    repo.push_main()
    reasons = repo.reasons()
    assert reasons["tmp/wt-rebased"] is None
    assert reasons["tmp/wt-resolved"] is None  # landed but for its CHANGELOG.md, resolved by hand


def test_s002_9_a_branch_merged_with_a_merge_commit_is_removable(repo: Checkout) -> None:
    feat = repo.add(".claude/worktrees/merged", "-b", "merged")
    repo.commit(feat, {"a.py": "A = 1\n"}, "One")
    repo.commit(feat, {"b.py": "B = 1\n"}, "Two")
    repo.commit(repo.root, {"other.py": "O = 1\n"}, "Someone else first")
    repo.git().run("merge", "-q", "--no-ff", "-m", "Merge merged", "merged")
    repo.push_main()
    assert repo.judge()[".claude/worktrees/merged"].verdict is Verdict.REMOVABLE


@pytest.mark.parametrize("hand_resolved", [False, True])
def test_s002_9_a_branch_squashed_from_several_commits_is_removable(repo: Checkout, hand_resolved: bool) -> None:
    feat = repo.add(".claude/worktrees/squashed", "-b", "squashed")
    repo.commit(feat, {"a.py": "A = 1\n"}, "One")
    repo.commit(feat, {"b.py": "B = 1\n", "CHANGELOG.md": changelog("Squashed (#1)")}, "Two")
    repo.commit(feat, {"a.py": "A = 2\n"}, "Three")
    if hand_resolved:
        repo.commit(repo.root, {"CHANGELOG.md": changelog("Other (#2)")}, "Other")
    clean = repo.git().ok("merge", "-q", "--squash", "squashed")
    assert clean is not hand_resolved  # both sides added to CHANGELOG.md: a person resolves it
    if hand_resolved:
        (repo.root / "CHANGELOG.md").write_text(changelog("Other (#2)", "Squashed (#1)"), encoding="utf-8")
        repo.git().run("add", "CHANGELOG.md")
    repo.commit(repo.root, {}, "Squashed (#1)")
    repo.push_main()
    judged = repo.judge()[".claude/worktrees/squashed"]
    assert (judged.verdict, judged.reason) == (Verdict.REMOVABLE, None)


def test_s002_9_a_branch_with_no_commits_of_its_own_is_removable(repo: Checkout) -> None:
    repo.add("tmp/wt-empty", "-b", "empty")
    assert repo.judge()["tmp/wt-empty"].verdict is Verdict.REMOVABLE


def test_s002_10_an_open_pull_request_keeps_a_worktree(repo: Checkout) -> None:
    repo.add("tmp/wt-head", "-b", "feat/head")
    upstream = repo.add("tmp/wt-up", "-b", "local-name")
    repo.git(upstream).run("push", "-q", "-u", "origin", "local-name:feat/remote-name")
    repo.add("tmp/wt-free", "-b", "feat/free")
    repo.github.pulls = [PullRequest(9, "feat/remote-name"), PullRequest(7, "feat/head"), PullRequest(8, "other")]
    reasons = repo.reasons()
    assert reasons["tmp/wt-head"] == "open PR #7"
    assert reasons["tmp/wt-up"] == "open PR #9"
    assert reasons["tmp/wt-free"] is None


def test_the_checks_run_in_the_spec_order(repo: Checkout) -> None:
    """A worktree failing several checks names the first: locked before young before dirty"""
    both = repo.add("tmp/wt-both", "-b", "both", hours=1)
    repo.git().run("worktree", "lock", "--reason", "busy", str(both))
    young = repo.add("tmp/wt-young", "-b", "young", hours=1)
    (young / "x.txt").write_text("x\n", encoding="utf-8")
    dirty = repo.add("tmp/wt-dirty", "-b", "dirty")
    repo.commit(dirty, {"y.py": "Y = 1\n"}, "Unlanded")
    (dirty / "x.txt").write_text("x\n", encoding="utf-8")
    repo.github.pulls = [PullRequest(1, "dirty")]
    reasons = repo.reasons()
    assert reasons == {
        ".": "main checkout",
        "tmp/wt-both": "locked: busy",
        "tmp/wt-young": "created 1h ago",
        "tmp/wt-dirty": "uncommitted changes",
    }


def fake_tool(tmp_path: Path, name: str, script: str) -> Path:
    """A directory holding an executable `name` and git, to put on PATH"""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    tool = bin_dir / name
    tool.write_text("#!/bin/sh\n" + script, encoding="utf-8")
    tool.chmod(tool.stat().st_mode | stat.S_IXUSR)
    git = shutil.which("git")
    assert git is not None
    if not (bin_dir / "git").exists():
        (bin_dir / "git").symlink_to(git)
    return bin_dir


@pytest.mark.parametrize(
    ("script", "message"),
    [
        ("echo 'not logged in' >&2\nexit 3\n", "exit 3: not logged in"),
        ('echo \'{"cwd": "/x"}\'\n', "not a JSON array"),
        ("echo 'sessions: none'\n", "printed no JSON"),
        ('echo \'[{"name": "a"}]\'\n', "without a string cwd"),
        ('echo \'[{"name": "a", "cwd": 7}]\'\n', "without a string cwd"),
        ("echo '[\"/x\"]'\n", "without a string cwd"),
    ],
)
def test_s002_11_a_failing_claude_agents_is_refused(tmp_path: Path, script: str, message: str) -> None:
    bin_dir = fake_tool(tmp_path, "claude", script)
    with pytest.raises(ReleaseError, match=message):
        ClaudeSessions(tmp_path, str(bin_dir / "claude")).live()


def test_s002_11_a_missing_claude_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ReleaseError, match="not on PATH"):
        ClaudeSessions(tmp_path, str(tmp_path / "no-claude")).live()


def test_s002_11_the_command_exits_2_and_removes_nothing_when_claude_fails(repo: Checkout, tmp_path: Path) -> None:
    repo.add(".claude/worktrees/done", "-b", "feat/done")
    before = repo.state()
    for script in (None, "exit 1\n", "echo '[{\"pid\": 1}]'\n"):
        bin_dir = tmp_path / "bin"
        shutil.rmtree(bin_dir, ignore_errors=True)
        if script is None:
            bin_dir.mkdir()
            git = shutil.which("git")
            assert git is not None
            (bin_dir / "git").symlink_to(git)
        else:
            fake_tool(tmp_path, "claude", script)
        env = {**os.environ, "PATH": str(bin_dir)}
        cmd = [sys.executable, "-c", "from shipmill.cli import run; run()", "--repo", str(repo.root), "worktrees"]
        proc = subprocess.run(cmd, capture_output=True, text=True, env=env)
        assert proc.returncode == 2, proc.stderr
        assert proc.stderr.startswith("shipmill: ") and "claude" in proc.stderr
        assert proc.stdout == ""
    assert repo.state() == before


def test_s002_11_a_failing_gh_pr_list_judges_nothing(repo: Checkout) -> None:
    repo.add(".claude/worktrees/done", "-b", "feat/done")
    before = repo.state()
    repo.github.pulls_error = "HTTP 502"
    with pytest.raises(ReleaseError, match="gh pr list failed: HTTP 502"):
        main(["--repo", str(repo.root), "worktrees"], repo.github, sessions=repo.sessions)
    assert repo.state() == before


def test_s002_11_gh_pr_list_fails_or_prints_bad_rows(tmp_path: Path) -> None:
    bin_dir = fake_tool(tmp_path, "gh", "echo 'HTTP 401' >&2\nexit 1\n")
    with pytest.raises(ReleaseError, match="HTTP 401"):
        GhCli(tmp_path, str(bin_dir / "gh")).open_pull_requests()
    fake_tool(tmp_path, "gh", 'echo \'[{"number": "7", "headRefName": "a"}]\'\n')
    with pytest.raises(ReleaseError, match="needs a number and a headRefName"):
        GhCli(tmp_path, str(bin_dir / "gh")).open_pull_requests()
    fake_tool(tmp_path, "gh", 'echo \'[{"number": 9, "headRefName": "b"}, {"number": 7, "headRefName": "a"}]\'\n')
    assert GhCli(tmp_path, str(bin_dir / "gh")).open_pull_requests() == [PullRequest(7, "a"), PullRequest(9, "b")]


def test_worktree_list_parses_every_kind_of_record() -> None:
    text = "\0".join(
        [
            "worktree /r",
            "bare",
            "",
            "worktree /r/a",
            "HEAD " + "1" * 40,
            "branch refs/heads/feat/a",
            "locked",
            "",
            "worktree /r/b",
            "HEAD " + "2" * 40,
            "detached",
            "locked claude agent b (pid 1)",
            "prunable gitdir file points to non-existent location",
            "",
            "",
        ]
    )
    bare, a, b = parse_worktrees(text)
    assert (bare.bare, bare.head, bare.branch) == (True, None, None)
    assert (a.branch, a.locked, a.prunable) == ("feat/a", "", False)
    assert (b.branch, b.locked, b.prunable) == (None, "claude agent b (pid 1)", True)


@pytest.mark.parametrize(
    "text",
    [
        "",
        "HEAD 1\0\0",
        "worktree /r\0HEAD 1\0\0",  # neither on a branch nor detached
        "worktree /r\0HEAD 1\0detached\0sparse\0\0",
    ],
)
def test_a_worktree_list_shipmill_cannot_read_is_refused(text: str) -> None:
    with pytest.raises(ReleaseError):
        parse_worktrees(text)


def branches(c: Checkout) -> set[str]:
    return set(c.git().run("for-each-ref", "--format=%(refname:short)", "refs/heads/").split())


def remote_branches(c: Checkout) -> set[str]:
    return set(c.git().run("for-each-ref", "--format=%(refname:short)", "refs/remotes/").split())


def rows_by_path(out: str) -> dict[str, list[str]]:
    rows = [line.split(None, 4) for line in out.splitlines()[1:]]
    return {r[1]: r for r in rows}


def test_s002_12_prune_removes_each_removable_worktree_and_its_branch_only(
    repo: Checkout, capsys: pytest.CaptureFixture[str]
) -> None:
    done = repo.add(".claude/worktrees/done", "-b", "feat/done")
    pushed = repo.add("tmp/wt-pushed", "-b", "feat/pushed")
    repo.git(pushed).run("push", "-q", "-u", "origin", "feat/pushed")
    young = repo.add("tmp/wt-young", "-b", "feat/young", hours=5)
    open_ = repo.add("tmp/wt-open", "-b", "feat/open")
    repo.commit(open_, {"feature.py": "A = 1\n"}, "Unlanded")
    gate = repo.add("tmp/shipmill-gate", "--detach")
    repo.git().run("branch", "landed-alone", "origin/main")  # landed, but no worktree holds it
    remotes = remote_branches(repo)
    assert "origin/feat/pushed" in remotes
    assert main(["--repo", str(repo.root), "worktrees", "--prune"], repo.github, sessions=repo.sessions) == 0
    assert rows_by_path(capsys.readouterr().out) == {
        ".": ["KEPT", ".", "main", "-", "main checkout"],
        ".claude/worktrees/done": ["REMOVED", ".claude/worktrees/done", "feat/done", "3d"],
        "tmp/wt-pushed": ["REMOVED", "tmp/wt-pushed", "feat/pushed", "3d"],
        "tmp/wt-young": ["KEPT", "tmp/wt-young", "feat/young", "5h", "created 5h ago"],
        "tmp/wt-open": ["KEPT", "tmp/wt-open", "feat/open", "3d", "1 commit(s) not landed"],
        "tmp/shipmill-gate": ["KEPT", "tmp/shipmill-gate", "-", "3d", "not a shipmill worktree"],
    }
    assert not done.exists() and not pushed.exists()
    assert young.is_dir() and open_.is_dir() and gate.is_dir()
    assert branches(repo) == {"main", "feat/young", "feat/open", "landed-alone"}
    assert remote_branches(repo) == remotes
    assert "refs/heads/feat/pushed" in repo.git().run("ls-remote", "--heads", "origin")
    listed = repo.git().run("worktree", "list", "--porcelain")
    assert "wt-pushed" not in listed and ".claude/worktrees/done" not in listed


def test_s002_12_prune_json_marks_the_removed_rows(repo: Checkout, capsys: pytest.CaptureFixture[str]) -> None:
    repo.add(".claude/worktrees/done", "-b", "feat/done")
    repo.add("tmp/wt-detached", "--detach")
    argv = ["--repo", str(repo.root), "worktrees", "--prune", "--json"]
    assert main(argv, repo.github, sessions=repo.sessions) == 0
    rows = {row["path"]: row for row in json.loads(capsys.readouterr().out)["worktrees"]}
    assert (rows[".claude/worktrees/done"]["verdict"], rows[".claude/worktrees/done"]["reason"]) == ("REMOVED", None)
    assert rows["tmp/wt-detached"]["verdict"] == "KEPT"
    assert rows["."]["verdict"] == "KEPT"


def test_s002_13_dry_run_prints_would_remove_and_touches_nothing(
    repo: Checkout, capsys: pytest.CaptureFixture[str]
) -> None:
    done = repo.add(".claude/worktrees/done", "-b", "feat/done")
    repo.add("tmp/wt-young", "-b", "feat/young", hours=5)
    before = repo.state()
    argv = ["--repo", str(repo.root), "worktrees", "--prune", "--dry-run"]
    assert main(argv, repo.github, sessions=repo.sessions) == 0
    verdicts = {path: row[0] for path, row in rows_by_path(capsys.readouterr().out).items()}
    assert verdicts == {".": "KEPT", ".claude/worktrees/done": "WOULD_REMOVE", "tmp/wt-young": "KEPT"}
    assert main([*argv, "--json"], repo.github, sessions=repo.sessions) == 0
    found = json.loads(capsys.readouterr().out)["worktrees"]
    assert {row["path"]: row["verdict"] for row in found}[".claude/worktrees/done"] == "WOULD_REMOVE"
    assert done.is_dir()
    assert repo.state() == before


def test_s002_13_dry_run_without_prune_exits_2_before_judging(repo: Checkout) -> None:
    repo.add(".claude/worktrees/done", "-b", "feat/done")
    before = repo.state()
    repo.github.pulls_error = "gh must not be called"
    with pytest.raises(ReleaseError, match="--dry-run goes with --prune"):
        main(["--repo", str(repo.root), "worktrees", "--dry-run"], repo.github, sessions=repo.sessions)
    cmd = [sys.executable, "-c", "from shipmill.cli import run; run()", "--repo", str(repo.root), "worktrees"]
    proc = subprocess.run([*cmd, "--dry-run"], capture_output=True, text=True)
    assert proc.returncode == 2 and "--dry-run goes with --prune" in proc.stderr
    assert repo.state() == before


def removable_in_order(repo: Checkout, *names: str) -> list[Path]:
    """Linked worktrees under tmp/wt-<name>, each REMOVABLE, in the order the prune visits them"""
    paths = {repo.add(f"tmp/wt-{n}", "-b", n).resolve() for n in names}
    order = [j.worktree.path.resolve() for j in repo.judge().values() if j.verdict is Verdict.REMOVABLE]
    assert set(order) == paths
    return order


def test_s002_14_a_branch_git_cannot_delete_stops_the_prune(repo: Checkout) -> None:
    first, second, third = removable_in_order(repo, "a", "b", "c")
    held = second.name.removeprefix("wt-")
    common = Path(repo.git().run("rev-parse", "--path-format=absolute", "--git-common-dir").strip())
    (common / "refs/heads" / f"{held}.lock").write_text("", encoding="utf-8")  # another git holds the ref
    root = repo.root.resolve()
    message = (
        rf"(?s)prune stopped at {os.path.relpath(second, root)} \({held}\): git branch -D {held} failed: .+; "
        rf"removed before it: {os.path.relpath(first, root)}$"
    )
    with pytest.raises(ReleaseError, match=message):
        main(["--repo", str(repo.root), "worktrees", "--prune"], repo.github, sessions=repo.sessions)
    assert not first.exists() and not second.exists()  # its worktree went before its branch failed
    assert third.is_dir()
    assert branches(repo) == {"main", held, third.name.removeprefix("wt-")}


def test_s002_14_a_worktree_git_refuses_to_remove_stops_the_prune(repo: Checkout) -> None:
    """A worktree that changed between the judgement and its removal: git refuses it
    without --force, and nothing after it is touched"""
    first, second, third = removable_in_order(repo, "a", "b", "c")
    judged = judge(repo.git(), repo.github, repo.sessions, dt.datetime.now(dt.UTC))
    (second / "late.txt").write_text("written after the check\n", encoding="utf-8")
    rel = os.path.relpath(second, repo.root.resolve())
    with pytest.raises(ReleaseError, match=rf"prune stopped at {rel} \(.+\): git worktree remove .+ failed: .+"):
        prune(repo.git(), judged, dry_run=False)
    assert not first.exists()
    assert (second / "late.txt").is_file() and third.is_dir()
    assert branches(repo) == {"main", second.name.removeprefix("wt-"), third.name.removeprefix("wt-")}


@pytest.mark.parametrize("flags", [["--prune"], ["--prune", "--dry-run"]])
def test_s002_11_prune_removes_nothing_when_claude_or_gh_fails(repo: Checkout, flags: list[str]) -> None:
    repo.add(".claude/worktrees/done", "-b", "feat/done")
    before = repo.state()
    argv = ["--repo", str(repo.root), "worktrees", *flags]
    repo.github.pulls_error = "HTTP 502"
    with pytest.raises(ReleaseError, match="gh pr list failed: HTTP 502"):
        main(argv, repo.github, sessions=repo.sessions)
    repo.github.pulls_error = ""
    with pytest.raises(ReleaseError, match="claude agents"):
        main(argv, repo.github, sessions=ClaudeSessions(repo.root, str(repo.root / "no-claude")))
    assert repo.state() == before
