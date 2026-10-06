"""The GitHub App a gated session writes as (spec 004, D-14): its private key on the host,
the JWT signed with it, and the checks that the App is installed on the gate's repo with
every permission a session needs.

The key is a host secret: shipmill reads its mode, hands its path to `openssl`, and never
reads its bytes, so they can't reach output. The package keeps no dependencies: the JWT is
signed by `openssl dgst -sha256 -sign <key>`, and GitHub is asked over urllib.

A session never holds a token (spec 004, Tokens): its `gh` and git credential helpers mint
one, limited to the gate's repo, at each call, or reuse the one cached in `app-token.json`
(mode 0600) while it has at least 10 minutes left. A token reaches stdout only as
`shipmill app-token`'s answer, which the helpers read; never an error message, a process's
arguments, or a helper's file.
"""

import base64
import datetime as dt
import enum
import http.client
import json
import os
import secrets
import shlex
import shutil
import stat
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from shipmill.errors import ReleaseError

API = "https://api.github.com"
TIMEOUT = 30.0  # seconds a GitHub API call may take
ISSUED_BEFORE = dt.timedelta(seconds=60)  # iat: allows for a host clock ahead of GitHub's
VALID_FOR = dt.timedelta(minutes=9)  # exp: GitHub refuses a JWT valid longer than 10 minutes
_BODY_LIMIT = 1 << 20


class Access(enum.StrEnum):
    READ = "read"
    WRITE = "write"

    def covers(self, need: Access) -> bool:
        """write covers read"""
        return self is Access.WRITE or need is Access.READ


@dataclass(frozen=True, slots=True)
class Permission:
    key: str  # the name GitHub's API gives it
    label: str  # the name GitHub's settings page gives it
    access: Access


# The spec's table: what a session needs on the repo, and nothing more
PERMISSIONS = (
    Permission("contents", "Contents", Access.WRITE),
    Permission("pull_requests", "Pull requests", Access.WRITE),
    Permission("issues", "Issues", Access.WRITE),
    Permission("actions", "Actions", Access.WRITE),
    Permission("workflows", "Workflows", Access.WRITE),
    Permission("checks", "Checks", Access.READ),
    Permission("statuses", "Commit statuses", Access.READ),
    Permission("discussions", "Discussions", Access.READ),
    Permission("metadata", "Metadata", Access.READ),
)


def default_key(app_id: int, home: Path) -> Path:
    """Where the key lives when `--app-key` doesn't say"""
    return home / ".config" / "shipmill" / f"app-{app_id}.pem"


def check_key(path: Path) -> Path:
    """The key's path once it is a file only its owner can read; its bytes are never read here"""
    if not path.is_file():
        raise ReleaseError(f"no App private key at {path}; download it from the App's settings to that path")
    if path.stat().st_mode & (stat.S_IRGRP | stat.S_IROTH):
        raise ReleaseError(f"the App private key {path} is readable by group or others; run `chmod 600 {path}`")
    return path


class Signer(Protocol):
    def sign(self, key: Path, data: bytes) -> bytes:
        """data's RSA SHA-256 signature with the key"""
        ...


class Openssl:
    def __init__(self, command: str = "openssl") -> None:
        self.command = command

    def sign(self, key: Path, data: bytes) -> bytes:
        binary = shutil.which(self.command)
        if binary is None:
            raise ReleaseError(f"{self.command} not found; the App's JWT is signed with `openssl dgst -sha256 -sign`")
        proc = subprocess.run(
            [binary, "dgst", "-sha256", "-sign", str(key)], input=data, capture_output=True, check=False
        )
        if proc.returncode != 0 or not proc.stdout:
            detail = proc.stderr.decode(errors="replace").strip()[:300]
            raise ReleaseError(f"openssl could not sign with the App private key {key}: {detail}")
        return proc.stdout


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def jwt(app_id: int, key: Path, now: dt.datetime, signer: Signer) -> str:
    """An RS256 App JWT, issued 60 seconds before now and valid until 9 minutes after it"""
    header = {"alg": "RS256", "typ": "JWT"}
    claims = {
        "iat": int((now - ISSUED_BEFORE).timestamp()),
        "exp": int((now + VALID_FOR).timestamp()),
        "iss": app_id,
    }
    signing = f"{_b64(json.dumps(header).encode())}.{_b64(json.dumps(claims).encode())}"
    return f"{signing}.{_b64(signer.sign(key, signing.encode('ascii')))}"


