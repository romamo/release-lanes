"""Spec 006: shipmill app-create plans, creates, and installs the gate's GitHub App"""

import argparse
import html
import io
import json
import re
import stat
import threading
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from shipmill.app import PERMISSIONS, Answer
from shipmill.app_create import (
    Account,
    Created,
    accounts,
    check_key_dir,
    config_lines,
    convert,
    create,
    free_name,
    gated_repos,
    given_repos,
    manifest,
    new_app_url,
    owner_menu,
    page,
    pick_owner,
    plan,
    save_key,
    slug,
)
from shipmill.cli import _app_create
from shipmill.errors import ReleaseError

TOKEN = "gho_host"
PEM = "-----BEGIN RSA PRIVATE KEY-----\nsecret\n-----END RSA PRIVATE KEY-----\n"
ME = Account("romamo", "User")
SHIPMILL = Account("shipmill", "Organization")
CAS = Account("cli-agent-spec", "Organization")
PERSONAL_PUBLIC = (
    "your personal account (--owner picks an org you administer); public, since 2 gated repos are elsewhere"
)


@dataclass
class FakeHub:
    """GitHub's REST API for one login: its orgs, their repos, which hold the config, which
    have the App installed, and the manifest conversion"""

    memberships: list[dict[str, object]] = field(
        default_factory=lambda: [
            {"state": "active", "role": "admin", "organization": {"login": "shipmill"}},
            {"state": "active", "role": "admin", "organization": {"login": "cli-agent-spec"}},
            {"state": "active", "role": "member", "organization": {"login": "theagenttimes"}},
            {"state": "pending", "role": "admin", "organization": {"login": "invited"}},
        ]
    )
    repos: dict[str, list[dict[str, object]]] = field(
        default_factory=lambda: {
            "romamo": [{"full_name": "romamo/notes"}],
            "shipmill": [{"full_name": "shipmill/shipmill"}, {"full_name": "shipmill/old", "archived": True}],
            "cli-agent-spec": [{"full_name": "cli-agent-spec/cli-agent-spec"}],
        }
    )
    gated: set[str] = field(
        default_factory=lambda: {"shipmill/shipmill", "cli-agent-spec/cli-agent-spec", "shipmill/old"}
    )
    contents_status: dict[str, int] = field(default_factory=dict)
    memberships_status: int = 200
    installed: list[str] = field(default_factory=list)
    install_after: dict[str, int] = field(default_factory=dict)  # repo: polls before it appears
    taken_slugs: set[str] = field(default_factory=set)
    calls: list[tuple[str, str, str | None]] = field(default_factory=list)

    def get(self, path: str, token: str | None) -> Answer:
        self.calls.append(("GET", path, token))
        route = path.split("?", 1)[0]
        query = urllib.parse.parse_qs(path.split("?", 1)[1]) if "?" in path else {}
        page = int(query.get("page", ["1"])[0])
        if route == "/user":
            return Answer(200, json.dumps({"login": "romamo"}))
        if route == "/user/memberships/orgs":
            if self.memberships_status != 200:
                return Answer(self.memberships_status, '{"message": "Forbidden"}')
            return Answer(200, json.dumps(self.memberships if page == 1 else []))
        if route == "/user/repos":
            assert query.get("affiliation") == ["owner"]
            return Answer(200, json.dumps(self.repos["romamo"] if page == 1 else []))
        if m := re.fullmatch(r"/orgs/([^/]+)/repos", route):
            return Answer(200, json.dumps(self.repos.get(m.group(1), []) if page == 1 else []))
        if m := re.fullmatch(r"/repos/([^/]+/[^/]+)/contents/\.github/shipmill\.toml", route):
            repo = m.group(1)
            status = self.contents_status.get(repo, 200 if repo in self.gated else 404)
            return Answer(status, "{}")
        if route == "/app":
            return Answer(200, json.dumps({"slug": "shipmill-agent", "owner": {"login": "romamo", "type": "User"}}))
        if route == "/app/installations":
            return Answer(200, "[]")
        if m := re.fullmatch(r"/apps/([^/]+)", route):
            return Answer(200, "{}") if m.group(1) in self.taken_slugs else Answer(404, "{}")
        if m := re.fullmatch(r"/repos/([^/]+/[^/]+)/installation", route):
            repo = m.group(1)
            if repo in self.install_after:
                self.install_after[repo] -= 1
                if self.install_after[repo] <= 0:
                    self.installed.append(repo)
                    del self.install_after[repo]
            return Answer(200, '{"id": 7}') if repo in self.installed else Answer(404, "{}")
        raise AssertionError(f"unexpected GET {path}")

    def post(self, path: str, token: str, body: Mapping[str, object]) -> Answer:
        self.calls.append(("POST", path, token))
        if path == "/app-manifests/the-code/conversions":
            return Answer(
                201,
                json.dumps(
                    {
                        "id": 4242,
                        "slug": "shipmill-agent",
                        "pem": PEM,
                        "client_id": "Iv1.client",
                        "client_secret": "client-secret-value",
                        "webhook_secret": "webhook-secret-value",
                    }
                ),
            )
        return Answer(404, '{"message": "Not Found"}')


