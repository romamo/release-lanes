"""Spec 007: shipmill app-install guides installing the gate's App on more repos"""

import argparse
import datetime as dt
import io
import json
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from shipmill.app import Answer
from shipmill.app_install import Installed, Step, guide, steps
from shipmill.cli import _app_install
from shipmill.errors import ReleaseError

APP_ID = 5209412


@dataclass
class FakeApp:
    """GitHub's App API: who owns the App, its installations, and which repos they cover,
    with repos appearing after a number of checks"""

    owner_type: str = "User"
    accounts: dict[str, tuple[str, str]] = field(default_factory=dict)  # login: (type, selection)
    covered: set[str] = field(default_factory=set)
    appears_after: dict[str, int] = field(default_factory=dict)  # repo: polls after the first check
    status: dict[str, int] = field(default_factory=dict)
    calls: list[str] = field(default_factory=list)

    def get(self, path: str, token: str | None) -> Answer:
        self.calls.append(path)
        if path == "/app":
            owner = {"login": "romamo" if self.owner_type == "User" else "acme", "type": self.owner_type}
            return Answer(200, json.dumps({"slug": "shipmill-romamo", "owner": owner}))
        if path.startswith("/app/installations"):
            rows = [
                {"id": n, "account": {"login": login, "type": kind}, "repository_selection": selection}
                for n, (login, (kind, selection)) in enumerate(self.accounts.items(), 1)
            ]
            return Answer(200, json.dumps(rows))
        if path.startswith("/repos/") and path.endswith("/installation"):
            repo = path.removeprefix("/repos/").removesuffix("/installation")
            if repo in self.status:
                return Answer(self.status[repo], "{}")
            if repo in self.appears_after:
                self.appears_after[repo] -= 1
                if self.appears_after[repo] < -1:
                    self.covered.add(repo)
            return Answer(200, "{}") if repo in self.covered else Answer(404, "{}")
        raise AssertionError(f"unexpected GET {path}")

    def post(self, path: str, token: str, body: Mapping[str, object]) -> Answer:
        raise AssertionError(f"app-install never writes: POST {path}")


class FakeSigner:
    def sign(self, key: Path, data: bytes) -> bytes:
        return b"signature"


@dataclass
class Clock:
    now: dt.datetime = dt.datetime(2026, 10, 6, 12, tzinfo=dt.UTC)

    def __call__(self) -> dt.datetime:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += dt.timedelta(seconds=seconds)


def run_guide(app: FakeApp, repos: list[str], seconds: float = 600) -> tuple[list[str], list[str], object]:
    said: list[str] = []
    opened: list[str] = []
    clock = Clock()
    signer = FakeSigner()
    guided = guide(APP_ID, Path("k.pem"), repos, app, signer, said.append, opened.append, clock, clock.sleep, seconds)
    return said, opened, guided


PAGE = "https://github.com/settings/apps/shipmill-romamo/installations"


def test_s007_3_a_covered_repo_opens_nothing() -> None:
    said, opened, _ = run_guide(FakeApp(covered={"shipmill/shipmill"}), ["shipmill/shipmill"])
    assert (said, opened) == (["already installed on shipmill/shipmill"], [])


def test_s007_3_another_status_names_the_repo() -> None:
    with pytest.raises(ReleaseError, match=r"can't tell whether the App covers acme/web"):
        run_guide(FakeApp(status={"acme/web": 500}), ["acme/web"])


def test_s007_4_missing_repos_open_the_install_app_page_once() -> None:
    app = FakeApp(appears_after={"acme/web": 0, "acme/api": 0, "beta/x": 0})
    said, opened, _ = run_guide(app, ["acme/web", "acme/api", "beta/x"])
    assert opened == [PAGE]
    assert said[0] == (
        f"shipmill-romamo doesn't cover acme/web, acme/api, beta/x yet; GitHub needs your click. Open {PAGE}"
    )


def test_s007_4_an_org_owned_app_opens_the_orgs_page() -> None:
    _, opened, _ = run_guide(FakeApp(owner_type="Organization", appears_after={"acme/web": 0}), ["acme/web"])
    assert opened == ["https://github.com/organizations/acme/settings/apps/shipmill-romamo/installations"]


def test_s007_5_each_account_is_told_to_install_or_configure() -> None:
    found = {"acme": Installed(7, "acme", True, True)}
    assert [s.line() for s in steps(["acme/web", "beta/x", "acme/api"], found)] == [
        "  acme: click Configure, add acme/web, acme/api under Repository access, Save",
        "  beta: click Install, choose Only select repositories, pick beta/x",
    ]
    assert steps(["beta/x"], {}) == [Step("beta", "install", ("beta/x",))]


def test_s007_6_installations_are_reported_and_the_wait_ends() -> None:
    app = FakeApp(appears_after={"acme/web": 1, "beta/x": 3})
    said, _, guided = run_guide(app, ["acme/web", "beta/x"])
    assert said[-2:] == ["installed on acme/web", "installed on beta/x"]
    assert guided.installed == ("acme/web", "beta/x")  # type: ignore[attr-defined]
    _, _, partial = run_guide(FakeApp(appears_after={"acme/web": 0}), ["acme/web", "beta/x"], seconds=30)
    assert partial.installed == ("acme/web",)  # type: ignore[attr-defined]