@dataclass(frozen=True, slots=True)
class Answer:
    status: int
    body: str


class Api(Protocol):
    def get(self, path: str, token: str | None) -> Answer:
        """GET api.github.com's path with the token as a bearer, or anonymously with None; an
        error status is an Answer"""
        ...

    def post(self, path: str, token: str, body: Mapping[str, object]) -> Answer:
        """POST body as JSON to api.github.com's path with the token as a bearer; an error
        status is an Answer"""
        ...


class UrllibApi:
    def get(self, path: str, token: str | None) -> Answer:
        return self._send("GET", path, token, None)

    def post(self, path: str, token: str, body: Mapping[str, object]) -> Answer:
        return self._send("POST", path, token, json.dumps(body).encode())

    def _send(self, method: str, path: str, token: str | None, data: bytes | None) -> Answer:
        headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": "shipmill-gate",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if token is not None:
            headers["Authorization"] = f"Bearer {token}"
        if data is not None:
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(API + path, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT) as answer:
                return Answer(int(answer.status), answer.read(_BODY_LIMIT).decode(errors="replace"))
        except urllib.error.HTTPError as exc:  # 4xx and 5xx: an answer to report
            body = exc.read(_BODY_LIMIT) if exc.fp is not None else b""
            return Answer(exc.code, body.decode(errors="replace"))
        except (OSError, http.client.HTTPException) as exc:  # URLError and timeouts are OSErrors
            reason = getattr(exc, "reason", exc)
            raise ReleaseError(f"GitHub's API is unreachable for {method} {path}: {reason}") from None


def _json(answer: Answer, path: str, method: str = "GET") -> dict[str, Any]:
    """The answer's JSON object; the message never quotes the body, which may hold a token"""
    try:
        data = json.loads(answer.body)
    except json.JSONDecodeError:
        raise ReleaseError(f"{method} {path} answered {answer.status} without JSON") from None
    if not isinstance(data, dict):
        raise ReleaseError(f"{method} {path} answered {answer.status} with JSON that is not an object")
    return data


def _ok(answer: Answer, path: str, app_id: int, method: str = "GET", status: int = 200) -> dict[str, Any]:
    """The JSON of an answer with the expected status; an error answer's body, which is only
    ever GitHub's message, is quoted"""
    if answer.status == status:
        return _json(answer, path, method)
    message = answer.body.strip()[:300]
    if answer.status == 401:
        raise ReleaseError(
            f"GitHub refused the JWT for app_id {app_id} ({method} {path}: 401 {message}); "
            f"check that the private key is App {app_id}'s and the host's clock is right"
        )
    raise ReleaseError(f"{method} {path} for app_id {app_id} answered {answer.status}: {message}")


@dataclass(frozen=True, slots=True)
class Installation:
    """The App as GitHub knows it, installed on the gate's repo"""

    slug: str
    id: int
    permissions: Mapping[str, Access]

    @property
    def bot(self) -> str:
        return f"{self.slug}[bot]"


def missing(granted: Mapping[str, Access]) -> list[Permission]:
    """The table's permissions the installation lacks, or grants only read where write is needed"""
    return [p for p in PERMISSIONS if (have := granted.get(p.key)) is None or not have.covers(p.access)]


_ACCESS = {"read": Access.READ, "write": Access.WRITE, "admin": Access.WRITE}  # admin covers write


def _permissions(raw: object, path: str) -> dict[str, Access]:
    if not isinstance(raw, dict):
        raise ReleaseError(f"GET {path}: permissions is not an object")
    granted = {}
    for key, value in raw.items():
        if not isinstance(key, str) or not isinstance(value, str):
            raise ReleaseError(f"GET {path}: permission {key!r} has access {value!r}, not a string")
        if value not in _ACCESS:
            raise ReleaseError(f"GET {path}: permission {key} has unknown access {value!r}")
        granted[key] = _ACCESS[value]
    return granted


