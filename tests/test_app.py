"""Spec 004: the gate checks the GitHub App a session would write as, before any launch"""

import base64
import datetime as dt
import json
import shutil
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from shipmill.agents import AgentsConfig
from shipmill.app import (
    PERMISSIONS,
    Access,
    Answer,
    Installation,
    Openssl,
    app_check,
    check_key,
    default_key,
    installation,
    jwt,
)
from shipmill.autonomy import Hold
from shipmill.cli import main
from shipmill.errors import ReleaseError
from shipmill.gate import RECORD, Action, Finding, gate, state_dir
from shipmill.gitrepo import Git

from .test_gate import ISSUES, NO_PRUNE, NOW, FakeClaude, FakeNotifier, bg, write_config


@pytest.fixture
def checkout(tmp_path: Path) -> Git:
    root = tmp_path / "demo"
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    git = Git(root)
    git.run("remote", "add", "origin", "https://github.com/romamo/demo.git")
    return git


REPO = "romamo/demo"
APP_ID = 123456
GRANTED = {p.key: str(p.access) for p in PERMISSIONS}


@dataclass
class FakeSigner:
    signed: list[tuple[Path, bytes]] = field(default_factory=list)

    def sign(self, key: Path, data: bytes) -> bytes:
        self.signed.append((key, data))
        return b"signature"


@dataclass
class FakeApi:
    """Answers GET /app and the installation from its fields; records each call"""

    installed: bool = True
    permissions: dict[str, str] = field(default_factory=lambda: dict(GRANTED))
    calls: list[tuple[str, str]] = field(default_factory=list)

    def get(self, path: str, token: str) -> Answer:
        self.calls.append((path, token))
        if path == "/app":
            return Answer(200, json.dumps({"slug": "demo-agent", "id": APP_ID}))
        if path == f"/repos/{REPO}/installation":
            if not self.installed:
                return Answer(404, '{"message": "Not Found"}')
            return Answer(200, json.dumps({"id": 77, "permissions": self.permissions}))
        raise AssertionError(f"unexpected GET {path}")

    def post(self, path: str, token: str, body: Mapping[str, object]) -> Answer:
        raise AssertionError(f"unexpected POST {path}")


def key_file(tmp_path: Path, mode: int = 0o600) -> Path:
    path = tmp_path / "app.pem"
    path.write_text("not a real key\n", encoding="utf-8")
    path.chmod(mode)
    return path


def app_cfg(app_id: int | None = APP_ID) -> AgentsConfig:
    return AgentsConfig(prompt="/t", prs=False, retry_hours=24, app_id=app_id)


# S-004-1


def test_s004_1_app_id_is_unset_by_default_and_read_when_given(tmp_path: Path) -> None:
    write_config(tmp_path, '[agents]\nprompt = "/t"\n')
    assert AgentsConfig.load(tmp_path).app_id is None
    write_config(tmp_path, '[agents]\nprompt = "/t"\napp_id = 123456\n')
    assert AgentsConfig.load(tmp_path).app_id == 123456


@pytest.mark.parametrize(
    ("value", "message"),
    [
        ('"123"', "app_id must be an integer"),
        ("1.5", "app_id must be an integer"),
        ("true", "app_id must be an integer"),
        ("0", "app_id must be 1 or more, got 0"),
        ("-4", "app_id must be 1 or more, got -4"),
    ],
)
def test_s004_1_a_bad_app_id_is_refused_naming_the_key(tmp_path: Path, value: str, message: str) -> None:
    write_config(tmp_path, f'[agents]\nprompt = "/t"\napp_id = {value}\n')
    with pytest.raises(ReleaseError, match=message):
        AgentsConfig.load(tmp_path)


# S-004-2


NO_APP_KEY = "--app-key names an App's key, but .* sets no app_id"


@pytest.mark.parametrize("findings", [[], [ISSUES]])
def test_s004_2_app_key_without_app_id_exits_2_and_starts_nothing(checkout: Git, findings: list[Finding]) -> None:
    claude = FakeClaude([bg("old", "idle", "done")])
    with pytest.raises(ReleaseError, match=NO_APP_KEY):
        gate(
            checkout,
            REPO,
            lambda: app_cfg(None),
            claude,
            lambda: findings,
            NOW,
            Hold,
            FakeNotifier(),
            NO_PRUNE,
            app_key_named=True,
        )
    assert (claude.launched, claude.stopped) == ([], [])


