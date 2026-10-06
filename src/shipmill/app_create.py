"""Spec 006: create the gate's GitHub App in one click.

Discovers the accounts the host's gh login administers and the repos in them that hold
`.github/shipmill.toml`, plans the App's owner and visibility from where those repos are,
creates the App through GitHub's manifest flow (the maintainer clicks Create on GitHub),
saves its private key with mode 0600, and waits for it to be installed on the repos.

The key is a host secret: it is written once, never printed, and never overwritten. The
conversion's client secret, webhook secret, and client id are dropped unread. No repo's
config is edited (D-4): the command prints the `app_id` line to commit.
"""

import html
import json
import os
import re
import secrets
import stat
import subprocess
import threading
import urllib.parse
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

from shipmill.app import PERMISSIONS, Api
from shipmill.errors import ReleaseError

CONFIG = ".github/shipmill.toml"
HOMEPAGE = "https://github.com/shipmill/shipmill"  # S-006-9: the App's page links to shipmill
FLOW_SECONDS = 600  # how long the create and the install steps each wait
POLL_SECONDS = 5
_PAGE = 100


@dataclass(frozen=True, slots=True)
class Account:
    login: str
    kind: str  # Organization or User, as GitHub names them

    @property
    def is_org(self) -> bool:
        return self.kind == "Organization"


@dataclass(frozen=True, slots=True)
class Plan:
    owner: Account
    public: bool
    name: str
    reason: str
    repos: tuple[str, ...]  # the gated repos, owner/name, sorted
    warnings: tuple[str, ...] = ()

    @property
    def installable(self) -> tuple[str, ...]:
        """The gated repos the App can be installed on: all of them when public"""
        return tuple(r for r in self.repos if self.public or _owner(r) == self.owner.login)

    def lines(self) -> list[str]:
        visibility = "public" if self.public else "private"
        lines = [f"plan: {self.name} under {self.owner.login}, {visibility}", f"  why: {self.reason}"]
        lines.append(f"  repos: {', '.join(self.repos) if self.repos else '(none found)'}")
        return lines + [f"warning: {w}" for w in self.warnings]

    def record(self) -> dict[str, object]:
        return {
            "owner": self.owner.login,
            "owner_type": self.owner.kind,
            "public": self.public,
            "name": self.name,
            "reason": self.reason,
            "repos": list(self.repos),
        }


def _owner(repo: str) -> str:
    return repo.partition("/")[0]


def _get(api: Api, path: str, token: str) -> Any:
    """The JSON of a 200 answer; any other status exits 2 quoting GitHub's message"""
    answer = api.get(path, token)
    if answer.status != 200:
        raise ReleaseError(f"GET {path} answered {answer.status}: {answer.body.strip()[:300]}")
    try:
        return json.loads(answer.body)
    except json.JSONDecodeError:
        raise ReleaseError(f"GET {path} answered without JSON") from None


def _pages(api: Api, path: str, token: str) -> list[dict[str, Any]]:
    """Every item of a listed resource, page by page"""
    items: list[dict[str, Any]] = []
    sep = "&" if "?" in path else "?"
    page = 1
    while True:
        batch = _get(api, f"{path}{sep}per_page={_PAGE}&page={page}", token)
        if not isinstance(batch, list):
            raise ReleaseError(f"GET {path} gave no list")
        items += [b for b in batch if isinstance(b, dict)]
        if len(batch) < _PAGE:
            return items
        page += 1


def accounts(api: Api, token: str) -> tuple[Account, ...]:
    """S-006-1: the login, then each org it administers, by name"""
    user = _get(api, "/user", token)
    login = user.get("login") if isinstance(user, dict) else None
    if not isinstance(login, str) or not login:
        raise ReleaseError("GET /user gave no login")
    if api.get("/user/memberships/orgs?per_page=1", token).status in (401, 403, 404):
        raise ReleaseError("gh's token can't read your org memberships; run `gh auth refresh -s read:org`")
    orgs = []
    for m in _pages(api, "/user/memberships/orgs", token):
        org = m.get("organization")
        name = org.get("login") if isinstance(org, dict) else None
        if m.get("state") == "active" and m.get("role") == "admin" and isinstance(name, str):
            orgs.append(Account(name, "Organization"))
    return (Account(login, "User"), *sorted(orgs, key=lambda a: a.login.lower()))