def installation(app_id: int, repo: str, token: str, api: Api) -> Installation:
    """Spec 004, At launch step 3: the App's slug, then its installation on repo, which must
    grant every permission in the table"""
    app = _ok(api.get("/app", token), "/app", app_id)
    slug = app.get("slug")
    if not isinstance(slug, str) or not slug:
        raise ReleaseError(f"GET /app for app_id {app_id} gave no slug")
    path = f"/repos/{repo}/installation"
    answer = api.get(path, token)
    if answer.status == 404:
        raise ReleaseError(f"app {slug} is not installed on {repo}")
    found = _ok(answer, path, app_id)
    number = found.get("id")
    if not isinstance(number, int) or isinstance(number, bool):
        raise ReleaseError(f"GET {path} gave no installation id")
    granted = _permissions(found.get("permissions"), path)
    if lacking := missing(granted):
        names = ", ".join(f"{p.label}: {p.access}" for p in lacking)
        raise ReleaseError(
            f"app {slug} on {repo} lacks these permissions: {names}; grant them in the App's settings, "
            "then accept them on the installation"
        )
    return Installation(slug, number, granted)


NOREPLY = "users.noreply.github.com"


@dataclass(frozen=True, slots=True)
class Identity:
    """Who a gated session writes as: the App's bot account, and the key its helpers mint with"""

    app_id: int
    slug: str
    bot_id: int  # the bot account's user id, not the App's id
    key: Path

    @property
    def login(self) -> str:
        return f"{self.slug}[bot]"

    @property
    def email(self) -> str:
        """The bot's noreply address, which GitHub links its commits to"""
        return f"{self.bot_id}+{self.login}@{NOREPLY}"


def bot_id(slug: str, app_id: int, api: Api) -> int:
    """Spec 004, At launch step 4: the user id of the App's bot account. A public read, made
    without the JWT, which GitHub accepts only on the App's own endpoints"""
    login = f"{slug}[bot]"
    path = f"/users/{urllib.parse.quote(login)}"
    answer = api.get(path, None)
    if answer.status == 404:
        raise ReleaseError(f"GitHub has no bot account {login} for app_id {app_id}")
    found = _ok(answer, path, app_id)
    number = found.get("id")
    if not isinstance(number, int) or isinstance(number, bool) or number < 1:
        raise ReleaseError(f"GET {path} gave no user id")
    if found.get("login") != login:
        raise ReleaseError(f"GET {path} answered for {found.get('login')!r}, not {login}")
    return number


AppCheck = Callable[[int, dt.datetime], Identity]


def app_check(repo: str, key: Path | None, home: Path, signer: Signer, api: Api) -> AppCheck:
    """The gate's App check for repo, spec 004's At launch steps 1 to 4: the key (`--app-key`,
    else the default path for the App's id), the JWT, the installation, then the bot account.
    Any failure raises ReleaseError (exit 2). It writes nothing, so a dry run runs it too"""

    def check(app_id: int, now: dt.datetime) -> Identity:
        path = check_key(key if key is not None else default_key(app_id, home))
        found = installation(app_id, repo, jwt(app_id, path, now, signer), api)
        return Identity(app_id, found.slug, bot_id(found.slug, app_id, api), path)

    return check


CACHE = "app-token.json"  # in the gate's state folder, `$(git rev-parse --git-common-dir)/shipmill`
REUSE_FOR = dt.timedelta(minutes=10)  # a cached token is reused while it has at least this long left


def repo_name(repo: str) -> str:
    """The name in owner/name, which the token is limited to"""
    owner, slash, name = repo.partition("/")
    if not owner or not slash or not name or "/" in name:
        raise ReleaseError(f"the repo is owner/name, such as romamo/demo; got {repo!r}")
    return name


@dataclass(frozen=True, slots=True)
class Token:
    """An installation token for one repository; repr leaves the value out"""

    value: str = field(repr=False)
    expires: dt.datetime
    repo: str
    app_id: int

    def reusable(self, repo: str, app_id: int, now: dt.datetime) -> bool:
        return self.repo.lower() == repo.lower() and self.app_id == app_id and self.expires - now >= REUSE_FOR


def _malformed(path: Path, reason: str) -> ReleaseError:
    return ReleaseError(f"the App token cache {path} is malformed ({reason}); delete it to mint a new token")


def _expiry(value: object) -> dt.datetime | None:
    if not isinstance(value, str):
        return None
    try:
        moment = dt.datetime.fromisoformat(value)
    except ValueError:
        return None
    return moment if moment.tzinfo is not None else None


