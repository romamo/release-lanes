"""Spec 007: guide installing the gate's GitHub App on more repos.

GitHub has no API to install an App, and adding a repo to an installation refuses the gh
login's token, so shipmill never makes the click: for the repos the App doesn't cover it
opens the App's Install App page, says per account whether to Install or Configure and
which repos to pick, and checks with the App's JWT until each is covered.
"""

import datetime as dt
import json
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from shipmill.app import Api, Signer, jwt
from shipmill.errors import ReleaseError

POLL_SECONDS = 5
WAIT_SECONDS = 600


@dataclass(frozen=True, slots=True)
class Installed:
    """One installation of the App, as GET /app/installations lists it"""

    id: int
    account: str
    is_org: bool
    selected: bool  # repository_selection is "selected", not "all"


@dataclass(frozen=True, slots=True)
class Step:
    """What one account needs so its repos are covered"""

    account: str
    action: str  # install, or add to the installation there
    repos: tuple[str, ...]

    def line(self) -> str:
        picked = ", ".join(self.repos)
        if self.action == "install":
            return f"  {self.account}: click Install, choose Only select repositories, pick {picked}"
        return f"  {self.account}: click Configure, add {picked} under Repository access, Save"


def _json(api: Api, path: str, token: str | None) -> Any:
    answer = api.get(path, token)
    if answer.status != 200:
        raise ReleaseError(f"GET {path} answered {answer.status}: {answer.body.strip()[:300]}")
    try:
        return json.loads(answer.body)
    except json.JSONDecodeError:
        raise ReleaseError(f"GET {path} answered without JSON") from None


def app_page(api: Api, token: str) -> tuple[str, str]:
    """S-007-4: the App's slug and its Install App page, which lists every account with an
    Install or Configure button"""
    data = _json(api, "/app", token)
    slug, owner = data.get("slug"), data.get("owner") or {}
    login = owner.get("login")
    if not isinstance(slug, str) or not slug or not isinstance(login, str):
        raise ReleaseError("GET /app gave no slug or owner")
    if owner.get("type") == "Organization":
        return slug, f"https://github.com/organizations/{login}/settings/apps/{slug}/installations"
    return slug, f"https://github.com/settings/apps/{slug}/installations"


def installations(api: Api, token: str) -> dict[str, Installed]:
    """The App's installations by account login, lowercased"""
    found: dict[str, Installed] = {}
    page = 1
    while True:
        batch = _json(api, f"/app/installations?per_page=100&page={page}", token)
        if not isinstance(batch, list):
            raise ReleaseError("GET /app/installations gave no list")
        for item in batch:
            account = item.get("account") or {}
            login, number = account.get("login"), item.get("id")
            if isinstance(login, str) and isinstance(number, int):
                found[login.lower()] = Installed(
                    number, login, account.get("type") == "Organization", item.get("repository_selection") == "selected"
                )
        if len(batch) < 100:
            return found
        page += 1


def covered(api: Api, token: str, repo: str) -> bool:
    """S-007-3: whether the App's installation covers the repo"""
    path = f"/repos/{repo}/installation"
    answer = api.get(path, token)
    if answer.status not in (200, 404):
        raise ReleaseError(f"can't tell whether the App covers {repo}: GET {path} answered {answer.status}")
    return answer.status == 200


def steps(missing: Sequence[str], found: dict[str, Installed]) -> list[Step]:
    """S-007-4, S-007-5: one step per account, listing each of its repos to pick"""
    by_account: dict[str, list[str]] = {}
    for repo in missing:
        by_account.setdefault(repo.partition("/")[0], []).append(repo)
    return [
        Step(
            installed.account if (installed := found.get(account.lower())) else account,
            "add" if installed else "install",
            tuple(repos),
        )
        for account, repos in by_account.items()
    ]


@dataclass(frozen=True, slots=True)
class Guided:
    slug: str
    page: str
    steps: tuple[Step, ...]
    already: tuple[str, ...]
    installed: tuple[str, ...]  # covered by the end, the already ones included

    def record(self, app_id: int, repos: Sequence[str]) -> dict[str, object]:
        """S-007-8"""
        step_of = {r: s for s in self.steps for r in s.repos}
        rows = []
        for repo in repos:
            step = step_of.get(repo)
            rows.append(
                {
                    "repo": repo,
                    "account": repo.partition("/")[0],
                    "action": "none" if step is None else step.action,
                    "installed": repo in self.installed,
                }
            )
        return {"app_id": app_id, "slug": self.slug, "page": self.page, "repos": rows}


def guide(
    app_id: int,
    key: Path,
    repos: Sequence[str],
    api: Api,
    signer: Signer,
    say: Callable[[str], None],
    browse: Callable[[str], None],
    clock: Callable[[], dt.datetime] = lambda: dt.datetime.now(dt.UTC),
    sleep: Callable[[float], None] = time.sleep,
    seconds: float = WAIT_SECONDS,
) -> Guided:
    """S-007-3 to S-007-6: report what is covered; for the rest, open the App's Install App
    page once, say what each account needs, and wait until each is covered or the time runs
    out. Nothing is installed or added for the maintainer"""
    token = jwt(app_id, key, clock(), signer)
    slug, page = app_page(api, token)
    already = tuple(r for r in repos if covered(api, token, r))
    for repo in already:
        say(f"already installed on {repo}")
    missing = [r for r in repos if r not in already]
    if not missing:
        return Guided(slug, page, (), already, already)
    planned = steps(missing, installations(api, token))
    say(f"{slug} doesn't cover {', '.join(missing)} yet; GitHub needs your click. Open {page}")
    for step in planned:
        say(step.line())
    browse(page)
    start = clock()
    found: list[str] = []
    while True:
        token = jwt(app_id, key, clock(), signer)
        for repo in missing:
            if repo not in found and covered(api, token, repo):
                found.append(repo)
                say(f"installed on {repo}")
        if len(found) == len(missing) or (clock() - start).total_seconds() >= seconds:
            done = already + tuple(r for r in missing if r in found)
            return Guided(slug, page, tuple(planned), already, done)
        sleep(POLL_SECONDS)
