"""Write a starting policy and the calling workflow into a repository, from what it has:
its CHANGELOG style, its version file, and its default branch"""

import tomllib
from dataclasses import dataclass
from pathlib import Path

from shipmill import UVX
from shipmill.config import CONFIG_PATH
from shipmill.doctor import CALLER, OPERATE_CALLER
from shipmill.errors import ReleaseError
from shipmill.gitrepo import Git
from shipmill.policy import Style, VersionFiles

BOT_REPO = "shipmill/shipmill"
BOT_REF = "v0"


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


def policy_text(d: Detected) -> str:
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


def caller_text(d: Detected, ci: str) -> str:
    uses = f"{BOT_REPO}/.github/workflows"
    return f"""name: Release

# shipmill: https://github.com/{BOT_REPO}. The policy in
# .github/shipmill.toml decides what releases and when; this file only wires
# shipmill's workflows to this repository's CI.

on:
  push:
    branches: [{d.branch}]
  schedule:
    - cron: "7 * * * *" # hourly: the policy decides whether a lane's window is open
  workflow_dispatch:
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
      hotfix-from: ${{{{ inputs.hotfix-from || '' }}}}
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
      base: ${{{{ needs.prepare.outputs.base }}}}
    permissions:
      contents: write # main, release branches, tags, and releases
      actions: write # starts the lane's dispatch workflows
      issues: write # closes the issue release = "propose" opens, once the lane released
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


def init(root: Path, ci: str, force: bool) -> Initialized:
    """Write .github/shipmill.toml and the caller workflow. Either one already there refuses
    without force; with force, the files are overwritten."""
    d = detect(root)
    files = ((root / CONFIG_PATH, policy_text(d)), (root / CALLER, caller_text(d, ci)))
    present = [path for path, _ in files if path.exists()]
    if present and not force:
        names = ", ".join(str(p.relative_to(root)) for p in present)
        raise ReleaseError(f"{names} {'exists' if len(present) == 1 else 'exist'}; pass --force to overwrite")
    for path, text in files:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return Initialized(tuple(path for path, _ in files))


def init_operate(root: Path, force: bool) -> Path:
    """Write the operate caller, for a repository whose environments use from or health"""
    path = root / OPERATE_CALLER
    if path.exists() and not force:
        raise ReleaseError(f"{OPERATE_CALLER} exists; pass --force to overwrite")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(operate_caller_text(), encoding="utf-8")
    return path