def load_token(path: Path) -> Token | None:
    """The cached token, or None when there is no cache; a malformed one raises naming it.
    No message quotes the file, which holds the token"""
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_bytes())
    except json.JSONDecodeError, UnicodeDecodeError:
        raise _malformed(path, "not JSON") from None
    if not isinstance(data, dict):
        raise _malformed(path, "not a JSON object")
    value, repo, app_id = data.get("token"), data.get("repository"), data.get("app_id")
    if not isinstance(value, str) or not value:
        raise _malformed(path, "token is not a non-empty string")
    if not isinstance(repo, str) or not repo:
        raise _malformed(path, "repository is not a non-empty string")
    if not isinstance(app_id, int) or isinstance(app_id, bool):
        raise _malformed(path, "app_id is not an integer")
    expires = _expiry(data.get("expires_at"))
    if expires is None:
        raise _malformed(path, "expires_at is not an ISO time with an offset")
    return Token(value, expires, repo, app_id)


def _write_atomic(path: Path, data: bytes, mode: int) -> None:
    """Write path through a sibling created with mode (never wider, so no chmod follows) and
    renamed over it, so a reader sees the old file or the new one, never part of one"""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{secrets.token_hex(4)}")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    done = False
    try:
        with os.fdopen(descriptor, "wb") as out:
            out.write(data)
            out.flush()
            os.fsync(out.fileno())
        os.replace(temporary, path)
        done = True
    finally:
        if not done:
            temporary.unlink(missing_ok=True)


def save_token(path: Path, token: Token) -> None:
    """Cache the token with mode 0600; two helpers minting at once each write a whole, valid
    token, and the later rename wins"""
    record = {
        "app_id": token.app_id,
        "repository": token.repo,
        "expires_at": token.expires.isoformat(),
        "token": token.value,
    }
    _write_atomic(path, (json.dumps(record, indent=2) + "\n").encode(), 0o600)


def _scope(data: Mapping[str, object], repo: str, path: str) -> None:
    """D-14: the token must be limited to the gate's repo, as asked"""
    repos = data.get("repositories")
    if not isinstance(repos, list) or len(repos) != 1:
        raise ReleaseError(f"POST {path} gave a token not limited to {repo}; refusing it")
    only = repos[0]
    full = only.get("full_name") if isinstance(only, dict) else None
    if not isinstance(full, str) or full.lower() != repo.lower():
        raise ReleaseError(f"POST {path} gave a token for another repository than {repo}; refusing it")


def mint(app_id: int, repo: str, key: Path, now: dt.datetime, signer: Signer, api: Api) -> Token:
    """Spec 004, Tokens: an installation token limited to repo and the table's permissions"""
    name = repo_name(repo)
    bearer = jwt(app_id, check_key(key), now, signer)
    found = installation(app_id, repo, bearer, api)
    path = f"/app/installations/{found.id}/access_tokens"
    body = {"repositories": [name], "permissions": {p.key: str(p.access) for p in PERMISSIONS}}
    data = _ok(api.post(path, bearer, body), path, app_id, "POST", 201)
    value = data.get("token")
    if not isinstance(value, str) or not value or any(c.isspace() for c in value):
        raise ReleaseError(f"POST {path} gave no token")
    expires = _expiry(data.get("expires_at"))
    if expires is None:
        raise ReleaseError(f"POST {path} gave no expires_at with an offset")
    _scope(data, repo, path)
    return Token(value, expires, repo, app_id)


def app_token(cache: Path, repo: str, app_id: int, key: Path, now: dt.datetime, signer: Signer, api: Api) -> str:
    """The cached token while it has at least 10 minutes left by this host's clock, else a new
    one, cached. GitHub's expiry is an hour out, so the margin also absorbs a host clock up to
    10 minutes behind GitHub's"""
    repo_name(repo)
    cached = load_token(cache)
    if cached is not None and cached.reusable(repo, app_id, now):
        return cached.value
    fresh = mint(app_id, repo, key, now, signer, api)
    save_token(cache, fresh)
    return fresh.value


CREDENTIAL_HOST = "github.com"


def credential(operation: str, request: str, token: Callable[[], str]) -> str:
    """git-credential(1)'s helper protocol. get answers an https request for github.com with
    the App's token as x-access-token, and anything else with nothing, so git asks its next
    helper; store, erase, and any other operation are ignored, as the protocol asks"""
    if operation != "get":
        return ""
    asked: dict[str, str] = {}
    for line in request.splitlines():
        if not line:
            break
        key, equals, value = line.partition("=")
        if not equals or not key:
            raise ReleaseError("git sent a credential request line that is not key=value")
        asked[key] = value
    if asked.get("protocol") != "https" or asked.get("host") != CREDENTIAL_HOST:
        return ""
    return f"username=x-access-token\npassword={token()}\n"


