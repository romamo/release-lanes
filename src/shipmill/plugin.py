"""The gate's daily check of its own checkout's shipmill@shipmill installs (D-23).

Claude Code keys a project- or local-scope plugin install on a folder, so the gate's checkout
(`tmp/shipmill-gate`) can have installs of its own, apart from the repo's. With [agents]
plugin_update = true, a tick that is about to start a session checks at most once per 24
hours whether any install keyed on the checkout, at project or local scope, is behind
shipmill's latest release and, when one is, updates it with `claude plugin update --scope
<its scope>` run in the checkout. Claude Code documents no way to name the project an update
acts on, so the check reads the installs again afterwards: an install still behind, or an
install keyed on another folder that the update changed, is reported as a failure. The last
check is recorded under the repo's git directory, beside the gate's other state. A failed
check or update is reported on the tick, and the session still starts. A user-scope install
is never updated.
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
SCOPES = ("project", "local")  # the scopes Claude Code keys on a folder, in the order they're updated

Runner = Callable[[list[str], Path], str]  # (command, cwd) -> its stdout; ReleaseError when it fails


@dataclass(frozen=True, slots=True)
class Install:
    """One install of the plugin keyed on a folder: its scope, folder, and version"""

    scope: str
    folder: Path
    version: str


@dataclass(frozen=True, slots=True)
class PluginUpdate:
    """What a tick's check found and did: the checkout's installs as it found them (none
    without one), the latest release (None when the check failed before it read it), the
    scopes it updated and confirmed, and the failure, if any"""

    installs: tuple[Install, ...]
    latest: str | None
    updated: tuple[str, ...]
    error: str | None = None

    def lines(self) -> list[str]:
        """The tick's lines: each update, then a failure; current installs say nothing"""
        lines = [
            f"plugin updated: {PLUGIN} {i.version} -> {self.latest} in this checkout ({i.scope} scope)"
            for i in self.installs
            if i.scope in self.updated
        ]
        if self.error is not None:
            lines.append(f"plugin update failed, the session starts anyway: {self.error}")
        return lines

    def record(self) -> dict[str, object]:
        installs = [
            {"scope": i.scope, "installed": i.version, "updated": i.scope in self.updated} for i in self.installs
        ]
        return {"latest": self.latest, "installs": installs, "error": self.error}


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


def keyed_installs(text: str) -> list[Install]:
    """The plugin's project- and local-scope installs, from `claude plugin list --json`, each
    with its folder resolved; a user-scope install is left out. Anything but the documented
    shape is refused"""
    try:
        entries = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ReleaseError(f"claude plugin list --json printed no JSON: {exc}") from None
    if not isinstance(entries, list):
        raise ReleaseError("claude plugin list --json: expected a JSON array")
    found = []
    for e in entries:
        if not isinstance(e, dict):
            raise ReleaseError(f"claude plugin list --json: an install is {e!r}, not an object")
        if e.get("id") != PLUGIN:
            continue
        if not isinstance(e.get("scope"), str) or not isinstance(e.get("version"), str):
            raise ReleaseError(f"claude plugin list --json: {PLUGIN} install {e!r}")
        if e["scope"] not in SCOPES:
            continue
        where = e.get("projectPath")
        if not isinstance(where, str):
            raise ReleaseError(f"claude plugin list --json: {PLUGIN} {e['scope']} install has no projectPath: {e!r}")
        found.append(Install(e["scope"], Path(where).resolve(), e["version"]))
    return found


def checkout_installs(installs: list[Install], checkout: Path) -> tuple[Install, ...]:
    """The installs keyed on the checkout, project scope first; two at one scope are refused"""
    here = checkout.resolve()  # the gate's --repo may be relative; Claude Code records an absolute path
    own = [i for i in installs if i.folder == here]
    for scope in SCOPES:
        if sum(i.scope == scope for i in own) > 1:
            raise ReleaseError(f"claude plugin list --json lists {PLUGIN} twice at {scope} scope in {checkout}")
    return tuple(sorted(own, key=lambda i: SCOPES.index(i.scope)))


def check(checkout: Path, run: Runner) -> PluginUpdate:
    """Read the checkout's installs and the latest release; update each install that's behind,
    then read the installs again to confirm the update reached it and no other folder's"""
    listing = ["claude", "plugin", "list", "--json"]
    before = keyed_installs(run(listing, checkout))
    own = checkout_installs(before, checkout)
    if not own:
        return PluginUpdate((), None, ())
    tag = run(["gh", "release", "view", "-R", SHIPMILL_REPO, "--json", "tagName", "-q", ".tagName"], checkout).strip()
    latest = Version.of_tag(tag)
    behind = [i for i in own if Version.parse(i.version) < latest]
    if not behind:
        return PluginUpdate(own, str(latest), ())
    errors = []
    tried = []
    for install in behind:
        try:
            run(["claude", "plugin", "update", PLUGIN, "--scope", install.scope], checkout)
        except ReleaseError as exc:
            errors.append(str(exc))
            continue
        tried.append(install.scope)
    try:
        after = keyed_installs(run(listing, checkout))
    except ReleaseError as exc:
        errors.append(f"the update couldn't be confirmed: {exc}")
        return PluginUpdate(own, str(latest), (), "; ".join(errors))
    now = {i.scope: i.version for i in checkout_installs(after, checkout)}
    updated = []
    for scope in tried:
        version = now.get(scope)
        if version is not None and Version.parse(version) >= latest:
            updated.append(scope)
        else:
            left = "removed its install" if version is None else f"left its install at {version}"
            errors.append(f"`claude plugin update --scope {scope}` in {checkout} {left}")
    errors += [f"the update changed the {i.scope} install in {i.folder}" for i in changed(before, after, checkout)]
    return PluginUpdate(own, str(latest), tuple(updated), "; ".join(errors) or None)


def changed(before: list[Install], after: list[Install], checkout: Path) -> list[Install]:
    """The installs keyed on folders other than the checkout that the update changed or removed"""
    here = checkout.resolve()
    now = {(i.scope, i.folder): i.version for i in after}
    return [i for i in before if i.folder != here and now.get((i.scope, i.folder)) != i.version]


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
        return PluginUpdate((), None, (), str(exc))
    save_checked(path, now)
    return found
