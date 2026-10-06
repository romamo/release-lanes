#!/usr/bin/env python3
"""Report what a repo still needs before the shipmill skills can triage, land, and
release it on their own, and with --fix make the parts that need no decision.

Usage: setup_state.py <owner/repo> [--repo-dir PATH] [--fix] [--json]

Releases (.github/shipmill.toml):
  RELEASE_MISSING  no config with release keys: shipmill-setup's steps 1 to 5
  RELEASE_FOREIGN  a config, but .github/workflows/release.yml doesn't call shipmill's
                   workflows
  RELEASE_OFF      mode = "off"
  RELEASE_DRY_RUN  mode = "dry-run": run the Release workflow by hand, then the user
                   sets mode = "release"
  RELEASE_READY    mode = "release"

Agents (the config's [agents] section, read by `shipmill gate`):
  AGENTS_MISSING   no [agents] section: the gate has no prompt. Fine when agents run
                   only on demand
  AGENTS_NO_APP    a prompt and mode = "headless" but no app_id: needs-decision
                   comments post as the host's gh login, so GitHub won't notify it;
                   the gate's desktop notifications only. Counted as done
  AGENTS_OK        a section with a prompt

Plugin (.claude/settings.json, so every session in the repo loads the skills):
  PLUGIN_MISSING   shipmill@shipmill is not enabled; --fix adds the marketplace and
                   enables it, keeping every other key
  PLUGIN_DISABLED  enabledPlugins sets it to false on purpose; --fix leaves it
  PLUGIN_OK        enabled

Labels (the triage skills and shipmill read them):
  LABELS_MISSING   some of postponed, blocked, shipmill-hold, the config's
                   blocker_label (default release-blocker), and, with [agents]
                   mode = "headless", needs-decision don't exist; --fix creates them
                   on GitHub
  LABELS_OK        all exist

Branches (the repo setting delete_branch_on_merge; github-pr-triage's stacked merges rely
on GitHub retargeting a stacked PR when the branch under it is deleted):
  BRANCH_DELETE_OFF  merged PR branches stay on GitHub; --fix turns the setting on
  BRANCH_DELETE_ON   GitHub deletes a PR's branch when it merges

Exit 0 when every row is RELEASE_READY, AGENTS_OK, AGENTS_NO_APP, PLUGIN_OK, LABELS_OK,
or BRANCH_DELETE_ON, 1 otherwise, 2 on bad input, a malformed settings.json, both config
files, or a git or gh failure (a failed read of the repo setting never reads as off).
Needs git and an authenticated gh. Python 3.10+, standard library only.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NoReturn

CONFIG = Path(".github/shipmill.toml")
CALLER = Path(".github/workflows/release.yml")
SETTINGS = Path(".claude/settings.json")
PLUGIN = "shipmill@shipmill"
MARKETPLACE = {"source": {"source": "github", "repo": "shipmill/shipmill"}, "autoUpdate": True}
# Where release.yml calls shipmill from: the repo, and its path before it moved to the org
SHIPMILL_REPOS = ("shipmill/shipmill/", "romamo/shipmill/")
LABELS = {
    "postponed": ("c5def5", "Triage decided not now; revisited after the next stable release"),
    "blocked": ("fbca04", "Waits on another issue, here or upstream"),
    "shipmill-hold": ("000000", "While open, no lane releases except a hotfix started by hand"),
}
BLOCKER = ("b60205", "Holds the release lanes the policy names until closed")
# Wanted only with [agents] mode = "headless" (spec 005): a session's question waits under it
NEEDS_DECISION = "needs-decision"
NEEDS_DECISION_LABEL = ("d876e3", "A shipmill session asked a question here; waits for a reply")
DONE = {"RELEASE_READY", "AGENTS_OK", "AGENTS_NO_APP", "PLUGIN_OK", "LABELS_OK", "BRANCH_DELETE_ON"}


@dataclass(frozen=True)
class Row:
    state: str
    detail: str

    def text(self) -> str:
        return f"{self.state:<17} {self.detail}"


def run(cmd: list[str], cwd: Path | None = None) -> str:
    proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        sys.stderr.write(f"error: {' '.join(cmd[:3])}...: {proc.stderr.strip()}\n")
        raise SystemExit(2)
    return proc.stdout


def fail(message: str) -> NoReturn:
    sys.stderr.write(f"error: {message}\n")
    raise SystemExit(2)


# -- config --------------------------------------------------------------------------------


def config_file(repo_dir: Path) -> Path | None:
    path = repo_dir / CONFIG
    return path if path.is_file() else None


def policy_value(text: str, key: str) -> str | None:
    found = re.search(rf'^{key}\s*=\s*"([^"]*)"', text, re.MULTILINE)
    return found.group(1) if found else None


def top_level(text: str) -> str:
    """The config's keys before its first table: the release keys, never [agents]'s mode"""
    return re.split(r"^\[", text, maxsplit=1, flags=re.MULTILINE)[0]


def agents_section(repo_dir: Path) -> str | None:
    """The [agents] section's body, or None without one"""
    config = config_file(repo_dir)
    text = config.read_text(encoding="utf-8") if config else ""
    section = re.search(r"^\[agents\][^\n]*\n?(.*?)(?=^\[|\Z)", text, re.MULTILINE | re.DOTALL)
    return section.group(1) if section else None


