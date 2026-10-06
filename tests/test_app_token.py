"""Spec 004, Tokens: `shipmill app-token`, its cache, git's credential protocol, and the
session's helpers. A token reaches stdout only as the command's answer"""

import datetime as dt
import io
import json
import os
import shlex
import stat
import subprocess
import sys
import threading
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from shipmill.app import (
    CACHE,
    PERMISSIONS,
    Answer,
    Token,
    app_token,
    credential,
    load_token,
    resolve_gh,
    save_token,
    write_helpers,
)
from shipmill.cli import main
from shipmill.errors import ReleaseError
from shipmill.gate import state_dir
from shipmill.gitrepo import Git

from .test_app import APP_ID, REPO, FakeApi, FakeSigner, key_file
from .test_gate import NOW


@pytest.fixture
def checkout(tmp_path: Path) -> Git:
    root = tmp_path / "demo"
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    return Git(root)


ACCESS_TOKENS = "/app/installations/77/access_tokens"
HOUR = dt.timedelta(hours=1)


@dataclass
class MintApi(FakeApi):
    """FakeApi that also mints: token-1, token-2, ... each valid for an hour from expires_from"""

    expires_from: dt.datetime = NOW
    status: int = 201
    repositories: list[str] | None = field(default_factory=lambda: [REPO])
    posts: list[tuple[str, str, Mapping[str, object]]] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def post(self, path: str, token: str, body: Mapping[str, object]) -> Answer:
        with self.lock:
            self.posts.append((path, token, body))
            count = len(self.posts)
        if path != ACCESS_TOKENS:
            raise AssertionError(f"unexpected POST {path}")
        if self.status != 201:
            return Answer(self.status, '{"message": "Validation Failed"}')
        answer: dict[str, object] = {
            "token": f"token-{count}",
            "expires_at": (self.expires_from + HOUR).isoformat().replace("+00:00", "Z"),
        }
        if self.repositories is not None:
            answer["repositories"] = [{"full_name": name} for name in self.repositories]
        return Answer(201, json.dumps(answer))


def cache_of(git: Git) -> Path:
    return state_dir(git) / CACHE


def mint_at(git: Git, tmp_path: Path, api: MintApi, now: dt.datetime) -> str:
    return app_token(cache_of(git), REPO, APP_ID, key_file(tmp_path), now, FakeSigner(), api)


# S-004-8


def test_s004_8_the_token_is_limited_to_the_one_repository(checkout: Git, tmp_path: Path) -> None:
    api = MintApi()
    assert mint_at(checkout, tmp_path, api, NOW) == "token-1"
    [(path, bearer, body)] = api.posts
    assert path == ACCESS_TOKENS
    assert bearer.count(".") == 2  # the App's JWT, as for the installation read
    assert body == {"repositories": ["demo"], "permissions": {p.key: str(p.access) for p in PERMISSIONS}}


def test_s004_8_the_cache_is_written_with_mode_0600_and_no_stray_files(checkout: Git, tmp_path: Path) -> None:
    mint_at(checkout, tmp_path, MintApi(), NOW)
    cache = cache_of(checkout)
    assert stat.S_IMODE(cache.stat().st_mode) == 0o600
    assert [p.name for p in cache.parent.iterdir()] == [CACHE]
    assert json.loads(cache.read_text(encoding="utf-8")) == {
        "app_id": APP_ID,
        "repository": REPO,
        "expires_at": (NOW + HOUR).isoformat(),
        "token": "token-1",
    }


@pytest.mark.parametrize(
    ("later", "token"),
    [
        (dt.timedelta(minutes=0), "token-1"),
        (dt.timedelta(minutes=50), "token-1"),  # exactly 10 minutes left: reused
        (dt.timedelta(minutes=50, seconds=1), "token-2"),
        (dt.timedelta(hours=2), "token-2"),
    ],
)
def test_s004_8_reused_while_10_minutes_remain_then_minted(
    checkout: Git, tmp_path: Path, later: dt.timedelta, token: str
) -> None:
    api = MintApi()
    mint_at(checkout, tmp_path, api, NOW)
    api.expires_from = NOW + later
    assert mint_at(checkout, tmp_path, api, NOW + later) == token
    assert len(api.posts) == int(token[-1])
    expires = NOW + HOUR if token == "token-1" else NOW + later + HOUR
    assert load_token(cache_of(checkout)) == Token(token, expires, REPO, APP_ID)


def test_s004_8_reuse_reads_no_key_and_asks_github_nothing(checkout: Git, tmp_path: Path) -> None:
    save_token(cache_of(checkout), Token("cached", NOW + HOUR, REPO, APP_ID))
    api = MintApi()
    key = tmp_path / "missing.pem"
    assert app_token(cache_of(checkout), REPO, APP_ID, key, NOW, FakeSigner(), api) == "cached"
    assert (api.calls, api.posts) == ([], [])