class FakeSigner:
    def sign(self, key: Path, data: bytes) -> bytes:
        return b"signature"


def test_s006_1_the_accounts_are_the_login_and_the_orgs_it_administers() -> None:
    assert accounts(FakeHub(), TOKEN) == (ME, CAS, SHIPMILL)


def test_s006_1_without_read_org_the_command_says_how_to_grant_it() -> None:
    with pytest.raises(ReleaseError, match=r"gh auth refresh -s read:org"):
        accounts(FakeHub(memberships_status=403), TOKEN)


def test_s006_2_a_repo_is_gated_when_its_config_exists_and_archived_ones_are_skipped() -> None:
    hub = FakeHub()
    assert gated_repos(hub, TOKEN, (ME, CAS, SHIPMILL)) == ("cli-agent-spec/cli-agent-spec", "shipmill/shipmill")
    assert not any("shipmill/old/contents" in path for _, path, _ in hub.calls)


def test_s006_2_any_other_status_names_the_repo() -> None:
    hub = FakeHub(contents_status={"shipmill/shipmill": 500})
    with pytest.raises(ReleaseError, match=r"shipmill/shipmill"):
        gated_repos(hub, TOKEN, (SHIPMILL,))


def test_s006_3_repos_replaces_discovery_and_refuses_another_account() -> None:
    assert given_repos(["shipmill/shipmill", "romamo/notes"], (ME, SHIPMILL)) == ("romamo/notes", "shipmill/shipmill")
    with pytest.raises(ReleaseError, match=r"theagenttimes/site is in theagenttimes"):
        given_repos(["theagenttimes/site"], (ME, SHIPMILL))


def test_s006_4_the_app_is_personal_and_private_by_default() -> None:
    planned = plan((ME, SHIPMILL), ("romamo/notes",), None, None, None)
    assert (planned.owner, planned.public, planned.name, planned.warnings) == (ME, False, "shipmill-romamo", ())
    assert planned.reason == "your personal account (--owner picks an org you administer)"
    assert plan((ME, SHIPMILL), (), None, None, None).owner == ME


def test_s006_5_gated_repos_outside_the_owner_make_it_public() -> None:
    planned = plan((ME, CAS, SHIPMILL), ("cli-agent-spec/x", "shipmill/a"), None, None, None)
    assert (planned.owner, planned.public) == (ME, True)
    assert planned.reason.endswith("; public, since 2 gated repos are elsewhere")
    assert planned.installable == ("cli-agent-spec/x", "shipmill/a")
    org = plan((ME, SHIPMILL), ("shipmill/a", "shipmill/b"), "shipmill", None, None)
    assert (org.owner, org.public, org.name) == (SHIPMILL, False, "shipmill-agent")


def test_s006_6_an_org_owner_is_asked_for_with_the_personal_account_first() -> None:
    found = (ME, CAS, SHIPMILL)
    assert owner_menu(found, ("shipmill/a", "shipmill/b", "cli-agent-spec/x")) == [
        "Who should own the App?",
        "  1. romamo (personal) (default)",
        "  2. cli-agent-spec (org, 1 gated repo)",
        "  3. shipmill (org, 2 gated repos)",
    ]
    assert [pick_owner(found, a) for a in ("", "3", "Shipmill")] == ["romamo", "shipmill", "shipmill"]
    with pytest.raises(ReleaseError, match=r"no account '9' in the list; pass --owner"):
        pick_owner(found, "9")


