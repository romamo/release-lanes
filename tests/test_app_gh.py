"""Spec 012: `shipmill app-token` with no checkout, and `shipmill gh`, which runs gh as the
repo's App when [agents] app_id is set and as the host's login when it isn't. No test
reaches the network: GitHub is MintApi and openssl FakeSigner"""

import datetime as dt
import json
import os
import stat
import subprocess
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from shipmill.app import CACHE, cache_outside, load_token
from shipmill.app_gh import named_repo
from shipmill.cli import main
from shipmill.errors import ReleaseError
from shipmill.gate import state_dir
from shipmill.gitrepo import Git

from .test_app import APP_ID, GRANTED, REPO, FakeSigner, key_file
from .test_app_token import FAKE_GH, MintApi, executable
from .test_gate import write_config

ORIGIN_ELSEWHERE = "https://github.com/romamo/elsewhere.git"


@dataclass
class Runner:
    """Stands in for gh: records each argv and env, answers with code"""

    code: int = 7
    calls: list[tuple[list[str], dict[str, str]]] = field(default_factory=list)

    def __call__(self, argv: Sequence[str], env: Mapping[str, str]) -> int:
        self.calls.append((list(argv), dict(env)))
        return self.code


@dataclass(frozen=True)
class Host:
    fakes: Path  # holds the gh on PATH
    record: Path
    home: Path

    @property
    def gh(self) -> str:
        return str(self.fakes / "gh")

    def env(self, **extra: str) -> dict[str, str]:
        return {"PATH": os.pathsep.join([str(self.fakes), "/usr/bin", "/bin"]), "RECORD": str(self.record), **extra}


@pytest.fixture
def host(tmp_path: Path) -> Host:
    fakes = tmp_path / "fakes"
    fakes.mkdir()
    executable(fakes / "gh", FAKE_GH)
    record = tmp_path / "record"
    record.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    return Host(fakes, record, home)


def checkout(tmp_path: Path, config: str | None, origin: str = f"https://github.com/{REPO}.git") -> Path:
    root = tmp_path / "demo"
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    Git(root).run("remote", "add", "origin", origin)
    if config is not None:
        write_config(root, config)
    return root


WITH_APP = f'[agents]\nprompt = "/t"\napp_id = {APP_ID}\n'


def gh(root: Path, args: list[str], host: Host, api: MintApi, runner: Runner, **env: str) -> int:
    return main(
        ["--repo", str(root), "gh", *args],
        api=api,
        signer=FakeSigner(),
        environ=host.env(**env),
        home=host.home,
        gh_runner=runner,
    )


def outside(tmp_path: Path) -> Path:
    folder = tmp_path / "nowhere"
    folder.mkdir()
    assert not Git(folder).ok("rev-parse", "--git-dir")  # not in any checkout
    return folder


def token_args(root: Path, tmp_path: Path) -> list[str]:
    return ["--repo", str(root), "app-token", REPO, "--app-id", str(APP_ID), "--app-key", str(key_file(tmp_path))]


# S-012-1


@pytest.mark.parametrize("xdg", [True, False])
def test_s012_1_app_token_outside_a_checkout_caches_under_the_xdg_cache(
    tmp_path: Path, host: Host, capsys: pytest.CaptureFixture[str], xdg: bool
) -> None:
    folder = outside(tmp_path)
    environ = {"XDG_CACHE_HOME": str(tmp_path / "xdg")} if xdg else {}
    api = MintApi(expires_from=dt.datetime.now(dt.UTC))
    assert main(token_args(folder, tmp_path), api=api, signer=FakeSigner(), environ=environ, home=host.home) == 0
    assert capsys.readouterr() == ("token-1\n", "")
    base = tmp_path / "xdg" if xdg else host.home / ".cache"
    cache = base / "shipmill" / "romamo" / "demo" / CACHE
    assert stat.S_IMODE(cache.stat().st_mode) == 0o600
    assert [p.name for p in cache.parent.iterdir()] == [CACHE]
    assert json.loads(cache.read_text(encoding="utf-8"))["repository"] == REPO
    # the same reuse rule: a second call asks GitHub nothing more
    assert main(token_args(folder, tmp_path), api=api, signer=FakeSigner(), environ=environ, home=host.home) == 0
    assert capsys.readouterr().out == "token-1\n"
    assert len(api.posts) == 1