def args(**given: object) -> argparse.Namespace:
    values: dict[str, object] = {"repos": [], "app_id": APP_ID, "app_key": None, "no_browser": True, "json": False}
    return argparse.Namespace(**(values | given))


@pytest.fixture
def key(tmp_path: Path) -> Path:
    path = tmp_path / "app.pem"
    path.write_text("not a real key\n", encoding="utf-8")
    path.chmod(0o600)
    return path


def checkout(root: Path, origin: str) -> Path:
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "remote", "add", "origin", origin], check=True)
    return root


def test_s007_1_the_default_repo_is_origins(tmp_path: Path, key: Path) -> None:
    root = checkout(tmp_path / "repo", "git@github.com:acme/web.git")
    app = FakeApp(covered={"acme/web"})
    out = io.StringIO()
    assert _app_install(root, args(app_key=key), app, FakeSigner(), out=out) == 0
    assert out.getvalue() == "already installed on acme/web\n"
    with pytest.raises(ReleaseError, match=r"is not a GitHub checkout; name the repos"):
        _app_install(tmp_path, args(app_key=key), app, FakeSigner())


def test_s007_2_the_app_id_comes_from_the_config_or_the_flag(tmp_path: Path, key: Path) -> None:
    root = checkout(tmp_path / "repo", "https://github.com/acme/web")
    with pytest.raises(ReleaseError, match=r"pass --app-id"):
        _app_install(root, args(app_id=None, app_key=key), FakeApp(), FakeSigner())
    (root / ".github").mkdir()
    (root / ".github" / "shipmill.toml").write_text('[agents]\nprompt = "/t"\napp_id = 77\n', encoding="utf-8")
    out = io.StringIO()
    assert _app_install(root, args(app_id=None, app_key=key), FakeApp(covered={"acme/web"}), FakeSigner(), out=out) == 0
    key.chmod(0o644)
    with pytest.raises(ReleaseError, match=r"chmod 600"):
        _app_install(root, args(app_key=key), FakeApp(), FakeSigner())


def test_s007_6_and_7_a_missing_repo_exits_1_with_lines_flushed(tmp_path: Path, key: Path) -> None:
    class Flushes(io.StringIO):
        count = 0

        def flush(self) -> None:
            Flushes.count += 1

    out = Flushes()
    opened: list[str] = []
    clock = Clock()
    code = _app_install(
        tmp_path,
        args(repos=["beta/x"], app_key=key),
        FakeApp(),
        FakeSigner(),
        opened.append,
        out,
        clock,
        clock.sleep,
        10,
    )
    lines = out.getvalue().splitlines()
    assert code == 1 and opened == [PAGE]
    assert lines[-1] == f"not installed on beta/x; install it at {PAGE}"
    assert Flushes.count == len(lines)


def test_s007_7_no_browser_prints_the_page_and_opens_nothing(
    tmp_path: Path, key: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    clock = Clock()
    code = _app_install(
        tmp_path, args(repos=["beta/x"], app_key=key), FakeApp(), FakeSigner(), None, None, clock, clock.sleep, 0
    )
    assert code == 1 and PAGE in capsys.readouterr().out


def test_s007_8_json_lists_each_repo(tmp_path: Path, key: Path, capsys: pytest.CaptureFixture[str]) -> None:
    app = FakeApp(covered={"acme/web"}, accounts={"beta": ("Organization", "selected")}, appears_after={"beta/x": 0})
    clock = Clock()
    code = _app_install(
        tmp_path,
        args(repos=["acme/web", "beta/x"], app_key=key, json=True),
        app,
        FakeSigner(),
        lambda url: None,
        None,
        clock,
        clock.sleep,
    )
    assert code == 0
    assert json.loads(capsys.readouterr().out) == {
        "app_id": APP_ID,
        "slug": "shipmill-romamo",
        "page": PAGE,
        "repos": [
            {"repo": "acme/web", "account": "acme", "action": "none", "installed": True},
            {"repo": "beta/x", "account": "beta", "action": "add", "installed": True},
        ],
    }


def test_s007_9_app_create_guides_its_install_step_the_same_way() -> None:
    root = Path(__file__).resolve().parents[1]
    cli = (root / "src" / "shipmill" / "cli.py").read_text(encoding="utf-8")
    body = cli.split("def _app_create(", 1)[1].split("\ndef ", 1)[0]
    assert "guide(created.app_id, created.key, targets" in body


def test_s007_10_the_docs_document_app_install() -> None:
    root = Path(__file__).resolve().parents[1]
    install = (root / "docs" / "install.md").read_text(encoding="utf-8")
    skill = (root / "skills" / "shipmill-setup" / "SKILL.md").read_text(encoding="utf-8")
    assert "shipmill app-install" in install and "$CR app-install" in skill
    for doc in (install, skill):
        assert "Install App" in doc and "click" in doc