def test_s006_7_flags_override_the_plan_and_a_private_app_warns_about_other_accounts() -> None:
    repos = ("cli-agent-spec/x", "shipmill/a")
    forced = plan((ME, CAS, SHIPMILL), repos, "romamo", True, "n")
    assert (forced.owner, forced.public) == (ME, True)
    private = plan((ME, CAS, SHIPMILL), repos, "shipmill", False, "n")
    assert private.warnings == ("a private App can't be installed on cli-agent-spec/x, outside shipmill",)
    assert private.installable == ("shipmill/a",)
    with pytest.raises(ReleaseError, match=r"doesn't administer theagenttimes; .*owner role"):
        plan((ME, SHIPMILL), repos, "theagenttimes", None, "n")


def args(**given: object) -> argparse.Namespace:
    values: dict[str, object] = {
        "owner": None,
        "public": None,
        "name": None,
        "repos": None,
        "dry_run": False,
        "json": False,
        "no_browser": True,
    }
    return argparse.Namespace(**(values | given))


def test_s006_8_a_dry_run_prints_the_plan_and_creates_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    hub = FakeHub()
    opened: list[str] = []
    keys = tmp_path / "keys"
    code = _app_create(tmp_path, args(dry_run=True), hub, FakeSigner(), TOKEN, opened.append, keys)
    assert code == 0 and opened == [] and not keys.exists()
    assert not any(method == "POST" for method, _, _ in hub.calls)
    assert capsys.readouterr().out.splitlines()[1:4] == [
        "plan: shipmill-romamo under romamo, public",
        f"  why: {PERSONAL_PUBLIC}",
        "  repos: cli-agent-spec/cli-agent-spec, shipmill/shipmill",
    ]


def test_s006_9_the_manifest_holds_exactly_the_gates_permissions_and_no_webhook() -> None:
    planned = plan((ME, SHIPMILL), ("shipmill/shipmill",), "shipmill", None, "shipmill-agent")
    made = manifest(planned, "http://127.0.0.1:9/callback")
    assert made["default_permissions"] == {p.key: p.access.value for p in PERMISSIONS}
    assert made["hook_attributes"] == {"url": "https://example.invalid/shipmill-no-webhook", "active": False}
    assert (made["default_events"], made["public"], made["name"]) == ([], False, "shipmill-agent")
    assert made["redirect_url"] == "http://127.0.0.1:9/callback"
    assert made["url"] == "https://github.com/shipmill/shipmill"  # not a gated repo
    assert new_app_url(planned, "s") == "https://github.com/organizations/shipmill/settings/apps/new?state=s"
    mine = plan((ME,), ("romamo/notes",), None, None, "n")
    assert new_app_url(mine, "s") == "https://github.com/settings/apps/new?state=s"


def fetch(url: str) -> tuple[int, str]:
    try:
        with urllib.request.urlopen(url, timeout=10) as answer:
            return int(answer.status), answer.read().decode()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode()


@dataclass
class Clicker:
    """A browser that loads the page, reads the state from the form, sends a bad callback,
    then the one GitHub would send"""

    pages: list[str] = field(default_factory=list)
    bad: int = 0
    host: str = ""

    def __call__(self, url: str) -> None:
        def click() -> None:
            self.host = urllib.parse.urlparse(url).hostname or ""
            _, page = fetch(url)
            self.pages.append(page)
            action = html.unescape(re.search(r"action='([^']+)'", page).group(1))  # type: ignore[union-attr]
            state = urllib.parse.parse_qs(urllib.parse.urlparse(action).query)["state"][0]
            self.bad, _ = fetch(f"{url}callback?state=forged&code=evil")
            fetch(f"{url}callback?state={urllib.parse.quote(state)}&code=the-code")

        threading.Thread(target=click, daemon=True).start()


def test_s006_10_the_server_is_local_and_a_forged_callback_is_refused() -> None:
    planned = plan((ME, SHIPMILL), ("shipmill/shipmill",), "shipmill", None, "shipmill-agent")
    clicker = Clicker()
    assert create(planned, clicker, seconds=10) == "the-code"
    assert clicker.host == "127.0.0.1" and clicker.bad == 400
    sent = html.unescape(re.search(r'name=manifest value="([^"]+)"', clicker.pages[0]).group(1))  # type: ignore[union-attr]
    assert json.loads(sent)["name"] == "shipmill-agent"