def test_s012_1_an_empty_xdg_cache_home_means_the_home_cache(tmp_path: Path) -> None:
    home = tmp_path / "home"
    assert cache_outside(REPO, {"XDG_CACHE_HOME": ""}, home) == home / ".cache" / "shipmill" / "romamo" / "demo" / CACHE


def test_s012_1_inside_a_checkout_the_cache_is_spec_004s(
    tmp_path: Path, host: Host, capsys: pytest.CaptureFixture[str]
) -> None:
    root = checkout(tmp_path, None)
    environ = {"XDG_CACHE_HOME": str(tmp_path / "xdg")}
    api = MintApi(expires_from=dt.datetime.now(dt.UTC))
    assert main(token_args(root, tmp_path), api=api, signer=FakeSigner(), environ=environ, home=host.home) == 0
    assert capsys.readouterr().out == "token-1\n"
    assert load_token(state_dir(Git(root)) / CACHE) is not None
    assert not (tmp_path / "xdg").exists()


@pytest.mark.parametrize(
    ("repo", "environ", "message"),
    [
        ("../demo", {}, "owner/name of letters"),
        ("romamo/..", {}, "owner/name of letters"),
        ("rom amo/demo", {}, "owner/name of letters"),
        (REPO, {"XDG_CACHE_HOME": "relative/cache"}, "XDG_CACHE_HOME must be an absolute path"),
    ],
)
def test_s012_1_the_cache_path_never_leaves_the_cache_folder(
    tmp_path: Path, repo: str, environ: dict[str, str], message: str
) -> None:
    with pytest.raises(ReleaseError, match=message):
        cache_outside(repo, environ, tmp_path)


def test_a_missing_repo_folder_is_refused(tmp_path: Path, host: Host) -> None:
    with pytest.raises(ReleaseError, match="is not a folder"):
        main(token_args(tmp_path / "typo", tmp_path), api=MintApi(), signer=FakeSigner(), environ={}, home=host.home)


# S-012-2


def test_s012_2_gh_runs_with_exactly_its_arguments_and_origins_token(
    tmp_path: Path, host: Host, capsys: pytest.CaptureFixture[str]
) -> None:
    root = checkout(tmp_path, WITH_APP)
    key_file(tmp_path)
    args = ["pr", "create", "--title", "two words", "--body", "", "--", "--repo", "x/y", "--app-key", "k", "-h"]
    api = MintApi()
    runner = Runner()
    assert gh(root, ["--app-key", str(tmp_path / "app.pem"), *args], host, api, runner) == 7
    [(argv, env)] = runner.calls
    assert argv == [host.gh, *args]  # past `--` nothing is read as a flag
    assert env["GH_TOKEN"] == "token-1"
    assert env["PATH"] == host.env()["PATH"]
    [(_, _, body)] = api.posts
    assert body["repositories"] == ["demo"]
    assert capsys.readouterr() == ("", "")  # the token never reaches output


@pytest.mark.parametrize(
    "flag",
    [["-R", REPO], ["--repo", REPO], [f"--repo={REPO}"], [f"-R{REPO}"], [f"-R={REPO}"], ["-R", f"github.com/{REPO}"]],
)
def test_s012_2_the_token_is_limited_to_the_repo_gh_names(tmp_path: Path, host: Host, flag: list[str]) -> None:
    root = checkout(tmp_path, WITH_APP, origin=ORIGIN_ELSEWHERE)
    key_file(tmp_path)
    args = ["issue", "comment", "250", *flag, "--body-file", "tmp/comment.md"]
    api = MintApi()
    runner = Runner()
    assert gh(root, ["--app-key", str(tmp_path / "app.pem"), *args], host, api, runner) == 7
    [(argv, env)] = runner.calls
    assert argv == [host.gh, *args]
    assert env["GH_TOKEN"] == "token-1"
    assert [path for path, _ in api.calls][:2] == ["/app", f"/repos/{REPO}/installation"]


