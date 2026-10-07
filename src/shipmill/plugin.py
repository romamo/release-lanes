"""The gate's daily check of its own checkout's shipmill@shipmill install (D-22).

Claude Code keys a project-scope plugin install on the folder it runs in, so the gate's
checkout (`tmp/shipmill-gate`) has an install of its own, apart from the repo's. With
[agents] plugin_update = true, a tick that is about to start a session checks at most once
per 24 hours whether that install is behind shipmill's latest release and, when it is,
updates it with `claude plugin update --scope project` run in the checkout. The last check
is recorded under the repo's git directory, beside the gate's other state. A failed check
or update is reported on the tick, and the session still starts.
"""

import datetime as dt
import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from shipmill.errors import ReleaseError
from shipmill.version import Version

PLUGIN = "shipmill@shipmill"  # the Claude Code plugin, in its marketplace
SHIPMILL_REPO = "shipmill/shipmill"  # where shipmill releases
CHECKED = "plugin-check.json"  # the last check's time, under the gate's state directory
EVERY = dt.timedelta(hours=24)

Runner = Callable[[list[str], Path], str]  # (command, cwd) -> its stdout; ReleaseError when it fails


@dataclass(frozen=True, slots=True)
class PluginUpdate:
    """What a tick's check found and did; installed is None without a project install in the
    checkout, latest None when the check failed before it read the release"""

    installed: str | None
    latest: str | None
    updated: bool
    error: str | None = None

    def line(self) -> str | None:
        """The tick's line: an update or a failure; a current install says nothing"""
        if self.error is not None:
            return f"plugin update failed, the session starts anyway: {self.error}"
        if self.updated:
            return f"plugin updated: {PLUGIN} {self.installed} -> {self.latest} in this checkout (project scope)"
        return None

    def record(self) -> dict[str, object]:
        return {"installed": self.installed, "latest": self.latest, "updated": self.updated, "error": self.error}


def load_checked(path: Path) -> dt.datetime | None:
    """The last check's time; None before the first. A malformed file fails, never reads as due"""
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ReleaseError(f"{path} is malformed: {exc}") from None
    if not isinstance(data, dict) or set(data) != {"checked"} or not isinstance(data["checked"], str):
        raise ReleaseError(f'{path} is malformed: expected {{"checked": <ISO time>}}')
    try:
        checked = dt.datetime.fromisoformat(data["checked"])
    except ValueError:
        raise ReleaseError(f"{path} is malformed: {data['checked']!r} is not an ISO time") from None
    if checked.tzinfo is None:
        raise ReleaseError(f"{path} is malformed: {data['checked']!r} has no time zone")
    return checked


def save_checked(path: Path, now: dt.datetime) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"checked": now.isoformat()}) + "\n", encoding="utf-8")


def project_install(text: str, checkout: Path) -> str | None:
    """The version of the checkout's own project-scope install, from `claude plugin list
    --json`; None without one. Anything but the documented shape is refused"""
    try:
        installs = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ReleaseError(f"claude plugin list --json printed no JSON: {exc}") from None
    if not isinstance(installs, list):
        raise ReleaseError("claude plugin list --json: expected a JSON array")
    found = []
    here = checkout.resolve()  # the gate's --repo may be relative; Claude Code records an absolute path
    for e in installs:
        if not isinstance(e, dict):
            raise ReleaseError(f"claude plugin list --json: an install is {e!r}, not an object")
        if e.get("id") != PLUGIN:
            continue
        if not isinstance(e.get("scope"), str) or not isinstance(e.get("version"), str):
            raise ReleaseError(f"claude plugin list --json: {PLUGIN} install {e!r}")
        where = e.get("projectPath")
        if e["scope"] == "project" and isinstance(where, str) and Path(where).resolve() == here:
            found.append(e["version"])
    if len(found) > 1:
        raise ReleaseError(f"claude plugin list --json lists {PLUGIN} twice at project scope in {checkout}")
    return found[0] if found else None


def check(checkout: Path, run: Runner) -> PluginUpdate:
    """Read the checkout's install and the latest release; update the install when it's behind"""
    installed = project_install(run(["claude", "plugin", "list", "--json"], checkout), checkout)
    if installed is None:
        return PluginUpdate(None, None, False)
    tag = run(["gh", "release", "view", "-R", SHIPMILL_REPO, "--json", "tagName", "-q", ".tagName"], checkout).strip()
    latest = Version.of_tag(tag)
    if Version.parse(installed) >= latest:
        return PluginUpdate(installed, str(latest), False)
    try:
        run(["claude", "plugin", "update", PLUGIN, "--scope", "project"], checkout)
    except ReleaseError as exc:
        return PluginUpdate(installed, str(latest), False, str(exc))
    return PluginUpdate(installed, str(latest), True)


def daily_update(path: Path, checkout: Path, now: dt.datetime, run: Runner) -> PluginUpdate | None:
    """check(), at most once per EVERY: None when the last check was more recent. A check
    that read the versions is recorded, a failed update included; one that failed to read
    them is reported and not recorded, so the next launch tries again"""
    last = load_checked(path)
    if last is not None and now - last < EVERY:
        return None
    try:
        found = check(checkout, run)
    except ReleaseError as exc:
        return PluginUpdate(None, None, False, str(exc))
    save_checked(path, now)
    return found