def gated_repos(api: Api, token: str, found: Sequence[Account]) -> tuple[str, ...]:
    """S-006-2: the unarchived repos of the accounts with the config on their default branch"""
    gated = []
    for account in found:
        path = f"/orgs/{account.login}/repos" if account.is_org else "/user/repos?affiliation=owner"
        for repo in _pages(api, path, token):
            name = repo.get("full_name")
            if not isinstance(name, str) or repo.get("archived") is True:
                continue
            answer = api.get(f"/repos/{name}/contents/{CONFIG}", token)
            if answer.status == 200:
                gated.append(name)
            elif answer.status != 404:
                raise ReleaseError(f"can't tell whether {name} has {CONFIG}: GET answered {answer.status}")
    return tuple(sorted(gated, key=str.lower))


def given_repos(repos: Sequence[str], found: Sequence[Account]) -> tuple[str, ...]:
    """S-006-3: --repos, each in an account the login administers"""
    logins = {a.login.lower() for a in found}
    for repo in repos:
        owner, _, name = repo.partition("/")
        if not owner or not name or "/" in name:
            raise ReleaseError(f"--repos takes owner/name, got {repo!r}")
        if owner.lower() not in logins:
            raise ReleaseError(f"{repo} is in {owner}, which your gh login doesn't administer")
    return tuple(sorted(set(repos), key=str.lower))


def plan(
    found: Sequence[Account],
    repos: Sequence[str],
    owner: str | None,
    public: bool | None,
    name: str | None,
) -> Plan:
    """S-006-4 to S-006-7: the App is the login's own unless --owner names an org it
    administers, and public only when a gated repo is outside the owner, since a private App
    installs on its owner alone"""
    if owner is None:
        chosen = found[0]
        reason = "your personal account (--owner picks an org you administer)"
    else:
        match = {a.login.lower(): a for a in found}.get(owner.lower())
        if match is None:
            raise ReleaseError(
                f"your gh login doesn't administer {owner}; creating an App under an org needs its owner role"
            )
        chosen, reason = match, f"{match.login}, as chosen"
    outside = [r for r in repos if _owner(r).lower() != chosen.login.lower()]
    if public is None:
        public = bool(outside)
        if outside:
            reason += f"; public, since {len(outside)} gated repo{'s are' if len(outside) > 1 else ' is'} elsewhere"
    warnings: tuple[str, ...] = ()
    if not public:
        warnings = tuple(f"a private App can't be installed on {r}, outside {chosen.login}" for r in outside)
    return Plan(chosen, public, name or default_name(chosen.login), reason, tuple(repos), warnings)


def owner_menu(found: Sequence[Account], repos: Sequence[str]) -> list[str]:
    """S-006-20: the accounts to pick the owner from, the login first as the default"""
    counts = Counter(_owner(r).lower() for r in repos)
    lines = ["Who should own the App?"]
    for index, account in enumerate(found, 1):
        kind = "personal" if not account.is_org else "org"
        n = counts.get(account.login.lower(), 0)
        gated = f", {n} gated repo{'s' if n != 1 else ''}" if n else ""
        default = " (default)" if index == 1 else ""
        lines.append(f"  {index}. {account.login} ({kind}{gated}){default}")
    return lines


def pick_owner(found: Sequence[Account], answer: str) -> str:
    """S-006-20: the menu's answer: empty for the default, else a number or a login"""
    text = answer.strip()
    if not text:
        return found[0].login
    if text.isdigit() and 1 <= int(text) <= len(found):
        return found[int(text) - 1].login
    for account in found:
        if account.login.lower() == text.lower():
            return account.login
    raise ReleaseError(f"no account {text!r} in the list; pass --owner")


def slug(name: str) -> str:
    """The slug GitHub makes of an App's name: lowercase, each run of other characters a dash"""
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def taken(api: Api, token: str, name: str) -> bool:
    """S-006-17: whether an App already uses the name's slug"""
    answer = api.get(f"/apps/{slug(name)}", token)
    if answer.status not in (200, 404):
        raise ReleaseError(f"can't tell whether the App name {name} is free: GET answered {answer.status}")
    return answer.status == 200


def default_name(owner: str) -> str:
    """S-006-17: shipmill-<owner>; shipmill's own App is shipmill-agent, not shipmill-shipmill"""
    return "shipmill-agent" if owner.lower() == "shipmill" else f"shipmill-{owner.lower()}"