@pytest.mark.parametrize(("repo", "app_id"), [("romamo/other", APP_ID), (REPO, APP_ID + 1)])
def test_s004_8_a_token_for_another_repo_or_app_is_not_reused(
    checkout: Git, tmp_path: Path, repo: str, app_id: int
) -> None:
    save_token(cache_of(checkout), Token("cached", NOW + HOUR, repo, app_id))
    assert mint_at(checkout, tmp_path, MintApi(), NOW) == "token-1"


@pytest.mark.parametrize(
    ("text", "reason"),
    [
        ("{not json", "not JSON"),
        ("[]", "not a JSON object"),
        ('{"token": "", "repository": "romamo/demo", "app_id": 1, "expires_at": "2026-10-04T13:00:00+00:00"}', "token"),
        ('{"token": "t", "repository": 3, "app_id": 1, "expires_at": "2026-10-04T13:00:00+00:00"}', "repository"),
        ('{"token": "t", "repository": "romamo/demo", "app_id": true, "expires_at": "x"}', "app_id"),
        ('{"token": "t", "repository": "romamo/demo", "app_id": 1, "expires_at": "2026-10-04T13:00:00"}', "offset"),
    ],
)
def test_s004_8_a_malformed_cache_exits_2_naming_its_path(
    checkout: Git, tmp_path: Path, capsys: pytest.CaptureFixture[str], text: str, reason: str
) -> None:
    cache = cache_of(checkout)
    cache.parent.mkdir(parents=True)
    cache.write_text(text, encoding="utf-8")
    api = MintApi()
    with pytest.raises(ReleaseError, match=reason) as raised:
        main(cli_args(checkout, tmp_path), api=api, signer=FakeSigner())
    assert str(cache) in str(raised.value)
    assert "delete it" in str(raised.value)
    assert '"t"' not in str(raised.value)  # the message never quotes the file
    assert (api.calls, api.posts, capsys.readouterr().out) == ([], [], "")


@pytest.mark.parametrize(
    ("api", "message"),
    [
        (MintApi(status=422), "POST /app/installations/77/access_tokens for app_id 123456 answered 422"),
        (MintApi(repositories=None), "not limited to romamo/demo"),
        (MintApi(repositories=[REPO, "romamo/other"]), "not limited to romamo/demo"),
        (MintApi(repositories=["romamo/other"]), "another repository than romamo/demo"),
        (MintApi(installed=False), "app demo-agent is not installed on romamo/demo"),
    ],
)
def test_s004_8_a_failed_mint_exits_2_and_caches_nothing(
    checkout: Git, tmp_path: Path, capsys: pytest.CaptureFixture[str], api: MintApi, message: str
) -> None:
    with pytest.raises(ReleaseError, match=message) as raised:
        main(cli_args(checkout, tmp_path), api=api, signer=FakeSigner())
    assert "token-" not in str(raised.value)
    assert not cache_of(checkout).exists()
    assert capsys.readouterr().out == ""


def test_s004_8_a_bad_key_mints_nothing(checkout: Git, tmp_path: Path) -> None:
    api = MintApi()
    with pytest.raises(ReleaseError, match="readable by group or others"):
        app_token(cache_of(checkout), REPO, APP_ID, key_file(tmp_path, 0o644), NOW, FakeSigner(), api)
    assert (api.calls, api.posts) == ([], [])


