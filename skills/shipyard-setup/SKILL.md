---
name: shipyard-setup
description: Set up shipyard in a repository so releases are cut by policy on lanes (dev, rc, stable, hotfix) from its hand-written CHANGELOG. Use when the user asks to add a release bot, automate releases, set up release lanes, scheduled or nightly releases, release candidates, or hotfix releases, or to move a repo onto release lanes. Also connects the agent side, so the repo triages, fixes, and lands on its own: the plugin in the repo's settings, the labels the skills read, the config's [agents] section, and where the agents run, including `shipyard gate` on launchd. Use too when the user asks to "connect a repo to shipyard" or to make it "ship by itself". Covers prerequisites, choosing lanes with the user, writing the policy, wiring CI and publishing, a dry run, and migration from hand-made or scripted releases.
---

# Set up shipyard

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

### Environments

Ask whether releases deploy somewhere (a staging or production service), and skip this
for a library that only publishes. For each environment the user names, add a table to the
policy with exactly one of `lane` (deploy every release of that lane when it lands) or
`from` (promoted from another environment; shipyard does not promote yet), plus the
`workflow` that deploys and an optional `health` URL:

```toml
[environments.staging]
lane = "rc"
workflow = "deploy.yml"
health = "https://staging.example.com/health"
```

The deploy workflow is the repo's own. `land` starts it with `tag` and `environment`
inputs; it deploys that tag, and its job sets `environment:` so GitHub records a
deployment, the record of what runs where:

```yaml
on:
  workflow_dispatch:
    inputs:
      tag:
        type: string
        required: true
      environment:
        type: string
        required: true
jobs:
  deploy:
    runs-on: ubuntu-latest
    environment: ${{ inputs.environment }}
    steps:
      - uses: actions/checkout@v7
        with:
          ref: refs/tags/${{ inputs.tag }}
```

Turn an existing deploy workflow into this shape rather than writing a second one, and
remove its own trigger on tags or pushes, so one release deploys once.

## 4. Write the files

```bash
$CR init --ci <ci-file>.yml
```

This writes `.github/shipyard.toml` (dry-run) and `.github/workflows/release.yml`. A repo
that already has the older name, `.github/release-policy.toml`, keeps working with it; `init`
refuses to write next to it, and `--force` replaces it with `.github/shipyard.toml`.
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
3. Run the checklist: `uv run --no-project python <skill>/scripts/setup_state.py <owner/repo>`.
   It reports releases, the `[agents]` section, the plugin in `.claude/settings.json`, and
   the labels (`postponed`, `blocked`, `shipyard-hold`, and the blocker label). `--fix`
   enables the plugin, keeping every other key, and creates the missing labels on GitHub
   at once; commit the settings change in a follow-up PR. A plugin turned off on purpose
   (`PLUGIN_DISABLED`) is the user's call; ask before changing it
4. Ask the user to switch `mode = "release"` once the dry run looks right
5. Ask where the agents will run: releasing needs none of them, but triage, landing, and
   the shipped notices do. Recommend on demand to start, and offer the other two:

   | Option | Runs | Stops when |
   |---|---|---|
   | On demand | in the user's session, when they invoke a skill | the task is done |
   | `/loop 30m /github-ship-watch <owner/repo> — watch and triage` | in this session | the session closes |
   | The gate (`shipyard gate` on launchd) | a new background session on this machine, only when the repo needs one, with its memory and repos | the job is removed |
   | A `/schedule` routine | in the cloud, with the user's computer off | the user deletes it |

   Recommend the gate over `/loop` for anything that runs for days: each pass is a fresh
   session, and a quiet tick makes no model call (see [The gate](#the-gate))

   For a routine, also ask whether it may merge: it merges only when its prompt says
   "merge when green". Write the prompt so it clones shipyard rather than relying on the
   plugin, which a cloud routine may not load (docs/flow.md, Keeping it running), and offer
   to create it with `/schedule`

## The gate

`shipyard gate` is code that reads the repo's state and starts a Claude Code background
session (`claude --bg`) only when something needs an agent, no session it started is still
working or waiting on the user, and the findings changed since its last launch.
docs/design/agent-modes.md has the details.

1. **Scope.** Ask what the session may do; the prompt carries it. Recommend triage without
   merges for the first week: `/github-issue-triage {repo} triage the new issues; do not
   merge`. "merge when green" lets it land PRs; then also set `prs = true`, so open PRs
   count as work
2. **Config.** Add the section to `.github/shipyard.toml` in a PR, and merge it before the
   job starts:

   ```toml
   [agents]
   prompt = "/github-issue-triage {repo} triage the new issues; do not merge"
   prs = false        # true when the prompt lands PRs
   retry_hours = 24   # unchanged findings start a new session after this
   ```
3. **A dedicated checkout.** The session branches and commits where it starts, so never
   use the user's working copy: `git worktree add --detach tmp/shipyard-gate
   origin/<default>` inside the trusted checkout (a worktree outside it would need its
   own trust prompt). If `git check-ignore tmp` prints nothing, add `tmp/` to
   `.git/info/exclude`, which stays local. The gate moves the checkout to the default
   branch's head before each launch
4. **Try it once.** `$CR --repo tmp/shipyard-gate gate <owner/repo> --dry-run` prints the
   decision and changes nothing
5. **Schedule it.** `$CR --repo tmp/shipyard-gate launchd <owner/repo> --every 15` writes
   `~/Library/LaunchAgents/dev.shipyard.gate.<owner>.<repo>.plist`, loads it, and runs
   it once now. Its log is under `~/Library/Logs/shipyard/`. `--remove` unloads it; on
   Linux, run the same `gate` command from a systemd timer
6. **Hand over.** Tell the user how to see a session (`claude agents`, `claude attach
   <id>`), that a session waiting on a question holds the repo until they answer, and
   how to stop it all (`launchd <owner/repo> --remove`)

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
