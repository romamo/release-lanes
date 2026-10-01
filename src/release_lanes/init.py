"""Write a starting policy and the calling workflow into a repository, from what it has:
its CHANGELOG style, its version file, and its default branch"""

import tomllib
from dataclasses import dataclass
from pathlib import Path

from release_lanes.doctor import CALLER
from release_lanes.errors import ReleaseError
from release_lanes.gitrepo import Git
from release_lanes.policy import POLICY_PATH, Style, VersionFiles

BOT_REPO = "romamo/release-lanes"
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
        raise ReleaseError("no CHANGELOG.md: the bot releases what its Unreleased section lists; add one first")
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
    return f'''# release-lanes reads this file on every run: https://github.com/{BOT_REPO}
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

# Lines a release rewrites besides the version files; each pattern matches exactly once.
# The replacement can use {{name}}, {{version}}, {{semver}}, {{minor}} (X.Y), and {{date}}.
# [[version_lines]]
# file = "README.md"
# pattern = '{d.name} v\\d+\\.\\d+\\.\\d+'
# replace = "{d.name} v{{version}}"
'''


def caller_text(d: Detected, ci: str) -> str:
    uses = f"{BOT_REPO}/.github/workflows"
    return f"""name: Release

# release-lanes: https://github.com/{BOT_REPO}. The policy in
# .github/release-policy.toml decides what releases and when; this file only wires the
# bot to this repository's CI.

on:
  push:
    branches: [{d.branch}]
  schedule:
    - cron: "7 * * * *" # hourly: the policy decides whether a lane's window is open
  workflow_dispatch:
    inputs:
      lane:
        description: "Release this lane now, skipping its triggers (gates still apply); empty follows the policy"
        type: choice
        options: ["", dev, rc, stable, hotfix]
        default: ""
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
      lane: ${{{{ inputs.lane || '' }}}}
      dry-run: ${{{{ inputs.dry-run || false }}}}
      hotfix-prs: ${{{{ inputs.hotfix-prs || '' }}}}
      hotfix-from: ${{{{ inputs.hotfix-from || '' }}}}
    permissions:
      contents: write # pushes the release commit's work branch
      issues: read # release-blocker issues and milestones
      pull-requests: read # a hotfix's merge commits

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
"""


def init(root: Path, ci: str, force: bool) -> list[Path]:
    d = detect(root)
    written = []
    for path, text in ((root / POLICY_PATH, policy_text(d)), (root / CALLER, caller_text(d, ci))):
        if path.exists() and not force:
            raise ReleaseError(f"{path.relative_to(root)} exists; pass --force to overwrite it")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        written.append(path)
    return written