def free_name(api: Api, token: str, p: Plan, login: str, chosen: bool) -> tuple[str, str | None]:
    """S-006-17: a chosen --name when free; by default the first free of shipmill-<owner> and
    shipmill-<login>, with a note when it isn't the first. Otherwise exits 2 naming free
    alternatives to pass as --name"""
    if chosen:
        if not taken(api, token, p.name):
            return p.name, None
        tried = [p.name]
    else:
        candidates = list(dict.fromkeys([p.name, default_name(login)]))
        for index, name in enumerate(candidates):
            if not taken(api, token, name):
                note = None if index == 0 else f"{candidates[0]} is taken; using {name} (--name picks another)"
                return name, note
        tried = candidates
    owner, me = p.owner.login.lower(), login.lower()
    extra = [f"shipmill-{who}-agent" for who in (owner, me) if who != "shipmill"]
    extra += [f"{owner}-shipmill"] if owner != "shipmill" else []
    alternatives = [n for n in dict.fromkeys([default_name(owner), default_name(me), *extra]) if n not in tried]
    free = [n for n in alternatives if not taken(api, token, n)]
    hint = f"; free: {', '.join(free)}; pass one as --name" if free else "; pass another --name"
    names = f"names {' and '.join(tried)} are" if len(tried) > 1 else f"name {tried[0]} is"
    raise ReleaseError(f"the App {names} taken{hint}")


def manifest(p: Plan, redirect: str) -> dict[str, object]:
    """S-006-9: exactly the permissions the gate checks, no webhook"""
    return {
        "name": p.name,
        "url": HOMEPAGE,
        "description": "Gated Claude Code sessions started by shipmill gate",
        "public": p.public,
        "redirect_url": redirect,
        "hook_attributes": {"url": "https://example.invalid/shipmill-no-webhook", "active": False},
        "default_permissions": {perm.key: perm.access.value for perm in PERMISSIONS},
        "default_events": [],
    }


def new_app_url(p: Plan, state: str) -> str:
    base = (
        f"https://github.com/organizations/{p.owner.login}/settings/apps/new"
        if p.owner.is_org
        else "https://github.com/settings/apps/new"
    )
    return f"{base}?state={urllib.parse.quote(state)}"


def check_key_dir(key_dir: Path) -> None:
    """S-006-11: refuse a key folder others can read before anything is created"""
    if key_dir.exists() and key_dir.stat().st_mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise ReleaseError(f"{key_dir} is readable by group or others; run `chmod 700 {key_dir}`")


def save_key(key_dir: Path, app_id: int, pem: str) -> Path:
    """S-006-11: mode 0600 in a 0700 folder, never over an existing file"""
    key_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = key_dir / f"app-{app_id}.pem"
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        raise ReleaseError(f"{path} exists; shipmill never overwrites an App key") from None
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(pem)
    return path


@dataclass(frozen=True, slots=True)
class Created:
    app_id: int
    slug: str
    key: Path


def convert(code: str, api: Api, token: str, key_dir: Path) -> Created:
    """S-006-11: the manifest code for the App's id, slug, and key; the secrets are dropped"""
    path = f"/app-manifests/{urllib.parse.quote(code)}/conversions"
    answer = api.post(path, token, {})
    if answer.status != 201:
        raise ReleaseError(f"POST {path} answered {answer.status}; the code may have expired")
    try:
        data = json.loads(answer.body)
    except json.JSONDecodeError:
        raise ReleaseError(f"POST {path} answered without JSON") from None
    app_id, slug, pem = data.get("id"), data.get("slug"), data.get("pem")
    if not isinstance(app_id, int) or isinstance(app_id, bool) or not isinstance(slug, str) or not isinstance(pem, str):
        raise ReleaseError(f"POST {path} gave no App id, slug, or key")
    return Created(app_id, slug, save_key(key_dir, app_id, pem))


@dataclass
class _Flow:
    """The one-callback server's state"""

    form: str
    state: str
    code: str | None = None
    done: threading.Event = field(default_factory=threading.Event)