HELPERS = "bin"  # in the gate's state folder, next to the token cache
GH_HELPER = "gh"
CREDENTIAL_HELPER = "git-credential-shipmill"


@dataclass(frozen=True, slots=True)
class Helpers:
    folder: Path  # goes first on a session's PATH
    gh: Path
    git_credential: Path
    real_gh: Path  # the gh the gh helper runs


def resolve_gh(folder: Path, path: str) -> Path:
    """The gh on path, skipping the helpers' folder, so the gh helper never runs itself"""
    mine = folder.resolve()
    rest = [entry for entry in path.split(os.pathsep) if entry and Path(entry).resolve() != mine]
    found = shutil.which("gh", path=os.pathsep.join(rest))
    if found is None:
        raise ReleaseError("gh not found on PATH; the session's gh helper runs it with the App's token")
    return Path(found).resolve()


def write_helpers(folder: Path, python: Path, checkout: Path, repo: str, app_id: int, key: Path, path: str) -> Helpers:
    """Spec 004, Tokens: write the session's gh and git credential helpers, mode 0700. Each
    runs `<python> -m shipmill app-token` with the App's id, key, repo, and checkout as
    arguments; neither holds a token"""
    repo_name(repo)
    real_gh = resolve_gh(folder, path)
    command = shlex.join(
        [
            str(python),
            "-m",
            "shipmill",
            "--repo",
            str(checkout.resolve()),
            "app-token",
            repo,
            "--app-id",
            str(app_id),
            "--app-key",
            str(key.resolve()),
        ]
    )
    header = "#!/bin/sh\n# Written by shipmill gate (spec 004). It holds no token: each call mints or reuses one.\n"
    gh = (
        header
        + f"token=$({command}) || exit $?\n"
        + "GH_TOKEN=$token\nexport GH_TOKEN\nunset token\n"
        + f'exec {shlex.quote(str(real_gh))} "$@"\n'
    )
    credential_helper = header + f'exec {command} --git-credential "$1"\n'
    helpers = Helpers(folder, folder / GH_HELPER, folder / CREDENTIAL_HELPER, real_gh)
    _write_atomic(helpers.gh, gh.encode(), 0o700)
    _write_atomic(helpers.git_credential, credential_helper.encode(), 0o700)
    return helpers


CREDENTIAL_KEY = "credential.https://github.com.helper"
INSTEAD_OF = "url.https://github.com/.insteadOf"
SSH_REMOTES = ("git@github.com:", "ssh://git@github.com/")  # sent over https, so a push goes through the App


def _helper_value(helper: Path) -> str:
    """credential.helper's value for an absolute path: git runs it through the shell, so a
    path that needs quoting goes in as a `!` shell command"""
    quoted = shlex.quote(str(helper))
    return quoted if quoted == str(helper) else f"!{quoted}"


def session_env(identity: Identity, helpers: Helpers, path: str) -> dict[str, str]:
    """Spec 004, At launch step 5: the env a session's `--settings` carries. The helpers'
    folder goes first on PATH, the git author and committer are the App's bot, and git's
    config, through GIT_CONFIG_*, drops the host's credential helpers for github.com, adds
    the App's, and sends SSH remotes over https. It holds no token"""
    config = [
        (CREDENTIAL_KEY, ""),
        (CREDENTIAL_KEY, _helper_value(helpers.git_credential)),
        *((INSTEAD_OF, remote) for remote in SSH_REMOTES),
    ]
    env = {
        "PATH": os.pathsep.join([str(helpers.folder), path]) if path else str(helpers.folder),
        "GIT_AUTHOR_NAME": identity.login,
        "GIT_AUTHOR_EMAIL": identity.email,
        "GIT_COMMITTER_NAME": identity.login,
        "GIT_COMMITTER_EMAIL": identity.email,
        "GIT_CONFIG_COUNT": str(len(config)),
    }
    for index, (key, value) in enumerate(config):
        env[f"GIT_CONFIG_KEY_{index}"] = key
        env[f"GIT_CONFIG_VALUE_{index}"] = value
    return env


def prepare_session(
    identity: Identity, folder: Path, python: Path, checkout: Path, repo: str, path: str
) -> dict[str, str]:
    """Write the session's helpers to folder, then give the env that puts them to work.
    python is the gate's own interpreter, so the helpers run the same shipmill"""
    helpers = write_helpers(folder.resolve(), python, checkout, repo, identity.app_id, identity.key, path)
    return session_env(identity, helpers, path)
