"""Write a starting policy and the calling workflow into a repository, from what it has:
its CHANGELOG style, its version file, and its default branch; and, unless told not to, the
changelog fragments folder the policy names (spec 013)"""

import datetime as dt
import json
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path

from shipmill import UVX
from shipmill.config import CONFIG_PATH, config_path, loads
from shipmill.doctor import CALLER, OPERATE_CALLER
from shipmill.errors import ReleaseError
from shipmill.gitrepo import Git
from shipmill.policy import Policy, Style, VersionFiles
from shipmill.ticks import HOURLY_TICKS, Ticks, needed
from shipmill.upgrades import FRAGMENTS, fragments_readme_text

BOT_REPO = "shipmill/shipmill"
BOT_REF = "v0"


@dataclass(frozen=True, slots=True)
class RunsOn:
    """The runner the release workflows' jobs run on (spec 015): one label, or a list of
    labels that a runner must all carry, as prepare.yml's and land.yml's `runs-on` input takes"""

    labels: tuple[str, ...]
    listed: bool

    # a bare YAML scalar can't start with an indicator; a runner label never does
    _INDICATOR = re.compile(r"[-?:,\[\]{}#&*!|>'\"%@`]")
    # a label YAML reads as the string it is when bare; any other (true, null, ~, 123, foo:)
    # would reach the `string` input as a bool, a null, a number, or not parse at all
    _PLAIN = re.compile(r"[A-Za-z][A-Za-z0-9_./-]*")
    _NOT_STRINGS = frozenset({"true", "false", "null", "yes", "no", "on", "off", "y", "n"})

    @classmethod
    def parse(cls, text: str) -> RunsOn:
        """A label, or a JSON list of labels when it starts with `[`"""
        if text.startswith("["):
            try:
                value = json.loads(text)
            except json.JSONDecodeError as exc:
                raise ReleaseError(f"--runs-on {text!r} starts with '[' but isn't JSON: {exc.msg}") from exc
            if not (isinstance(value, list) and value and all(isinstance(v, str) and v for v in value)):
                raise ReleaseError(f"--runs-on {text!r} is not a non-empty JSON list of non-empty strings")
            return cls(tuple(value), listed=True)
        if not text:
            raise ReleaseError("--runs-on '' is empty; name a runner label, or a JSON list of labels")
        if re.search(r"[\s'\",]", text):
            raise ReleaseError(
                f"--runs-on {text!r} is not a runner label: it holds whitespace, a quote, or a comma;"
                ' for several labels pass a JSON list, such as \'["self-hosted", "linux"]\''
            )
        if cls._INDICATOR.match(text):
            raise ReleaseError(f"--runs-on {text!r} is not a runner label: it starts with {text[0]!r}")
        return cls((text,), listed=False)

    def yaml(self) -> str:
        """The input's value in release.yml: a label bare, a list as compact JSON in single quotes,
        since an unquoted list would reach the workflow as a YAML sequence, not a string; a label
        YAML would read as something else is single-quoted too"""
        if not self.listed:
            label = self.labels[0]
            if self._PLAIN.fullmatch(label) and label.lower() not in self._NOT_STRINGS:
                return label
            return "'" + label.replace("'", "''") + "'"
        return "'" + json.dumps(list(self.labels), separators=(",", ":")).replace("'", "''") + "'"


@dataclass(frozen=True, slots=True)
class Detected:
    name: str
    branch: str
    style: Style
    version_files: VersionFiles