def test_s012_2_the_default_key_and_an_empty_gh_token_replaced(tmp_path: Path, host: Host) -> None:
    root = checkout(tmp_path, WITH_APP)
    key = host.home / ".config" / "shipmill" / f"app-{APP_ID}.pem"
    key.parent.mkdir(parents=True)
    key.write_text("not a real key\n", encoding="utf-8")
    key.chmod(0o600)
    runner = Runner(code=0)
    assert gh(root, ["api", "user"], host, MintApi(), runner, GH_TOKEN="") == 0
    [(argv, env)] = runner.calls
    assert (argv, env["GH_TOKEN"]) == ([host.gh, "api", "user"], "token-1")


def test_s012_2_the_real_gh_on_path_gets_the_token_and_its_code_is_shipmills(tmp_path: Path, host: Host) -> None:
    """End to end through subprocess: the fake gh records its arguments and GH_TOKEN, exits 7"""
    root = checkout(tmp_path, WITH_APP)
    key_file(tmp_path)
    args = ["pr", "comment", "1", "--body", "it's -R x"]
    code = main(
        ["--repo", str(root), "gh", "--app-key", str(tmp_path / "app.pem"), *args],
        api=MintApi(),
        signer=FakeSigner(),
        environ=host.env(),
        home=host.home,
    )
    assert code == 7
    assert (host.record / "gh-args").read_text() == "".join(f"{a}\n" for a in args)
    assert (host.record / "gh-token").read_text() == "token-1"


def test_s012_2_a_repo_flag_off_github_is_refused(tmp_path: Path, host: Host) -> None:
    root = checkout(tmp_path, WITH_APP)
    runner = Runner()
    with pytest.raises(ReleaseError, match="owner/name on github.com"):
        gh(root, ["pr", "list", "-R", "ghe.example.com/a/b"], host, MintApi(), runner)
    assert runner.calls == []


@pytest.mark.parametrize(
    ("args", "repo"),
    [
        (["pr", "list"], None),
        (["pr", "list", "-R", "a/one", "--repo", "a/two"], "a/two"),  # the last, as gh takes it
        (["api", "--", "-R", "a/b"], None),
        (["pr", "list", "-Ra/b"], "a/b"),
    ],
)
def test_s012_2_named_repo_reads_only_the_repo_flag(args: list[str], repo: str | None) -> None:
    assert named_repo(args) == repo


def test_s012_2_a_repo_flag_without_a_value_is_refused() -> None:
    with pytest.raises(ReleaseError, match="needs a repo"):
        named_repo(["pr", "list", "-R"])


# S-012-3


def _no_permission() -> MintApi:
    return MintApi(permissions={k: v for k, v in GRANTED.items() if k != "issues"})


@pytest.mark.parametrize(
    ("key_mode", "api", "cause", "fix"),
    [
        (None, MintApi(), "no App private key at", "download it from the App's settings"),
        (0o644, MintApi(), "readable by group or others", "chmod 600"),
        (0o600, MintApi(installed=False), "app demo-agent is not installed on romamo/demo", f"app-install {REPO}"),
        (0o600, _no_permission(), "lacks these permissions: Issues: write", "grant them in the App's settings"),
    ],
)
def test_s012_3_a_failure_to_get_the_token_exits_2_and_never_runs_gh(
    tmp_path: Path, host: Host, key_mode: int | None, api: MintApi, cause: str, fix: str
) -> None:
    root = checkout(tmp_path, WITH_APP)
    key = tmp_path / "app.pem" if key_mode is None else key_file(tmp_path, key_mode)
    runner = Runner()
    with pytest.raises(ReleaseError) as raised:  # cli.run prints it and exits 2
        gh(root, ["--app-key", str(key), "issue", "comment", "1", "--body", "x"], host, api, runner)
    assert cause in str(raised.value)
    assert fix in str(raised.value)
    assert runner.calls == []
    assert api.posts == []