def _handler(flow: _Flow) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def _page(self, status: int, body: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            style = "font:16px system-ui,sans-serif;margin:2em"
            self.wfile.write(f"<!doctype html><meta charset=utf-8><body style='{style}'>{body}".encode())

        def do_GET(self) -> None:
            url = urllib.parse.urlparse(self.path)
            if url.path == "/":
                self._page(200, flow.form)
                return
            if url.path == "/callback":
                query = urllib.parse.parse_qs(url.query)
                if query.get("state") != [flow.state] or not query.get("code"):
                    self._page(400, "This callback isn't from the App shipmill asked for; nothing was saved.")
                    return
                flow.code = query["code"][0]
                self._page(200, "GitHub created the App. Return to the terminal to install it.")
                flow.done.set()
                return
            self._page(404, "Not found")

        def log_message(self, format: str, *args: object) -> None:
            return

    return Handler


# What each permission lets the App's bot do, in the words the review page uses
WHY = {
    "contents": "push branches and merge pull requests",
    "pull_requests": "open, review, and merge pull requests",
    "issues": "comment on, label, and close issues",
    "actions": "rerun a failed release job",
    "workflows": "change files under .github/workflows/",
    "checks": "read CI results",
    "statuses": "read CI results",
    "discussions": "read discussions for product intake",
    "metadata": "read the repo's basic details (GitHub requires it)",
}


def page(p: Plan, action: str, body: str) -> str:
    """S-006-18: the plan, what the App may do, and a button; nothing is sent to GitHub until
    the maintainer clicks. action and body are already HTML-escaped"""
    visibility = (
        "Public: it can be installed on any account, but only your key can act as it"
        if p.public
        else f"Private: it can be installed only on {html.escape(p.owner.login)}"
    )
    repos = "".join(f"<li>{html.escape(r)}</li>" for r in p.installable) or "<li>none yet</li>"
    rows = "".join(
        f"<tr><td>{html.escape(perm.label)}</td><td>{perm.access.value}</td><td>{html.escape(WHY[perm.key])}</td></tr>"
        for perm in PERMISSIONS
    )
    return (
        f"<h1>Create {html.escape(p.name)}</h1>"
        f"<p>shipmill will ask GitHub to create a GitHub App named <b>{html.escape(p.name)}</b> owned by "
        f"<b>{html.escape(p.owner.login)}</b>. Every field on GitHub's page is filled in for you: "
        "the name, the homepage, the permissions below, and no webhook.</p>"
        f"<p>{visibility}.</p><p>You will install it on:</p><ul>{repos}</ul>"
        "<table><tr><th align=left>Permission</th><th align=left>Access</th><th align=left>Used to</th></tr>"
        f"{rows}</table>"
        f"<form method=post action='{action}'><input type=hidden name=manifest value=\"{body}\">"
        "<p><button style='font-size:1.2em;padding:.5em 1em'>Create on GitHub</button></p></form>"
        "<p>On GitHub, click <b>Create GitHub App</b>. If GitHub first asks you to sign in or confirm your "
        "password, it loses the request and shows <i>We didn't find an App Manifest</i>: come back to this "
        "page and click the button again.</p>"
    )


def create(p: Plan, browser: Callable[[str], None], seconds: float = FLOW_SECONDS) -> str:
    """S-006-9, S-006-10, S-006-12, S-006-18: serve the review page on 127.0.0.1, wait for
    GitHub's callback, and return its code"""
    state = secrets.token_urlsafe(24)
    server = HTTPServer(("127.0.0.1", 0), BaseHTTPRequestHandler)
    port = server.server_address[1]
    body = html.escape(json.dumps(manifest(p, f"http://127.0.0.1:{port}/callback")), quote=True)
    action = html.escape(new_app_url(p, state), quote=True)
    form = page(p, action, body)
    flow = _Flow(form, state)
    server.RequestHandlerClass = _handler(flow)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        browser(f"http://127.0.0.1:{port}/")
        if not flow.done.wait(seconds) or flow.code is None:
            raise ReleaseError(f"no App was created: GitHub didn't call back within {int(seconds // 60)} minutes")
        return flow.code
    finally:
        server.shutdown()
        server.server_close()


def host_token(run: Callable[[list[str]], str] | None = None) -> str:
    """The host's gh login token, for the reads and the conversion; never printed"""

    def gh(args: list[str]) -> str:
        proc = subprocess.run(args, capture_output=True, text=True, check=False)
        if proc.returncode != 0:
            raise ReleaseError("gh is not signed in; run `gh auth login`")
        return proc.stdout

    token = (run or gh)(["gh", "auth", "token"]).strip()
    if not token:
        raise ReleaseError("gh is not signed in; run `gh auth login`")
    return token


def config_lines(created: Created) -> list[str]:
    """S-006-14: what to commit, and how to prove it"""
    return [
        f"created {created.slug} (App ID {created.app_id}), key in {created.key}",
        "add to [agents] in each repo's .github/shipmill.toml:",
        f"  app_id = {created.app_id}",
        "then prove it: shipmill --repo <gate checkout> gate <owner/repo> --dry-run",
    ]
