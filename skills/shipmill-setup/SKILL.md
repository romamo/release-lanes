---
name: shipmill-setup
description: Set up shipmill in a repository so releases are cut by policy on lanes (dev, rc, stable, hotfix) from its hand-written CHANGELOG. Use when the user asks to add a release bot, automate releases, set up release lanes, scheduled or nightly releases, release candidates, or hotfix releases, or to move a repo onto release lanes. Also connects the agent side, so the repo triages, fixes, and lands on its own: the plugin in the repo's settings, the labels the skills read, the config's [agents] section, and where the agents run, including `shipmill gate` on launchd. Use too when the user asks to "connect a repo to shipmill" or to make it "ship by itself". Covers prerequisites, choosing lanes with the user, writing the policy, wiring CI and publishing, a dry run, and migration from hand-made or scripted releases.
---

# Set up shipmill

shipmill cuts releases from a hand-written CHANGELOG on lanes the project's policy
defines. Read the project's README section "How a release works" once before starting:
pre-releases are tagged off main, and stable releases promote a soaked rc.

Run the tool with:

```bash
CR="uvx --from git+https://github.com/shipmill/shipmill@v0 shipmill"
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
`from` (promoted from another environment once it baked there), plus the
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
  ref:
    runs-on: ubuntu-latest
    steps:
      - name: Fail a run that isn't on the tag it deploys
        env:
          TAG: ${{ inputs.tag }}
          ENVIRONMENT: ${{ inputs.environment }}
        run: |
          if [ "$GITHUB_REF" != "refs/tags/$TAG" ]; then
            echo "::error::run it on the tag: gh workflow run deploy.yml --ref $TAG -f tag=$TAG -f environment=$ENVIRONMENT"
            exit 1
          fi
  deploy:
    needs: ref
    runs-on: ubuntu-latest
    environment: ${{ inputs.environment }}
    steps:
      - uses: actions/checkout@v7
        with:
          ref: refs/tags/${{ inputs.tag }}
  operate: # with from or health: the first health check, right after the deploy
    needs: deploy
    runs-on: ubuntu-latest
    permissions:
      actions: write
    steps:
      - env:
          GH_TOKEN: ${{ github.token }}
          GH_REPO: ${{ github.repository }}
        run: gh workflow run operate.yml -f dry-run=false
```

Turn an existing deploy workflow into this shape rather than writing a second one, and
remove its own trigger on tags or pushes, so one release deploys once. shipmill starts it on
the tag, so the deployment GitHub records names the tag as its ref. Keep the `ref` job: GitHub
records the run's ref, not the checked-out tag, so a run started by hand without `--ref <tag>`
would record a branch, and the job fails it first with the command to run instead (`doctor`
warns when a deploy workflow lacks it). The `operate` job matters once an environment has
`health` or `from` (and `init --operate` has written `operate.yml`): it starts operate as
soon as the deploy succeeds, so a bad deploy gets its first health check right away instead
of at the next 10-minute run. Keep it a separate job that needs the deploy job, so a failure
to start operate doesn't mark the deployment failed; drop it for an environment without
either.

If the environment already exists on GitHub with deployment protection, it must also allow
the release tags, since `land` starts the workflow on the tag (enabling GitHub Pages creates
`github-pages` allowing only `main`). Add a tag rule `v*` under Settings → Environments →
`<environment>` → Deployment branches and tags, or run
`gh api -X POST repos/<owner>/<repo>/environments/<environment>/deployment-branch-policies -f name='v*' -f type=tag`.

When an environment has `health` or `from`, also run `$CR init --operate` in step 4. It
writes `.github/workflows/operate.yml`, which runs `shipmill operate` every 10 minutes:
it checks each `health` URL (2xx within 10 s; a JSON body's `version` must name the
deployed release), records the result as deployment statuses, and promotes a `from`
environment once its source stayed healthy for `bake_minutes`. An environment failing
`[operate] rollback_after` checks in a row (default 3) is rolled back to its last good tag
and gets an `incident` issue, which holds the rc and stable lanes until closed. Ask whether
production should deploy by itself after the bake (the default) or wait for approval
(`deploy.production = "propose"` in `[autonomy]`, approved with `gh workflow run
operate.yml -f approve=production -f dry-run=false`), and the same for rollbacks
(`rollback = "propose"`, approved with `-f approve-rollback=production`). Point the user at
`.github/workflows/operate.yml` as an extra scheduled workflow, and skip it for a
library with no environments.

## 4. Write the files

```bash
$CR init --ci <ci-file>.yml
```

This writes `.github/shipmill.toml` (dry-run) and `.github/workflows/release.yml`.
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
   the labels (`postponed`, `blocked`, `shipmill-hold`, and the blocker label). `--fix`
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
   | The gate (`shipmill gate` on launchd) | a new background session on this machine, only when the repo needs one, with its memory and repos | the job is removed |
   | A `/schedule` routine | in the cloud, with the user's computer off | the user deletes it |

   Recommend the gate over `/loop` for anything that runs for days: each pass is a fresh
   session, and a quiet tick makes no model call (see [The gate](#the-gate))

   For a routine, also ask whether it may merge: it merges only when its prompt says
   "merge when green". Write the prompt so it clones shipmill rather than relying on the
   plugin, which a cloud routine may not load (docs/flow.md, Keeping it running), and offer
   to create it with `/schedule`

## The gate

`shipmill gate` is code that reads the repo's state and starts a Claude Code background
session (`claude --bg`) only when something needs an agent, no session it started is still
working or waiting on the user, and the findings changed since its last launch. Something
needs an agent when github-ship-watch's `watch_state.py` marks a row `agent: true`
(github-ship-watch's SKILL.md lists them), plus open PRs with `prs = true`.
docs/design/agent-modes.md has the details.

1. **Scope.** Ask what the session may do; the prompt carries it. Recommend triage without
   merges for the first week: `/github-issue-triage {repo} triage the new issues; do not
   merge`. "merge when green" lets it land PRs; then also set `prs = true`, so open PRs
   count as work
2. **Config.** Add the section to `.github/shipmill.toml` in a PR, and merge it before the
   job starts:

   ```toml
   [agents]
   prompt = "/github-issue-triage {repo} triage the new issues; do not merge"
   prs = false        # true when the prompt lands PRs
   retry_hours = 24   # unchanged findings start a new session after this
   ```
3. **A dedicated checkout.** The session branches and commits where it starts, so never
   use the user's working copy: `git worktree add --detach tmp/shipmill-gate
   origin/<default>` inside the trusted checkout (a worktree outside it would need its
   own trust prompt). If `git check-ignore tmp` prints nothing, add `tmp/` to
   `.git/info/exclude`, which stays local. The gate moves the checkout to the default
   branch's head before each launch
4. **Try it once.** `$CR --repo tmp/shipmill-gate gate <owner/repo> --dry-run` prints the
   decision and changes nothing
5. **Schedule it.** `$CR --repo tmp/shipmill-gate launchd <owner/repo> --every 15` writes
   `~/Library/LaunchAgents/dev.shipmill.gate.<owner>.<repo>.plist`, loads it, and runs
   it once now. Its log is under `~/Library/Logs/shipmill/`. `--remove` unloads it; on
   Linux, run the same `gate` command from a systemd timer
6. **Hand over.** Tell the user how to see a session (`claude agents`, `claude attach
   <id>`), that a session waiting on a question holds the repo until they answer, that an
   open `shipmill-hold` issue stops every launch as well as every release (D-11), and how
   to remove the job (`launchd <owner/repo> --remove`)

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