def test_s012_3_the_exit_code_is_2(tmp_path: Path, host: Host) -> None:
    """Through `python -m shipmill`, a missing key exits 2 and gh never runs"""
    root = checkout(tmp_path, WITH_APP)
    env = {**host.env(), "HOME": str(host.home)}
    done = subprocess.run(
        [sys.executable, "-m", "shipmill", "--repo", str(root), "gh", "pr", "list"],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    assert done.returncode == 2
    assert "no App private key" in done.stderr
    assert not (host.record / "gh-args").exists()


# S-012-4


@pytest.mark.parametrize(
    "config",
    [None, '[agents]\nprompt = "/t"\n', "[release]\n", "outside"],
)
def test_s012_4_without_app_id_gh_runs_as_the_host_with_no_gh_token_and_no_mint(
    tmp_path: Path, host: Host, capsys: pytest.CaptureFixture[str], config: str | None
) -> None:
    root = outside(tmp_path) if config == "outside" else checkout(tmp_path, config)
    api = MintApi()
    signer = FakeSigner()
    runner = Runner()
    args = ["issue", "comment", "250", "-R", REPO, "--body-file", "tmp/comment.md", "--", "--app-key", "x"]
    code = main(
        ["--repo", str(root), "gh", *args],
        api=api,
        signer=signer,
        environ=host.env(GH_TOKEN=""),
        home=host.home,
        gh_runner=runner,
    )
    assert code == 7
    [(argv, env)] = runner.calls
    assert argv == [host.gh, *args]
    assert "GH_TOKEN" not in env
    assert (api.calls, api.posts, signer.signed) == ([], [], [])
    assert capsys.readouterr() == ("", "")


def test_without_app_id_a_callers_own_gh_token_is_left_as_it_is(tmp_path: Path, host: Host) -> None:
    root = checkout(tmp_path, None)
    runner = Runner()
    assert gh(root, ["auth", "status"], host, MintApi(), runner, GH_TOKEN="the-callers") == 7
    assert runner.calls[0][1]["GH_TOKEN"] == "the-callers"


def test_without_app_id_an_app_key_is_refused(tmp_path: Path, host: Host) -> None:
    root = checkout(tmp_path, None)
    runner = Runner()
    with pytest.raises(ReleaseError, match="no \\[agents\\] app_id"):
        gh(root, ["--app-key", "k.pem", "pr", "list"], host, MintApi(), runner)
    assert runner.calls == []


# the passthrough


@pytest.mark.parametrize("args", [[], ["--help"], ["--version"], ["-R"], ["gh", "--repo", "x"]])
def test_every_argument_after_gh_goes_to_gh(tmp_path: Path, host: Host, args: list[str]) -> None:
    root = checkout(tmp_path, None)
    runner = Runner()
    assert gh(root, args, host, MintApi(), runner) == 7
    assert runner.calls[0][0] == [host.gh, *args]


def test_only_repo_may_come_before_gh(tmp_path: Path, host: Host) -> None:
    runner = Runner()
    with pytest.raises(ReleaseError, match="only --repo PATH may come before gh"):
        main(["--", "gh"], environ=host.env(), home=host.home, gh_runner=runner)
    with pytest.raises(SystemExit) as exited:  # argparse's own refusal, also exit 2
        main(["--", "gh", "pr", "list"], environ=host.env(), home=host.home, gh_runner=runner)
    assert exited.value.code == 2
    assert runner.calls == []