def test_s004_2_the_cli_passes_app_key_to_the_gate(tmp_path: Path) -> None:
    """A checkout whose origin is another repo: the CLI no longer refuses before the gate's own checks"""
    write_config(tmp_path, '[agents]\nprompt = "/t"\n')
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "remote", "add", "origin", "https://github.com/x/y.git"], check=True)
    with pytest.raises(ReleaseError, match="not romamo/demo"):
        main(["--repo", str(tmp_path), "gate", REPO, "--app-key", str(tmp_path / "k.pem")])


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def test_s004_2_app_key_is_checked_against_the_refreshed_config(tmp_path: Path) -> None:
    """Regression: a gate started with --app-key before the app_id change reached its checkout
    must refresh and then check the App, not exit on the stale config every tick"""
    origin = tmp_path / "romamo" / "demo.git"
    origin.parent.mkdir()
    _git("init", "-q", "--bare", "-b", "main", str(origin), cwd=tmp_path)
    pusher = tmp_path / "pusher"
    _git("clone", "-q", str(origin), str(pusher), cwd=tmp_path)
    for k, v in (("user.name", "t"), ("user.email", "t@t")):
        _git("config", k, v, cwd=pusher)
    write_config(pusher, '[agents]\nprompt = "/t"\n')
    _git("add", ".", cwd=pusher)
    _git("commit", "-q", "-m", "agents", cwd=pusher)
    _git("push", "-q", "origin", "main", cwd=pusher)
    root = tmp_path / "gate"
    _git("clone", "-q", str(origin), str(root), cwd=tmp_path)
    _git("checkout", "-q", "--detach", cwd=root)
    write_config(pusher, f'[agents]\nprompt = "/t"\napp_id = {APP_ID}\n')
    _git("commit", "-q", "-am", "app_id", cwd=pusher)
    _git("push", "-q", "origin", "main", cwd=pusher)

    git = Git(root)
    assert AgentsConfig.load(root).app_id is None  # stale
    api = FakeApi()
    check = app_check(REPO, key_file(tmp_path), tmp_path, FakeSigner(), api)
    claude = FakeClaude([bg("old", "idle", "done")])
    with pytest.raises(ReleaseError, match="can't launch a session as an App yet"):
        gate(
            git,
            REPO,
            lambda: AgentsConfig.load(root),
            claude,
            lambda: [ISSUES],
            NOW,
            Hold,
            FakeNotifier(),
            NO_PRUNE,
            refresh_checkout=True,
            app=check,
            app_key_named=True,
        )
    assert AgentsConfig.load(root).app_id == APP_ID  # the checkout moved
    assert [path for path, _ in api.calls] == ["/app", f"/repos/{REPO}/installation"]
    assert (claude.launched, claude.stopped) == ([], [])


def test_s004_2_without_app_key_the_gate_reads_the_default_path(tmp_path: Path) -> None:
    home = tmp_path / "home"
    expected = home / ".config" / "shipmill" / f"app-{APP_ID}.pem"
    assert default_key(APP_ID, home) == expected
    check = app_check(REPO, None, home, FakeSigner(), FakeApi())
    with pytest.raises(ReleaseError, match=f"no App private key at {expected}"):
        check(APP_ID, NOW)
    expected.parent.mkdir(parents=True)
    key_file(expected.parent).rename(expected)
    signer = FakeSigner()
    assert app_check(REPO, None, home, signer, FakeApi())(APP_ID, NOW).slug == "demo-agent"
    assert signer.signed[0][0] == expected


def test_s004_2_app_key_overrides_the_default_path(tmp_path: Path) -> None:
    key = key_file(tmp_path)
    signer = FakeSigner()
    app_check(REPO, key, tmp_path / "home", signer, FakeApi())(APP_ID, NOW)
    assert signer.signed[0][0] == key


# S-004-3


@pytest.mark.parametrize("mode", [0o640, 0o604, 0o644])
def test_s004_3_a_key_readable_by_group_or_others_is_refused(tmp_path: Path, mode: int) -> None:
    key = key_file(tmp_path, mode)
    with pytest.raises(ReleaseError, match=f"{key} is readable by group or others; run `chmod 600 {key}`"):
        check_key(key)


def test_s004_3_an_owner_only_key_passes(tmp_path: Path) -> None:
    key = key_file(tmp_path, 0o600)
    assert check_key(key) == key
    key.chmod(0o400)
    assert check_key(key) == key