def is_headless(repo_dir: Path) -> bool:
    """[agents] mode = "headless" (spec 005); `shipmill gate` validates the value"""
    section = agents_section(repo_dir)
    return section is not None and policy_value(section, "mode") == "headless"


def calls_shipmill(repo_dir: Path) -> bool:
    """release.yml calls shipmill's prepare workflow, from shipmill/shipmill (or its old
    path, romamo/shipmill) or, in shipmill itself and its forks, from a local copy"""
    caller = repo_dir / CALLER
    if not caller.is_file():
        return False
    text = caller.read_text(encoding="utf-8")
    local = repo_dir / ".github" / "workflows" / "prepare.yml"
    return any(path in text for path in SHIPMILL_REPOS) or (
        "./.github/workflows/prepare.yml" in text
        and local.is_file()
        and local.read_text(encoding="utf-8").startswith("name: shipmill prepare")
    )


def release_row(repo_dir: Path) -> Row:
    config = config_file(repo_dir)
    mode = policy_value(top_level(config.read_text(encoding="utf-8")), "mode") if config else None
    if config is None or mode is None:
        return Row("RELEASE_MISSING", f"no release keys in {CONFIG}: shipmill-setup steps 1 to 5")
    if not calls_shipmill(repo_dir):
        return Row("RELEASE_FOREIGN", f"{CALLER} doesn't call shipmill's workflows")
    if mode == "release":
        return Row("RELEASE_READY", "mode = release")
    if mode == "dry-run":
        return Row("RELEASE_DRY_RUN", "run the Release workflow by hand, then set mode = release")
    if mode == "off":
        return Row("RELEASE_OFF", "mode = off: shipmill never releases")
    fail(f"{config}: mode must be off, dry-run, or release, got {mode!r}")


def agents_row(repo_dir: Path) -> Row:
    """Presence only; `shipmill gate` and the release config loader validate the section.
    Headless without app_id is done too: it works, but GitHub won't notify the maintainer of
    a question posted as their own login, so only the gate's desktop notification does"""
    section = agents_section(repo_dir)
    if section is None or not re.search(r"^prompt\s*=", section, re.MULTILINE):
        return Row("AGENTS_MISSING", f"no [agents] prompt in {CONFIG}: needed only for `shipmill gate`")
    if is_headless(repo_dir) and not re.search(r"^app_id\s*=", section, re.MULTILINE):
        return Row(
            "AGENTS_NO_APP",
            "headless without app_id: needs-decision comments post as you, so GitHub won't notify you;"
            " desktop notifications only",
        )
    return Row("AGENTS_OK", "[agents] has a prompt")


# -- plugin --------------------------------------------------------------------------------