def detect(root: Path) -> Detected:
    name = root.resolve().name
    version_files = VersionFiles.NONE
    pyproject = root / "pyproject.toml"
    git = Git(root)
    if pyproject.is_file():
        project = tomllib.loads(pyproject.read_text(encoding="utf-8")).get("project", {})
        name = str(project.get("name", name))
        # a version the tags never carried (a 0.0.0 placeholder) is not where releases live
        tagged = {str(t.version) for t in git.tags()}
        if "version" in project and (not tagged or str(project["version"]) in tagged):
            version_files = VersionFiles.PYPROJECT
    changelog = root / "CHANGELOG.md"
    if not changelog.is_file():
        raise ReleaseError("no CHANGELOG.md, whose Unreleased section shipmill releases; add one first")
    text = changelog.read_text(encoding="utf-8")
    if "## [Unreleased]" in text:
        style = Style.KEEP_A_CHANGELOG
    elif "## Unreleased" in text:
        style = Style.DASH
    else:
        raise ReleaseError("CHANGELOG.md has no '## [Unreleased]' or '## Unreleased' heading")
    if git.ok("symbolic-ref", "--quiet", "refs/remotes/origin/HEAD"):
        branch = git.run("symbolic-ref", "--short", "refs/remotes/origin/HEAD").strip().removeprefix("origin/")
    elif git.ok("symbolic-ref", "--quiet", "HEAD"):
        branch = git.run("symbolic-ref", "--short", "HEAD").strip()
    else:
        branch = "main"
    return Detected(name, branch, style, version_files)


def policy_text(d: Detected, fragments: bool = True) -> str:
    """The starting policy; fragments: it sets [changelog] fragments (spec 013)"""
    folder = (
        "# Each pull request adds its entry as a file in this folder instead of editing the CHANGELOG;\n"
        f'# a stable release writes them into its section and deletes them\nfragments = "{FRAGMENTS}"\n'
        if fragments
        else ""
    )
    if d.style is Style.KEEP_A_CHANGELOG:
        bump = (
            '[bump]\nfrom = "headings"\nmajor = ["Breaking"]\n'
            'minor = ["Added", "Changed", "Deprecated", "Removed"]\npatch = ["Fixed", "Security"]\n'
        )
    else:
        bump = (
            "# A change under these paths since the last stable tag makes a minor release; anything\n"
            "# else a patch. fnmatch patterns, where * also matches /\n"
            '[bump]\nfrom = "paths"\nminor_paths = ["src/*"]\n'
        )
    after = ""
    if d.version_files is VersionFiles.PYPROJECT:
        after = (
            "\n# Commands run after the version is written, before the commit; a failure stops the release\n"
            'after_stamp = ["uv lock --check"]\n'
        )
    return f'''# shipmill reads this file on every run: https://github.com/{BOT_REPO}
name = "{d.name}"

# off: do nothing; dry-run: show the release commit it would make; release: release
mode = "dry-run"
branch = "{d.branch}"

# pyproject: [project] version in pyproject.toml and uv.lock; none: tags and CHANGELOG only
version_files = "{d.version_files}"
{after}
[changelog]
path = "CHANGELOG.md"
style = "{d.style}"
{folder}
{bump}
[gates]
# An open issue with this label holds the listed lanes
blocker_label = "release-blocker"
blocker_lanes = ["rc", "stable"]
# Inclusive UTC date ranges when the listed lanes hold, such as "2026-12-24..2027-01-02"
freeze = []
freeze_lanes = ["dev", "rc", "stable"]

# Each lane releases when one of its triggers is due and no gate holds it; every lane can
# also be started by hand. One run releases one lane, in the order hotfix, stable, rc, dev.

# [lanes.dev]                       # X.Y.Z.devN after every batch of merges
# quiet_minutes = 30
# dispatch = ["publish.yml"]

[lanes.rc]                          # X.Y.ZrcN from main, each workday morning
schedule = ["Mon-Fri 07:00 UTC"]
github_release = true

[lanes.stable]                      # X.Y.Z, promoted from an rc nobody reported a blocker on
promote_from = "rc"
min_soak_days = 3
schedule = ["Mon 07:00 UTC"]
milestone = true
github_release = true

[lanes.hotfix]                      # X.Y.(Z+1) from release/X.Y with chosen PRs, by hand only
github_release = true

# Environments a release deploys to. The workflow runs on workflow_dispatch with `tag` and
# `environment` inputs, and its job sets `environment: ${{{{ inputs.environment }}}}`, so
# GitHub records a deployment for each run. With from or health, run
# `{UVX} init --operate`:
# its workflow checks health every 10 minutes and promotes after the bake.
# [environments.staging]
# lane = "rc"                       # deploy every release of this lane when it lands
# workflow = "deploy.yml"
# health = "https://staging.example.com/health"
#
# [environments.production]
# from = "staging"                  # promoted from staging once it baked there
# workflow = "deploy.yml"
# health = "https://example.com/health"
# bake_minutes = 60                 # how long staging stays healthy before promotion

# Lines a release rewrites besides the version files; each pattern matches exactly once.
# The replacement can use {{name}}, {{version}}, {{semver}}, {{minor}} (X.Y), and {{date}}.
# [[version_lines]]
# file = "README.md"
# pattern = '{d.name} v\\d+\\.\\d+\\.\\d+'
# replace = "{d.name} v{{version}}"

# How far shipmill goes by itself: observe (report only), propose (open an issue saying what
# it would release; a person starts the lane), or act (the default). An open issue labelled
# shipmill-hold turns every act into propose until it is closed.
# [autonomy]
# release = "act"

# What the gate starts a Claude Code session for; shipmill-setup writes this section with you.
# [agents]
# prompt = "/shipmill:github-issue-triage {{repo}} triage the new issues; do not merge"
# plugin_update = false             # the default: the gate changes no plugin install; true: it
#                                   # updates its checkout's shipmill plugin once a day
'''