@pytest.mark.parametrize("problem", ["missing", "open"])
def test_s004_3_a_bad_key_launches_and_stops_nothing(checkout: Git, tmp_path: Path, problem: str) -> None:
    key = tmp_path / "missing.pem" if problem == "missing" else key_file(tmp_path, 0o644)
    claude = FakeClaude([bg("old", "idle", "done")])
    check = app_check(REPO, key, tmp_path, FakeSigner(), FakeApi())
    with pytest.raises(ReleaseError, match=str(key)) as raised:
        gate(checkout, REPO, app_cfg, claude, lambda: [ISSUES], NOW, Hold, FakeNotifier(), NO_PRUNE, app=check)
    assert "not a real key" not in str(raised.value)
    assert (claude.launched, claude.stopped) == ([], [])
    assert not (state_dir(checkout) / RECORD).exists()


# S-004-4


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def test_s004_4_the_jwt_claims_and_header(tmp_path: Path) -> None:
    signer = FakeSigner()
    token = jwt(APP_ID, tmp_path / "k.pem", NOW, signer)
    header, claims, signature = token.split(".")
    assert json.loads(_unb64(header)) == {"alg": "RS256", "typ": "JWT"}
    issued = int(NOW.timestamp())
    assert json.loads(_unb64(claims)) == {"iat": issued - 60, "exp": issued + 9 * 60, "iss": APP_ID}
    assert _unb64(signature) == b"signature"
    assert "=" not in token
    assert signer.signed == [(tmp_path / "k.pem", f"{header}.{claims}".encode())]


@pytest.mark.skipif(shutil.which("openssl") is None, reason="needs openssl")
def test_s004_4_openssl_signs_a_jwt_its_public_key_verifies(tmp_path: Path) -> None:
    key = tmp_path / "app.pem"
    subprocess.run(["openssl", "genrsa", "-out", str(key), "2048"], check=True, capture_output=True)
    key.chmod(0o600)
    public = tmp_path / "app.pub"
    subprocess.run(["openssl", "rsa", "-in", str(key), "-pubout", "-out", str(public)], check=True, capture_output=True)
    token = jwt(APP_ID, check_key(key), NOW, Openssl())
    header, claims, signature = token.split(".")
    (tmp_path / "signed").write_bytes(f"{header}.{claims}".encode())
    (tmp_path / "sig").write_bytes(_unb64(signature))
    verify = ["openssl", "dgst", "-sha256", "-verify", str(public), "-signature", str(tmp_path / "sig")]
    proc = subprocess.run([*verify, str(tmp_path / "signed")], capture_output=True, text=True, check=False)
    assert proc.returncode == 0, proc.stderr
    assert key.read_text(encoding="utf-8").splitlines()[1] not in token


def test_s004_4_a_missing_openssl_is_named(checkout: Git, tmp_path: Path) -> None:
    missing = str(tmp_path / "bin" / "openssl")
    with pytest.raises(ReleaseError, match=f"{missing} not found"):
        Openssl(missing).sign(key_file(tmp_path), b"data")
    claude = FakeClaude([bg("old", "idle", "done")])
    check = app_check(REPO, key_file(tmp_path), tmp_path, Openssl(missing), FakeApi())
    with pytest.raises(ReleaseError, match="openssl not found"):
        gate(checkout, REPO, app_cfg, claude, lambda: [ISSUES], NOW, Hold, FakeNotifier(), NO_PRUNE, app=check)
    assert (claude.launched, claude.stopped) == ([], [])


# S-004-5


def test_s004_5_an_app_not_installed_on_the_repo_is_named(checkout: Git, tmp_path: Path) -> None:
    claude = FakeClaude([bg("old", "idle", "done")])
    check = app_check(REPO, key_file(tmp_path), tmp_path, FakeSigner(), FakeApi(installed=False))
    with pytest.raises(ReleaseError, match=f"^app demo-agent is not installed on {REPO}$"):
        gate(checkout, REPO, app_cfg, claude, lambda: [ISSUES], NOW, Hold, FakeNotifier(), NO_PRUNE, app=check)
    assert (claude.launched, claude.stopped) == ([], [])