def load_settings(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        fail(f"{path}: {exc}")
    if not isinstance(data, dict):
        fail(f"{path}: the top level must be a JSON object")
    for key in ("extraKnownMarketplaces", "enabledPlugins"):
        if not isinstance(data.get(key, {}), dict):
            fail(f"{path}: {key} must be a JSON object")
    return data


def plugin_row(settings: dict[str, Any]) -> Row:
    enabled = settings.get("enabledPlugins", {}).get(PLUGIN)
    if enabled is True:
        return Row("PLUGIN_OK", f"{SETTINGS} enables {PLUGIN}")
    if enabled is False:
        return Row("PLUGIN_DISABLED", f"{SETTINGS} disables {PLUGIN} on purpose")
    return Row("PLUGIN_MISSING", f"{SETTINGS} doesn't enable {PLUGIN}")


def enable_plugin(settings: dict[str, Any]) -> dict[str, Any]:
    """The settings with the shipmill marketplace known and the plugin on. An existing
    marketplace entry (a fork, a local path) is kept"""
    merged = dict(settings)
    merged["extraKnownMarketplaces"] = {"shipmill": MARKETPLACE, **settings.get("extraKnownMarketplaces", {})}
    merged["enabledPlugins"] = {**settings.get("enabledPlugins", {}), PLUGIN: True}
    return merged


# -- labels --------------------------------------------------------------------------------


def wanted_labels(repo_dir: Path) -> dict[str, tuple[str, str]]:
    wanted = dict(LABELS)
    config = config_file(repo_dir)
    blocker = policy_value(config.read_text(encoding="utf-8"), "blocker_label") if config else None
    wanted[blocker or "release-blocker"] = BLOCKER
    if is_headless(repo_dir):
        wanted[NEEDS_DECISION] = NEEDS_DECISION_LABEL
    return wanted


def labels_row(missing: list[str], wanted: dict[str, tuple[str, str]]) -> Row:
    if missing:
        return Row("LABELS_MISSING", ", ".join(missing))
    extra = f" {NEEDS_DECISION}," if NEEDS_DECISION in wanted else ""
    return Row("LABELS_OK", f"postponed, blocked, shipmill-hold,{extra} and the blocker label exist")


def create_labels(
    repo: str, wanted: dict[str, tuple[str, str]], missing: list[str], gh: Callable[[list[str]], str] = run
) -> None:
    """--fix: create each missing label with its color and description"""
    for name in missing:
        color, description = wanted[name]
        gh(["gh", "label", "create", name, "-R", repo, "--color", color, "--description", description])


def existing_labels(repo: str) -> set[str]:
    out = run(["gh", "label", "list", "-R", repo, "--limit", "500", "--json", "name"])
    return {label["name"] for label in json.loads(out)}


# -- branches ------------------------------------------------------------------------------


def read_flag(out: str, what: str) -> bool:
    """gh's -q output for a boolean: anything but true or false fails, never reads as off"""
    value = out.strip()
    if value not in ("true", "false"):
        fail(f"{what}: expected true or false, got {value!r}")
    return value == "true"


def branch_delete_row(repo: str, fix: bool, gh: Callable[[list[str]], str] = run) -> Row:
    """Whether GitHub deletes a PR's branch on merge; with fix, turns it on when it's off"""
    query = ["-q", ".delete_branch_on_merge"]
    on = read_flag(gh(["gh", "api", f"repos/{repo}", *query]), f"{repo}'s delete_branch_on_merge")
    if not on and fix:
        patch = ["gh", "api", "-X", "PATCH", f"repos/{repo}", "-F", "delete_branch_on_merge=true", *query]
        on = read_flag(gh(patch), f"{repo}'s delete_branch_on_merge after --fix")
        if not on:
            fail(f"{repo}: turning delete_branch_on_merge on didn't take")
    if on:
        return Row("BRANCH_DELETE_ON", "GitHub deletes a pull request's branch when it merges")
    return Row("BRANCH_DELETE_OFF", "merged branches stay on GitHub and stacked PRs aren't retargeted")


# -- main ----------------------------------------------------------------------------------


def check_checkout(repo: str, repo_dir: Path) -> None:
    owner, _, name = repo.partition("/")
    if not owner or not name or "/" in name:
        fail(f"repo must be owner/name, got {repo!r}")
    origin = run(["git", "remote", "get-url", "origin"], cwd=repo_dir).strip()
    if not re.search(rf"[:/]{re.escape(repo)}(\.git)?/?$", origin, re.IGNORECASE):
        fail(f"{repo_dir}'s origin is {origin}, not {repo}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("repo", help="owner/name")
    parser.add_argument("--repo-dir", type=Path, default=Path.cwd(), help="the repo's checkout (default: here)")
    parser.add_argument(
        "--fix",
        action="store_true",
        help="enable the plugin, create the missing labels, and turn on deleting branches on merge",
    )
    parser.add_argument("--json", action="store_true", help="JSON lines instead of a table")
    args = parser.parse_args()
    repo_dir: Path = args.repo_dir.resolve()
    check_checkout(args.repo, repo_dir)

    settings_path = repo_dir / SETTINGS
    settings = load_settings(settings_path)
    if args.fix and plugin_row(settings).state == "PLUGIN_MISSING":
        settings = enable_plugin(settings)
        settings_path.parent.mkdir(exist_ok=True)
        settings_path.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")

    wanted = wanted_labels(repo_dir)
    have = existing_labels(args.repo)
    missing = [name for name in wanted if name not in have]
    if args.fix:
        create_labels(args.repo, wanted, missing)
        missing = []

    rows = [
        release_row(repo_dir),
        agents_row(repo_dir),
        plugin_row(settings),
        labels_row(missing, wanted),
        branch_delete_row(args.repo, args.fix),
    ]
    for row in rows:
        print(json.dumps({"state": row.state, "detail": row.detail}) if args.json else row.text())
    return 0 if all(row.state in DONE for row in rows) else 1


if __name__ == "__main__":
    sys.exit(main())
