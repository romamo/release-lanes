---
name: release-lanes-setup
description: Set up the shipyard release bot in a repository so releases are cut by policy on lanes (dev, rc, stable, hotfix) from its hand-written CHANGELOG. Use when the user asks to add a release bot, automate releases, set up release lanes, scheduled or nightly releases, release candidates, or hotfix releases, or to move a repo onto release lanes. Covers prerequisites, choosing lanes with the user, writing the policy, wiring CI and publishing, a dry run, and migration from hand-made or scripted releases.
---

# Set up release lanes

shipyard cuts releases from a hand-written CHANGELOG on lanes the project's policy
defines. Read the project's README section "How a release works" once before starting:
pre-releases are tagged off main, and stable releases promote a soaked rc.

Run the tool with:

```bash
CR="uvx --from git+https://github.com/romamo/shipyard@v0 shipyard"
```

Work on a branch and finish with a pull request. Never set `mode = "release"` in the
first change: the bot starts in `dry-run`.

## 1. Inspect the repository

Collect these facts before asking the user anything; each one has a fixed answer in the
repo:

| Fact | Where to look | What the bot needs |
|---|---|---|
| Default branch | `git symbolic-ref refs/remotes/origin/HEAD` | the policy's `branch` |
| CHANGELOG style | `CHANGELOG.md` headings | `## [Unreleased]` with `## [X.Y.Z] - YYYY-MM-DD` and compare links (keep-a-changelog), or `## Unreleased` with `## X.Y.Z — YYYY-MM-DD` (dash) |
| Entry shape | the Unreleased section | keep-a-changelog: `- ` bullets under `### Category` headings, continuation lines indented; dash: one `### Title` block per entry |
| Version source | `pyproject.toml` `[project] version` | `version_files = "pyproject"`; a dynamic version or a non-Python project uses `"none"` plus `version_lines` |
| Other version strings | `git grep -n "<current version>"` | one `[[version_lines]]` entry each, a pattern matching exactly once |
| Stable tags | `git tag -l 'v*'` | at least one `vX.Y.Z`; the bot counts from the newest |
| CI workflow | `.github/workflows/*.yml` with the test jobs | must run on `workflow_call` with a `ref` input and check out `${{ inputs.ref }}` |
| Publish workflow | the one that uploads to PyPI, npm, or similar | must run on `workflow_dispatch` with a `tag` input and build `refs/tags/<tag>` |
| Existing release automation | release scripts, a release bot, tag-triggered workflows | replaced or disabled in the same pull request, so two tools never tag |
| Branch protection | `gh api repos/{owner}/{repo}/rulesets`, `.../branches/<branch>/protection` | `github-actions[bot]` must be able to push main, `release/*`, and `v*` tags |
| Python package basics | `pyproject.toml`, `src/`, the repo root | see [Python packages](#python-packages); a release ships whatever the build picks up |

## 2. Fix the prerequisites

Make each change the inspection showed missing, in the same branch:

- **No CHANGELOG**: create one in keep-a-changelog style with an empty `## [Unreleased]`
  and an `[Unreleased]: https://github.com/<owner>/<repo>/compare/v<latest>...HEAD` link
- **Entries the parser rejects**: `doctor` names the line; rewrite it into the entry shape,
  never loosen the policy around it
- **No stable tag**: ask the user which commit and version to tag; don't invent one
- **CI not callable**: add to its `on:`

  ```yaml
  workflow_call:
    inputs:
      ref:
        type: string
        required: true
  ```

  and pass `ref: ${{ inputs.ref }}` to its `actions/checkout` steps (empty outside a
  call, so pushes and pull requests keep checking out their own commit)
- **Publish workflow**: give it a `workflow_dispatch` `tag` input and build that tag. Remove
  any check that the tagged commit is on main: rc and promoted stable tags are off main
- **Tag-triggered publish** (`on: push: tags`): keep it only if it does not also run for
  the bot's tags; a workflow-token push starts no workflow, so the bot always dispatches
- **Python package**: make the fixes in [Python packages](#python-packages)

### Python packages

The bot publishes what `uv build` makes from the tag, so check the package once here.
`doctor` fails a `[tool.uv.sources]` entry with a `path` or `editable`: CI and users don't
have that checkout. Fix the rest by hand:

- `[project]` has a one-line `description`, `license`, `authors`, `requires-python`, and
  `[project.urls]` with `Repository` and `Issues` from `git remote get-url origin`. Take
  the author and license from the user's memory or earlier packages; never write a
  placeholder
- `LICENSE` and `README.md` exist; a typed package ships `src/<pkg>/py.typed`
- `.gitignore` covers `.env`, `__pycache__`, `*.pyc`, and `.DS_Store`, and `git ls-files`
  lists none of them
- The sdist leaves out tooling: with hatchling, an explicit
  `[tool.hatch.build.targets.sdist]` `include` (`/src`, `/tests`, `/CHANGELOG.md`) or an
  `exclude` covering `/.github`, `/.agents`, `/.claude`, and `/uv.lock`
- The publish workflow builds the tag, checks the artifacts, and smoke-tests them before
  uploading. Add these steps after its checkout of the tag:

  ```yaml
  - run: uv build --out-dir dist
  - name: Check the sdist leaves out tooling
    run: |
      if tar -tzf dist/*.tar.gz | grep -E '^[^/]+/(\.github|\.agents?|\.claude|uv\.lock)(/|$)'; then
        exit 1
      fi
  - name: Smoke-test the wheel and the sdist
    run: |
      for dist in dist/*.whl dist/*.tar.gz; do
        uv run --isolated --no-project --with "$dist" python -c "import <pkg>"
      done
  ```

  A CLI checks its entry point too (`--with "$dist" <cli> --version`); a
  `tests/smoke_test.py` that calls one public function replaces the `import` line

## 3. Choose the lanes with the user

These are the user's decisions. Ask them together, with the defaults below as the
recommendation:

| Decision | Default | Notes |
|---|---|---|
| Lanes | `rc`, `stable`, `hotfix` | add `dev` when users install builds between rcs |
| rc cadence | `schedule = ["Mon-Fri 07:00 <their zone>"]` | the zone is an IANA name |
| Stable | `promote_from = "rc"`, `min_soak_days = 3`, a weekly window | without `promote_from`, stable releases main's head |
| Milestones | `milestone = true` on stable | due once the milestone titled with the version has no open issues |
| Bump rule | `from = "headings"` for keep-a-changelog | `from = "paths"` when headings are topics, such as a spec |
| Where releases go | `github_release = true`; `dispatch = ["publish.yml"]` | per lane; pre-releases are marked pre-release |
| Holds | `release-blocker` holding `rc` and `stable` | create the label; add `freeze` ranges for holidays |

A project that only wants "release after merges stop" uses a single
`[lanes.stable]` with `quiet_minutes = 30` and no `promote_from`.

## 4. Write the files

```bash
$CR init --ci <ci-file>.yml
```

This writes `.github/release-policy.toml` (dry-run) and `.github/workflows/release.yml`.
Edit the policy to the user's choices from step 3, then add `version_lines` and
`after_stamp` (for uv projects `["uv lock --check"]`; for a project with a docs check that
reads the version, that command too).

## 5. Verify

```bash
$CR doctor
```

Every line must be PASS; a WARN about the version is fine when the project is mid-series
(an rc on main). Then preview each lane locally (needs `gh auth status`):

```bash
$CR plan --lane rc --dry-run
$CR plan --lane stable --dry-run
```

Read the JSON: `version` must be what the user expects next. If it is not, the bump lists
or the tags are wrong; fix those, not the version.

## 6. Hand over

1. Open the pull request with the policy, the workflow, and the prerequisite changes
2. After it merges: run the Release workflow by hand (lane empty, dry-run on) and read the
   release commit in the run summary
3. Create the `release-blocker` label if the policy uses it
4. Ask the user to switch `mode = "release"` once the dry run looks right

## Migrating from hand-made or scripted releases

- Releases made by commits on main (`Release X` commits tagged on main) keep working as
  history: the bot reads their tags. Delete the old scripts and workflows in the same pull
  request
- A package from the retired oss-package-engineer skill (its `publish.yml` runs on
  `push: tags: v*`, installs Python 3.11, and has "Smoke test (wheel)" and "Smoke test
  (source distribution)" steps): keep its smoke-test steps and `tests/smoke_test.py`, give
  it the `workflow_dispatch` `tag` input, and drop the tag trigger. Its `chore: release vX`
  commits are history like any other; their CHANGELOG sections stay as written
- Under the bot, rc releases do not write CHANGELOG sections; their entries stay under
  Unreleased until the stable release. Existing rc sections stay as they are
- A project in an rc series (`1.0.0rc9`) continues it: the next rc is `1.0.0rc10`, and the
  stable lane promotes to `1.0.0`. If the user wants 1.0.0 to be a deliberate step, leave
  stable without triggers so it only runs by hand