def test_s004_8_helpers_minting_at_once_each_end_with_a_valid_token(checkout: Git, tmp_path: Path) -> None:
    api = MintApi()
    key = key_file(tmp_path)
    got: list[str] = []

    def call() -> None:
        got.append(app_token(cache_of(checkout), REPO, APP_ID, key, NOW, FakeSigner(), api))

    threads = [threading.Thread(target=call) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    minted = {f"token-{n}" for n in range(1, len(api.posts) + 1)}
    assert len(got) == 8 and set(got) <= minted
    cached = load_token(cache_of(checkout))
    assert cached is not None and cached.value in minted
    assert [p.name for p in cache_of(checkout).parent.iterdir()] == [CACHE]


def cli_args(git: Git, tmp_path: Path, *extra: str) -> list[str]:
    return [
        "--repo",
        str(git.root),
        "app-token",
        REPO,
        "--app-id",
        str(APP_ID),
        "--app-key",
        str(key_file(tmp_path)),
        *extra,
    ]


def test_s004_8_the_command_prints_the_token_alone(
    checkout: Git, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert (
        main(cli_args(checkout, tmp_path), api=MintApi(expires_from=dt.datetime.now(dt.UTC)), signer=FakeSigner()) == 0
    )
    assert capsys.readouterr() == ("token-1\n", "")


@pytest.mark.parametrize(("app_id", "message"), [("0", "--app-id must be 1 or more, got 0")])
def test_a_bad_app_id_is_refused(checkout: Git, app_id: str, message: str) -> None:
    with pytest.raises(ReleaseError, match=message):
        main(["--repo", str(checkout.root), "app-token", REPO, "--app-id", app_id], api=MintApi(), signer=FakeSigner())


@pytest.mark.parametrize("repo", ["demo", "romamo/", "/demo", "a/b/c"])
def test_a_repo_that_is_not_owner_name_is_refused(checkout: Git, tmp_path: Path, repo: str) -> None:
    with pytest.raises(ReleaseError, match="the repo is owner/name"):
        app_token(cache_of(checkout), repo, APP_ID, key_file(tmp_path), NOW, FakeSigner(), MintApi())


def test_a_tokens_repr_leaves_the_value_out() -> None:
    assert "secret" not in repr(Token("secret", NOW, REPO, APP_ID))


# S-004-9


GET = 'protocol=https\nhost=github.com\npath=romamo/demo.git\nwwwauth[]=Basic realm="GitHub"\n\n'


def test_s004_9_get_answers_x_access_token_and_the_token(
    checkout: Git, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    api = MintApi(expires_from=dt.datetime.now(dt.UTC))
    args = cli_args(checkout, tmp_path, "--git-credential", "get")
    assert main(args, api=api, signer=FakeSigner(), stdin=io.StringIO(GET)) == 0
    assert capsys.readouterr() == ("username=x-access-token\npassword=token-1\n", "")


@pytest.mark.parametrize("operation", ["store", "erase", "capability"])
def test_s004_9_store_erase_and_others_print_nothing_and_exit_0(
    checkout: Git, tmp_path: Path, capsys: pytest.CaptureFixture[str], operation: str
) -> None:
    api = MintApi()
    request = GET.replace("\n\n", "\nusername=x-access-token\npassword=token-1\n\n")
    args = cli_args(checkout, tmp_path, "--git-credential", operation)
    assert main(args, api=api, signer=FakeSigner(), stdin=io.StringIO(request)) == 0
    assert capsys.readouterr() == ("", "")
    assert (api.calls, api.posts) == ([], [])


@pytest.mark.parametrize(
    "request_text",
    ["protocol=https\nhost=gitlab.com\n\n", "protocol=http\nhost=github.com\n\n", "host=github.com\n", ""],
)
def test_s004_9_get_for_another_host_answers_nothing_and_mints_nothing(request_text: str) -> None:
    def never() -> str:
        raise AssertionError("minted for a request it should not answer")

    assert credential("get", request_text, never) == ""


def test_s004_9_get_reads_the_request_up_to_its_blank_line() -> None:
    text = "protocol=https\nhost=github.com\n\nhost=gitlab.com\n"
    assert credential("get", text, lambda: "t") == "username=x-access-token\npassword=t\n"


def test_s004_9_a_request_line_without_equals_is_refused() -> None:
    with pytest.raises(ReleaseError, match="not key=value"):
        credential("get", "protocol=https\ngithub.com\n\n", lambda: "t")


# S-004-10


FAKE_GH = """#!/bin/sh
printf '%s\\n' "$@" > "$RECORD/gh-args"
printf '%s' "$GH_TOKEN" > "$RECORD/gh-token"
exit 7
"""

FAKE_PYTHON = """#!/bin/sh
printf '%s\\n' "$@" > "$RECORD/python-args"
if [ -n "$FAIL" ]; then echo "shipmill: no App private key at /k.pem" >&2; exit 2; fi
echo minted-secret
"""


@dataclass(frozen=True)
class Host:
    folder: Path  # the helpers' folder
    fakes: Path  # holds the real gh and the fake python
    record: Path
    path: str  # the session's PATH: the helpers' folder first


def executable(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    path.chmod(0o700)
    return path


@pytest.fixture
def host(tmp_path: Path) -> Host:
    fakes = tmp_path / "fakes"
    fakes.mkdir()
    executable(fakes / "gh", FAKE_GH)
    executable(fakes / "python", FAKE_PYTHON)
    record = tmp_path / "record"
    record.mkdir()
    folder = tmp_path / "state" / "bin"
    return Host(folder, fakes, record, os.pathsep.join([str(folder), str(fakes), "/usr/bin", "/bin"]))


def helpers_for(host: Host, tmp_path: Path, python: Path | None = None) -> tuple[Path, Path]:
    helpers = write_helpers(
        host.folder, python or host.fakes / "python", tmp_path / "checkout", REPO, APP_ID, tmp_path / "k.pem", host.path
    )
    return helpers.gh, helpers.git_credential


def run_helper(
    helper: Path, host: Host, args: list[str], stdin: str = "", **env: str
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(helper), *args],
        input=stdin,
        capture_output=True,
        text=True,
        check=False,
        env={"PATH": host.path, "RECORD": str(host.record), **env},
    )


def test_s004_10_the_gh_helper_runs_gh_with_its_arguments_and_the_minted_token(host: Host, tmp_path: Path) -> None:
    gh, _ = helpers_for(host, tmp_path)
    done = run_helper(gh, host, ["pr", "create", "--title", "two words", "--body", ""])
    assert done.returncode == 7  # gh's own exit code
    assert (host.record / "gh-args").read_text() == "pr\ncreate\n--title\ntwo words\n--body\n\n"
    assert (host.record / "gh-token").read_text() == "minted-secret"
    called = (host.record / "python-args").read_text().splitlines()
    assert called == [
        "-m",
        "shipmill",
        "--repo",
        str((tmp_path / "checkout").resolve()),
        "app-token",
        REPO,
        "--app-id",
        str(APP_ID),
        "--app-key",
        str((tmp_path / "k.pem").resolve()),
    ]
    assert "minted-secret" not in done.stdout + done.stderr


def test_s004_10_when_minting_fails_the_gh_helper_exits_non_zero_without_gh(host: Host, tmp_path: Path) -> None:
    gh, _ = helpers_for(host, tmp_path)
    done = run_helper(gh, host, ["pr", "list"], FAIL="1")
    assert done.returncode == 2
    assert "no App private key" in done.stderr
    assert not (host.record / "gh-args").exists()


def test_s004_10_the_credential_helper_passes_the_operation_and_the_request(host: Host, tmp_path: Path) -> None:
    _, helper = helpers_for(host, tmp_path)
    done = run_helper(helper, host, ["get"], GET)
    assert done.returncode == 0
    assert (host.record / "python-args").read_text().splitlines()[-2:] == ["--git-credential", "get"]


def test_s004_10_neither_helper_file_contains_a_token(host: Host, tmp_path: Path) -> None:
    gh, helper = helpers_for(host, tmp_path)
    run_helper(gh, host, ["auth", "status"])
    run_helper(helper, host, ["get"], GET)
    assert (host.record / "gh-token").read_text() == "minted-secret"  # the helpers did mint
    for path in (gh, helper):
        assert stat.S_IMODE(path.stat().st_mode) == 0o700
        assert "minted-secret" not in path.read_text(encoding="utf-8")
    assert "GH_TOKEN=$token" in gh.read_text(encoding="utf-8")
    assert sorted(p.name for p in host.folder.iterdir()) == ["gh", "git-credential-shipmill"]


def test_s004_10_the_real_gh_is_resolved_past_the_helpers_folder(host: Host, tmp_path: Path) -> None:
    helpers_for(host, tmp_path)  # the helpers' folder, holding a gh, is first on PATH
    assert resolve_gh(host.folder, host.path) == (host.fakes / "gh").resolve()
    assert f'exec {shlex.quote(str((host.fakes / "gh").resolve()))} "$@"' in (host.folder / "gh").read_text()


def test_s004_10_a_missing_gh_is_named(host: Host) -> None:
    with pytest.raises(ReleaseError, match="gh not found on PATH"):
        resolve_gh(host.folder, os.pathsep.join([str(host.folder), str(host.record)]))


def test_s004_10_the_helpers_run_this_shipmill_through_python_m(checkout: Git, host: Host, tmp_path: Path) -> None:
    """End to end, with no network: a cached token, the real `python -m shipmill`, and the fake gh"""
    save_token(cache_of(checkout), Token("cached-secret", dt.datetime.now(dt.UTC) + HOUR, REPO, APP_ID))
    helpers = write_helpers(
        host.folder, Path(sys.executable), checkout.root, REPO, APP_ID, tmp_path / "absent.pem", host.path
    )
    done = run_helper(helpers.git_credential, host, ["get"], GET)
    assert (done.returncode, done.stdout, done.stderr) == (0, "username=x-access-token\npassword=cached-secret\n", "")
    done = run_helper(helpers.gh, host, ["api", "user"])
    assert (done.returncode, done.stdout, done.stderr) == (7, "", "")
    assert (host.record / "gh-token").read_text() == "cached-secret"
    for path in (helpers.gh, helpers.git_credential):
        assert "cached-secret" not in path.read_text(encoding="utf-8")