def test_s004_5_each_missing_permission_is_named_with_its_access(checkout: Git, tmp_path: Path) -> None:
    granted = dict(GRANTED, contents="read", checks="write")  # write covers read
    del granted["workflows"], granted["statuses"]
    claude = FakeClaude([bg("old", "idle", "done")])
    check = app_check(REPO, key_file(tmp_path), tmp_path, FakeSigner(), FakeApi(permissions=granted))
    with pytest.raises(ReleaseError) as raised:
        gate(checkout, REPO, app_cfg, claude, lambda: [ISSUES], NOW, Hold, FakeNotifier(), NO_PRUNE, app=check)
    assert str(raised.value).startswith(
        f"app demo-agent on {REPO} lacks these permissions: Contents: write, Workflows: write, Commit statuses: read;"
    )
    assert (claude.launched, claude.stopped) == ([], [])


def test_s004_5_the_installation_is_read_with_the_jwt() -> None:
    api = FakeApi()
    found = installation(APP_ID, REPO, "the-jwt", api)
    assert found == Installation("demo-agent", 77, {k: Access(v) for k, v in GRANTED.items()})
    assert found.bot == "demo-agent[bot]"
    assert api.calls == [("/app", "the-jwt"), (f"/repos/{REPO}/installation", "the-jwt")]


@pytest.mark.parametrize(
    ("answer", "message"),
    [
        (Answer(401, '{"message": "Bad credentials"}'), "GitHub refused the JWT for app_id 123456"),
        (Answer(500, "oops"), "GET /app for app_id 123456 answered 500: oops"),
        (Answer(200, "[]"), "not an object"),
        (Answer(200, "{}"), "gave no slug"),
    ],
)
def test_a_bad_app_answer_is_refused(answer: Answer, message: str) -> None:
    class Api:
        def get(self, path: str, token: str) -> Answer:
            return answer

        def post(self, path: str, token: str, body: Mapping[str, object]) -> Answer:
            raise AssertionError(f"unexpected POST {path}")

    with pytest.raises(ReleaseError, match=message):
        installation(APP_ID, REPO, "t", Api())


def test_an_unknown_access_is_refused() -> None:
    with pytest.raises(ReleaseError, match="unknown access 'owner'"):
        installation(APP_ID, REPO, "t", FakeApi(permissions=dict(GRANTED, contents="owner")))


# D-14 until the launch as the App lands: checks that pass still start nothing


def test_with_app_id_set_a_passing_check_still_launches_nothing(checkout: Git, tmp_path: Path) -> None:
    claude = FakeClaude([bg("old", "idle", "done")])
    check = app_check(REPO, key_file(tmp_path), tmp_path, FakeSigner(), FakeApi())
    for dry_run in (False, True):
        with pytest.raises(ReleaseError, match="app_id 123456 is set .* can't launch a session as an App yet"):
            gate(
                checkout,
                REPO,
                app_cfg,
                claude,
                lambda: [ISSUES],
                NOW,
                Hold,
                FakeNotifier(),
                NO_PRUNE,
                dry_run=dry_run,
                app=check,
            )
    assert (claude.launched, claude.stopped) == ([], [])
    assert not (state_dir(checkout) / RECORD).exists()


def test_with_app_id_set_a_tick_that_launches_nothing_checks_nothing(checkout: Git) -> None:
    def unchecked(app_id: int, now: dt.datetime) -> Installation:
        raise AssertionError("only a launch checks the App")

    decision, _, _, _ = gate(
        checkout, REPO, app_cfg, FakeClaude(), lambda: [], NOW, Hold, FakeNotifier(), NO_PRUNE, app=unchecked
    )
    assert decision.action is Action.QUIET
    busy = FakeClaude([bg("a", "busy", "working")])
    decision, _, _, _ = gate(
        checkout, REPO, app_cfg, busy, lambda: [ISSUES], NOW, Hold, FakeNotifier(), NO_PRUNE, app=unchecked
    )
    assert decision.action is Action.RUNNING


def test_with_app_id_unset_the_app_is_never_checked(checkout: Git) -> None:
    def unchecked(app_id: int, now: dt.datetime) -> Installation:
        raise AssertionError("no app_id, no App check")

    claude = FakeClaude()
    decision, launched, _, _ = gate(
        checkout,
        REPO,
        lambda: app_cfg(None),
        claude,
        lambda: [ISSUES],
        NOW,
        Hold,
        FakeNotifier(),
        NO_PRUNE,
        app=unchecked,
    )
    assert (decision.action, launched) == (Action.LAUNCH, "s1")
