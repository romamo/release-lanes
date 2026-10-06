"""The GitHub App a gated session writes as (spec 004, D-14): its private key on the host,
the JWT signed with it, and the checks that the App is installed on the gate's repo with
every permission a session needs.

The key is a host secret: shipmill reads its mode, hands its path to `openssl`, and never
reads its bytes, so they can't reach output. The package keeps no dependencies: the JWT is
signed by `openssl dgst -sha256 -sign <key>`, and GitHub is asked over urllib.
"""

import base64
import datetime as dt
import enum
import http.client
import json
import shutil
import stat
import subprocess
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
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
    def get(self, path: str, token: str) -> Answer:
        """GET api.github.com's path with the token as a bearer; an error status is an Answer"""
        ...


class UrllibApi:
    def get(self, path: str, token: str) -> Answer:
        request = urllib.request.Request(
            API + path,
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {token}",
                "User-Agent": "shipmill-gate",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT) as answer:
                return Answer(int(answer.status), answer.read(_BODY_LIMIT).decode(errors="replace"))
        except urllib.error.HTTPError as exc:  # 4xx and 5xx: an answer to report
            body = exc.read(_BODY_LIMIT) if exc.fp is not None else b""
            return Answer(exc.code, body.decode(errors="replace"))
        except (OSError, http.client.HTTPException) as exc:  # URLError and timeouts are OSErrors
            raise ReleaseError(f"GitHub's API is unreachable for GET {path}: {getattr(exc, 'reason', exc)}") from None


def _json(answer: Answer, path: str) -> dict[str, Any]:
    try:
        data = json.loads(answer.body)
    except json.JSONDecodeError:
        raise ReleaseError(f"GET {path} answered {answer.status} without JSON") from None
    if not isinstance(data, dict):
        raise ReleaseError(f"GET {path} answered {answer.status} with JSON that is not an object")
    return data


def _ok(answer: Answer, path: str, app_id: int) -> dict[str, Any]:
    if answer.status == 200:
        return _json(answer, path)
    message = answer.body.strip()[:300]
    if answer.status == 401:
        raise ReleaseError(
            f"GitHub refused the JWT for app_id {app_id} (GET {path}: 401 {message}); "
            f"check that the private key is App {app_id}'s and the host's clock is right"
        )
    raise ReleaseError(f"GET {path} for app_id {app_id} answered {answer.status}: {message}")


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


AppCheck = Callable[[int, dt.datetime], Installation]


def app_check(repo: str, key: Path | None, home: Path, signer: Signer, api: Api) -> AppCheck:
    """The gate's App check for repo: the key (`--app-key`, else the default path for the
    App's id), the JWT, then the installation. Any failure raises ReleaseError (exit 2)"""

    def check(app_id: int, now: dt.datetime) -> Installation:
        path = check_key(key if key is not None else default_key(app_id, home))
        return installation(app_id, repo, jwt(app_id, path, now, signer), api)

    return check