def caller_text(d: Detected, ci: str, runs_on: RunsOn | None = None, ticks: Ticks = HOURLY_TICKS) -> str:
    """The Release workflow; runs_on: the runner prepare.yml's and land.yml's jobs run on,
    which without it stay on their default, ubuntu-latest; ticks: the schedule the policy
    needs, the hourly tick by default (spec 015)"""
    uses = f"{BOT_REPO}/.github/workflows"
    runner = "" if runs_on is None else f"\n      runs-on: {runs_on.yaml()}"
    return f"""name: Release

# shipmill: https://github.com/{BOT_REPO}. The policy in
# .github/shipmill.toml decides what releases and when; this file only wires
# shipmill's workflows to this repository's CI.

on:
  push:
    branches: [{d.branch}]
{ticks.yaml()}  workflow_dispatch:
    inputs:
      lane:
        description: "policy: follow the policy; a lane: release it now, skipping triggers but not gates"
        type: choice
        options: [policy, dev, rc, stable, hotfix]
        default: policy
      dry-run:
        description: "Show the release commit, push nothing"
        type: boolean
        default: true
      hotfix-prs:
        description: "hotfix: merged pull requests to ship, such as 12,15"
        type: string
        default: ""
      hotfix-from:
        description: "hotfix: the stable tag to fix; empty takes the latest"
        type: string
        default: ""

permissions:
  contents: read

jobs:
  prepare:
    uses: {uses}/prepare.yml@{BOT_REF}
    with:
      lane: ${{{{ inputs.lane != 'policy' && inputs.lane || '' }}}}
      dry-run: ${{{{ inputs.dry-run || false }}}}
      hotfix-prs: ${{{{ inputs.hotfix-prs || '' }}}}
      hotfix-from: ${{{{ inputs.hotfix-from || '' }}}}{runner}
    permissions:
      contents: write # pushes the release commit's work branch
      issues: write # blockers and milestones; opens the issue a hold or release = "propose" leads to
      pull-requests: read # a hotfix's merge commits
      actions: read # replaces a work branch a cancelled run left, once no other run owns it

  ci:
    needs: prepare
    if: needs.prepare.outputs.sha != ''
    uses: ./.github/workflows/{ci}
    with:
      ref: ${{{{ needs.prepare.outputs.sha }}}}

  land:
    needs: [prepare, ci]
    if: always() && needs.prepare.outputs.sha != ''
    uses: {uses}/land.yml@{BOT_REF}
    with:
      ci-result: ${{{{ needs.ci.result }}}}
      lane: ${{{{ needs.prepare.outputs.lane }}}}
      version: ${{{{ needs.prepare.outputs.version }}}}
      sha: ${{{{ needs.prepare.outputs.sha }}}}
      base: ${{{{ needs.prepare.outputs.base }}}}{runner}
    permissions:
      contents: write # main, release branches, tags, and releases
      actions: write # starts the lane's dispatch workflows
      issues: write # closes the issue release = "propose" opens, and proposes config upgrades
"""