def test_s006_11_the_key_is_saved_0600_in_0700_and_the_secrets_never_appear(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    keys = tmp_path / "shipmill"
    created = convert("the-code", FakeHub(), TOKEN, keys)
    assert created == Created(4242, "shipmill-agent", keys / "app-4242.pem")
    assert created.key.read_text(encoding="utf-8") == PEM
    assert stat.S_IMODE(created.key.stat().st_mode) == 0o600 and stat.S_IMODE(keys.stat().st_mode) == 0o700
    assert [p.name for p in keys.iterdir()] == ["app-4242.pem"]
    with pytest.raises(ReleaseError, match=r"never overwrites"):
        save_key(keys, 4242, "other")
    assert created.key.read_text(encoding="utf-8") == PEM
    keys.chmod(0o755)
    with pytest.raises(ReleaseError, match=r"chmod 700"):
        check_key_dir(keys)
    out = capsys.readouterr()
    for secret in ("secret", "client-secret-value", "webhook-secret-value", "Iv1.client"):
        assert secret not in out.out + out.err


def test_s006_12_no_callback_means_no_app() -> None:
    planned = plan((ME, SHIPMILL), ("shipmill/shipmill",), "shipmill", None, "n")
    with pytest.raises(ReleaseError, match=r"no App was created"):
        create(planned, lambda url: None, seconds=0.2)


def test_s006_14_a_real_run_prints_the_app_id_line_and_edits_no_config(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    hub = FakeHub(installed=["shipmill/shipmill", "cli-agent-spec/cli-agent-spec"])
    keys = tmp_path / "keys"
    browser = Clicker()
    opened: list[str] = []

    def both(url: str) -> None:
        opened.append(url)
        if url.startswith("http://127.0.0.1"):
            browser(url)

    before = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    code = _app_create(tmp_path, args(json=True), hub, FakeSigner(), TOKEN, both, keys)
    out = capsys.readouterr()
    assert code == 0
    assert all("/settings/apps/" not in url for url in opened)  # every repo was covered: no page to open
    assert "  app_id = 4242" in out.err.splitlines()
    data = json.loads(out.out)
    assert data == {
        "owner": "romamo",
        "owner_type": "User",
        "public": True,
        "name": "shipmill-romamo",
        "reason": PERSONAL_PUBLIC,
        "repos": ["cli-agent-spec/cli-agent-spec", "shipmill/shipmill"],
        "app_id": 4242,
        "slug": "shipmill-agent",
        "key": str(keys / "app-4242.pem"),
        "installed": ["cli-agent-spec/cli-agent-spec", "shipmill/shipmill"],
    }
    assert {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file() and keys not in p.parents} == before
    assert config_lines(Created(4242, "shipmill-agent", keys / "app-4242.pem"))[2] == "  app_id = 4242"


def test_s006_15_json_on_a_dry_run_is_the_plan_alone(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code = _app_create(tmp_path, args(dry_run=True, json=True), FakeHub(), FakeSigner(), TOKEN, None, tmp_path / "k")
    out = capsys.readouterr()
    assert code == 0 and "plan: shipmill-romamo under romamo, public" in out.err.splitlines()
    assert set(json.loads(out.out)) == {"owner", "owner_type", "public", "name", "reason", "repos"}


def test_s006_13_the_command_exits_1_naming_a_repo_left_uninstalled(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    hub = FakeHub(installed=["shipmill/shipmill"])
    browser = Clicker()

    def local(url: str) -> None:
        if url.startswith("http://127.0.0.1"):
            browser(url)

    code = _app_create(tmp_path, args(), hub, FakeSigner(), TOKEN, local, tmp_path / "keys", install_seconds=0)
    lines = capsys.readouterr().out.splitlines()
    assert code == 1
    assert "already installed on shipmill/shipmill" in lines
    assert (
        "not installed on cli-agent-spec/cli-agent-spec; "
        "install it at https://github.com/settings/apps/shipmill-agent/installations"
    ) in lines


def test_s006_16_the_docs_document_app_create() -> None:
    root = Path(__file__).resolve().parents[1]
    install = (root / "docs" / "install.md").read_text(encoding="utf-8")
    skill = (root / "skills" / "shipmill-setup" / "SKILL.md").read_text(encoding="utf-8")
    assert "$CR app-create --dry-run" in install and "$CR app-create --dry-run" in skill
    for doc in (install, skill):
        assert "read:org" in doc and "--public" in doc and "--private" in doc
    assert "| Permission | Access | Why |" in install  # the manual steps stay as the fallback
    assert "private" in install and "public" in install


def test_s006_17_the_default_name_is_shipmill_owner_then_shipmill_login() -> None:
    acme = Account("acme", "Organization")
    planned = plan((ME, acme), ("acme/web",), "acme", None, None)
    assert planned.name == "shipmill-acme"
    assert plan((ME, SHIPMILL), ("shipmill/shipmill",), "shipmill", None, None).name == "shipmill-agent"
    assert free_name(FakeHub(), TOKEN, planned, "romamo", chosen=False) == ("shipmill-acme", None)
    assert free_name(FakeHub(taken_slugs={"shipmill-acme"}), TOKEN, planned, "romamo", chosen=False) == (
        "shipmill-romamo",
        "shipmill-acme is taken; using shipmill-romamo (--name picks another)",
    )
    assert slug("Shipmill Agent!") == "shipmill-agent"


def test_s006_17_with_both_taken_it_exits_2_suggesting_free_alternatives() -> None:
    acme = Account("acme", "Organization")
    planned = plan((ME, acme), ("acme/web",), "acme", None, None)
    hub = FakeHub(taken_slugs={"shipmill-acme", "shipmill-romamo", "shipmill-acme-agent"})
    with pytest.raises(
        ReleaseError,
        match=r"the App names shipmill-acme and shipmill-romamo are taken; "
        r"free: shipmill-romamo-agent, acme-shipmill; pass one as --name",
    ):
        free_name(hub, TOKEN, planned, "romamo", chosen=False)


def test_s006_17_a_taken_chosen_name_exits_2_suggesting_free_ones() -> None:
    hub = FakeHub(taken_slugs={"bot", "bot-agent"})
    planned = plan((ME, SHIPMILL), ("shipmill/shipmill",), "shipmill", None, "bot")
    with pytest.raises(
        ReleaseError, match=r"the App name bot is taken; free: shipmill-agent, shipmill-romamo, shipmill-romamo-agent;"
    ):
        free_name(hub, TOKEN, planned, "romamo", chosen=True)
    assert free_name(FakeHub(), TOKEN, planned, "romamo", chosen=True) == ("bot", None)


def test_s006_18_the_page_reviews_the_plan_and_waits_for_a_click() -> None:
    planned = plan((ME, CAS, SHIPMILL), ("cli-agent-spec/x", "shipmill/a"), "shipmill", None, "shipmill-agent")
    shown = page(planned, "https://github.com/x", "{}")
    assert "<script" not in shown and "Create on GitHub" in shown
    assert "Public: it can be installed on any account" in shown
    assert "<li>cli-agent-spec/x</li><li>shipmill/a</li>" in shown
    for perm in PERMISSIONS:
        assert f"<td>{perm.label}</td><td>{perm.access.value}</td>" in shown
    assert "We didn't find an App Manifest" in shown and "click the button again" in shown
    private = plan((ME, SHIPMILL), ("shipmill/a",), "shipmill", None, "n")
    assert "Private: it can be installed only on shipmill" in page(private, "a", "b")


class Recorder(io.StringIO):
    """A stream that remembers what had been written at each flush"""

    def __init__(self) -> None:
        super().__init__()
        self.flushed: list[str] = []

    def flush(self) -> None:
        self.flushed.append(self.getvalue())


def test_s006_19_each_line_is_flushed_as_it_happens(tmp_path: Path) -> None:
    stream = Recorder()
    _app_create(tmp_path, args(dry_run=True), FakeHub(), FakeSigner(), TOKEN, out=stream)
    lines = stream.getvalue().splitlines()
    assert lines[0].startswith("looking for the accounts you administer")
    assert [f.count("\n") for f in stream.flushed] == list(range(1, len(lines) + 1))


def test_s006_20_an_interactive_run_asks_for_the_owner(tmp_path: Path) -> None:
    stream = Recorder()
    asked: list[str] = []

    def answer(prompt: str) -> str:
        asked.append(prompt)
        return "3"

    hub = FakeHub(installed=["shipmill/shipmill"])
    browser = Clicker()

    def local(url: str) -> None:
        if url.startswith("http://127.0.0.1"):
            browser(url)

    keys = tmp_path / "keys"
    _app_create(tmp_path, args(), hub, FakeSigner(), TOKEN, local, keys, install_seconds=0, out=stream, ask=answer)
    lines = stream.getvalue().splitlines()
    assert asked == ["Owner [1-3, Enter for romamo]: "]
    assert "  1. romamo (personal) (default)" in lines
    assert "plan: shipmill-agent under shipmill, public" in lines
    assert "  why: shipmill, as chosen; public, since 1 gated repo is elsewhere" in lines