def operate_caller_text() -> str:
    uses = f"{BOT_REPO}/.github/workflows"
    return f"""name: Operate

# shipmill: https://github.com/{BOT_REPO}. Every 10 minutes shipmill operate checks the
# health of each environment in .github/shipmill.toml, records it as GitHub deployment
# statuses, promotes a release once its source environment baked it, and rolls back an
# environment that fails its checks, opening an incident issue. Approve a deploy that waits
# on a proposal issue with: gh workflow run operate.yml -f approve=<environment> -f
# dry-run=false; a proposed rollback with -f approve-rollback=<environment> instead

on:
  schedule:
    - cron: "*/10 * * * *"
  workflow_dispatch:
    inputs:
      approve:
        description: "Deploy the tag proposed for this environment, once"
        type: string
        default: ""
      approve-rollback:
        description: "Start the rollback an incident proposes for this environment, once"
        type: string
        default: ""
      dry-run:
        description: "Show what it would do, change nothing"
        type: boolean
        default: true

permissions:
  contents: read

jobs:
  operate:
    uses: {uses}/operate.yml@{BOT_REF}
    with:
      approve: ${{{{ inputs.approve || '' }}}}
      approve-rollback: ${{{{ inputs.approve-rollback || '' }}}}
      dry-run: ${{{{ inputs.dry-run || false }}}}
    permissions:
      contents: read # the config and the release tags
      deployments: write # reads deployments and records health as their statuses
      actions: write # starts the deploy workflows
      issues: write # the hold, the issue a deploy under propose or the hold opens, and incidents
"""


@dataclass(frozen=True, slots=True)
class Initialized:
    written: tuple[Path, ...]


def init(
    root: Path,
    ci: str,
    force: bool,
    fragments: bool = True,
    runs_on: RunsOn | None = None,
    today: dt.date | None = None,
) -> Initialized:
    """Write .github/shipmill.toml and the caller workflow, and with fragments the fragments
    folder's README.md (spec 013); runs_on: the caller passes it as the release workflows'
    runner, and its schedule is the one the written policy needs from today, by default the
    UTC date (spec 015). The policy or the caller already there refuses without force; with
    force, they are overwritten. A README.md already in the folder is kept."""
    d = detect(root)
    policy = policy_text(d, fragments)
    ticks = needed(Policy.parse(loads(policy, str(CONFIG_PATH)), CONFIG_PATH.name), today or _today())
    files = [(root / CONFIG_PATH, policy), (root / CALLER, caller_text(d, ci, runs_on, ticks))]
    present = [path for path, _ in files if path.exists()]
    if present and not force:
        names = ", ".join(str(p.relative_to(root)) for p in present)
        raise ReleaseError(f"{names} {'exists' if len(present) == 1 else 'exist'}; pass --force to overwrite")
    readme = root / FRAGMENTS / "README.md"
    if fragments and not readme.exists():
        files.append((readme, fragments_readme_text(d.style)))
    for path, text in files:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return Initialized(tuple(path for path, _ in files))


def init_caller(root: Path, ci: str, force: bool, runs_on: RunsOn | None = None, today: dt.date | None = None) -> Path:
    """Write only the caller workflow, from the policy already in .github/shipmill.toml, so
    its schedule follows an edited policy (spec 015); a policy that isn't there or doesn't
    load refuses, and so does a caller already there without force"""
    policy = Policy.load(config_path(root))
    path = root / CALLER
    if path.exists() and not force:
        raise ReleaseError(f"{CALLER} exists; pass --force to overwrite")
    d = Detected(policy.name, policy.branch, policy.style, policy.version_files)
    text = caller_text(d, ci, runs_on, needed(policy, today or _today()))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _today() -> dt.date:
    return dt.datetime.now(dt.UTC).date()


def init_operate(root: Path, force: bool) -> Path:
    """Write the operate caller, for a repository whose environments use from or health"""
    path = root / OPERATE_CALLER
    if path.exists() and not force:
        raise ReleaseError(f"{OPERATE_CALLER} exists; pass --force to overwrite")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(operate_caller_text(), encoding="utf-8")
    return path
