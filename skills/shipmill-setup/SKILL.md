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
   the labels (`postponed`, `blocked`, `shipmill-hold`, the blocker label, and with
   `[agents] mode = "headless"` only, `needs-decision`), and
   whether GitHub deletes a pull request's branch when it merges (`BRANCH_DELETE_ON` or
   `BRANCH_DELETE_OFF`, the repo setting `delete_branch_on_merge`). That setting matters
   because github-pr-triage's stacked merges rely on GitHub retargeting a stacked PR when
   the branch under it is deleted, and without it merged branches pile up. `--fix`
   enables the plugin, keeping every other key, creates the missing labels, and turns the
   setting on, all on GitHub at once; commit the settings change in a follow-up PR. A
   plugin turned off on purpose
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

Each tick also prunes landed worktrees, before the hold check, so it prunes under a hold
too: it removes a worktree under `.claude/worktrees/` or `tmp/wt-*` and its local branch
once its commits landed on the default branch, it is clean and over a day old, and no
open PR or live Claude Code session holds it, and prints a `pruned <path> (<branch>)` line
for each (`would prune` under `--dry-run`). The worktrees a session leaves behind once it
exits are the gate's prune's to remove (D-12); never the main checkout, the gate's own
checkout, or a worktree elsewhere. `$CR --repo tmp/shipmill-gate worktrees` lists every
worktree as REMOVABLE or KEPT with why it is kept (`--json` for a record, `--prune
--dry-run` for what a prune would remove); github-ship-watch reports a kept one older than
7 days as WORKTREE_STALE, for a person to finish or remove.

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
   notify = true      # a desktop notification when a session waits on you
   remind_hours = 4   # repeat it while the session still waits
   max_wait_minutes = 15 # stop a session that waited this long; 0: never
   app_id = 123456    # replace with the App ID step 3 prints; sessions write as its bot
   mode = "interactive" # step 4's answer: sessions ask you; "headless": they ask on GitHub
   ```

   Every time you set up the gate, ask two questions together, before writing the
   section: who should own the gate's GitHub App (step 3), and whether anyone watches
   this machine, which picks `mode` (step 4). Write both answers into `[agents]`: `app_id`
   and `mode = "interactive"` or `mode = "headless"`, never left to the default (D-20).
   A session that waits on a question holds the repo, so by default the gate stops it
   after 15 minutes. Ask whether the user answers questions sooner or later than that; set
   `max_wait_minutes` (0..10080) to match, or 0 to never stop one. `app_id` is a
   placeholder until step 3 replaces it; never merge `123456` itself
3. **Its own identity (required for the gate, D-19).** Every gate gets a GitHub App, so its
   sessions' pull requests, comments, merges, and commits are made by the App's bot
   (D-14). Without `app_id`, a session writes as the host's `gh` login: its work reads as
   the user's own, the user can't approve its pull requests, and GitHub doesn't notify
   them of its mentions. Until `app_id` is set, `setup_state.py` reads `AGENTS_NO_APP` and
   github-ship-watch reads `GATE_NO_APP`, both an action for the user (exit 1). Ask the
   user who should own the App (below), then create it with `$CR app-create` (spec 006),
   or for a repo added to an existing App, run `$CR app-install`:
   - **Plan it:** `$CR app-create --dry-run` finds the accounts the user's `gh` login
     administers and the repos in them holding `.github/shipmill.toml`, and prints the
     plan: the App is the user's own (personal account) by default, **private** when every
     gated repo is in it and **public** when any is in an org (a private App installs only
     on its owner; a public one on any account, but only the key's holder can mint its
     tokens). Ask the user who should own it, their personal account first as the default
     or an org they administer, then show the plan and ask whether to create it; `--owner`,
     `--public`, `--private`, and `--name` change it. It needs the `read:org` scope, and
     it names the App `shipmill-<owner>`, or `shipmill-<login>` when that is taken, saying so
   - **Create it:** once the user agrees, run it yourself with their `--owner` and without
     `--dry-run`, in the
     background: it waits up to 10 minutes for the click. It opens a local page showing
     the plan and the permissions; the user clicks **Create on GitHub**, then **Create
     GitHub App** on GitHub's page, where every field is filled in. If GitHub asks them to
     sign in first it shows *We didn't find an App Manifest*: they go back to the local
     page and click again. Then they pick the repos on the install page it opens next. It saves the key to `~/.config/shipmill/app-<app_id>.pem`
     (mode `0600`), prints `installed on <owner/repo>` as each appears, and ends with the
     `app_id = <id>` line to commit; exit 1 names a repo still missing the App
   - **Set `app_id`** in `[agents]` through a PR, in each gated repo: the printed id in
     place of the placeholder
   - **A repo connected later, or in another account:** run `$CR app-install <owner/repo>`.
     It never installs or adds anything itself: for a repo the App doesn't cover it opens
     the App's **Install App** page and says what to click there (Install for a new account,
     Configure to add a repo to an existing installation). Relay those steps, ask the user
     to click, and wait for its `installed on <owner/repo>`

   By hand instead, in GitHub's settings (Developer settings, GitHub Apps, New GitHub App):
   - **No webhook:** clear Webhook's Active box; the App needs no callback URL either
   - **These repository permissions, and no others:**

     | Permission | Access | Why |
     |---|---|---|
     | Contents | write | push branches, merge |
     | Pull requests | write | open, review, and merge pull requests |
     | Issues | write | comments, labels, closing |
     | Actions | write | rerun a failed release job (github-ship-watch) |
     | Workflows | write | push a change under `.github/workflows/` |
     | Checks | read | CI state |
     | Commit statuses | read | CI state |
     | Discussions | read | product-intake's input |
     | Metadata | read | required by GitHub |

   - **Install it only on the repos the gate works on** ("Only select repositories")
   - **The private key:** generate one on the App's page, move the download to
     `~/.config/shipmill/app-<app_id>.pem`, and `chmod 600` it. The key is a host secret
     and never goes in the repo; a key elsewhere goes to `gate --app-key <path>`
   - **`app_id`:** the App ID from its settings page (not the client ID), set in
     `[agents]` in the same PR as the rest of the section

   With `app_id` set, each launch reads the key (missing, or readable by group or others:
   exit 2 naming its path), signs the App's JWT with `openssl dgst -sha256 -sign`, checks
   that the App is installed on the repo with every permission above, and starts the
   session as `<slug>[bot]`, with `<id>+<slug>[bot]@users.noreply.github.com` as its git
   author and committer. The session holds no token: the gate writes two helpers, mode
   `0700`, to `$(git rev-parse --git-common-dir)/shipmill/bin/` (a `gh` wrapper and
   `git-credential-shipmill`), puts that folder first on the session's `PATH`, and sends
   `git@github.com:` remotes over https so a push goes through the helper too. Each call
   runs `shipmill app-token`, which mints an installation token limited to this one repo
   and caches it in `shipmill/app-token.json` (mode `0600`) next to them while it has at
   least 10 minutes left. Any failure exits 2 and starts no session; it never falls back
   to the user's login. The gate's own reads keep the host's `gh` login. A session that
   works on other repos (fleet mode) fails there, since the token is limited to this one.
   A gate without `app_id` (one set up before D-19) still runs: its sessions launch as
   before, as the host's `gh` login, and the two reports above flag it until it is set
4. **Interactive or headless (asked every time, D-20).** On a machine nobody watches, a
   session that asks a question holds the repo until someone attaches. Ask whether anyone
   watches this one, with step 3's App question, and write the answer in `[agents]`:
   - **`mode = "interactive"`:** someone attaches to a session (`claude attach <id>`) and
     answers its questions on this machine
   - **`mode = "headless"`** (spec 005, D-17): questions go to GitHub instead

   The config's default is interactive, so a gate set up before D-20 keeps working, but
   never rely on it: with no `mode` key `setup_state.py` reads `AGENTS_NO_MODE`, an
   action (exit 1) that `--fix` leaves to the user. In headless mode each launch runs
   `claude -p` with no prompts, AskUserQuestion removed, and an allowlist of tools; it
   can't wait on anyone, so it ends, and a decision for the user
   becomes the needs-decision protocol
   ([needs-decision.md](../github-issue-triage/references/needs-decision.md)): a comment
   whose first line is `<!-- shipmill:needs-decision -->`, mentioning the host's `gh`
   login, and the `needs-decision` label. The item waits on GitHub, out of the gate's work,
   until an OWNER, MEMBER, or COLLABORATOR replies; the rest of the repo keeps moving. Tell
   the user:
   - **The allowlist** is `HEADLESS_TOOLS` in `src/shipmill/gate.py`: `Read Edit Write
     Glob Grep Skill Agent SendMessage ListAgents TodoWrite Bash(gh *) Bash(git *) Bash(uv
     *) Bash(uvx *)`. A call outside it is denied, and the session posts a needs-decision
     comment naming the tool. To widen it, pass `--claude-arg=--allowedTools --claude-arg
     "Bash(npm *)"` (the `=` matters: `--claude-arg --allowedTools` is an argument error)
     to `gate`, and to `launchd` again so the job gets it. It is not a sandbox: `git`, `gh`,
     and `uv run` run code. Headless refuses a `--claude-arg` of `--permission-mode`,
     `--permission-prompts`, `--dangerously-skip-permissions`,
     `--allow-dangerously-skip-permissions`, `--bg`, `--background`, or `--session-id`
     with exit 2
   - **The trust filter** (D-16): headless works only on issues opened by an OWNER,
     MEMBER, or COLLABORATOR or by the App's bot, and on pull requests whose branch is in
     the repo. The rest read UNTRUSTED (`watch_state.py --trusted-only`), start no session,
     and wait for an interactive one; github-ship-watch reports them
   - **The label:** `setup_state.py --fix` creates `needs-decision` in headless mode
     only; it reads `LABELS_MISSING` until then. With `interactive` or no `[agents]` it
     isn't wanted
   - **Notifications:** with `app_id`, the question is `<slug>[bot]`'s, so its mention
     notifies the user on GitHub and the gate sends no desktop notification. Without it,
     the comment is the user's own and GitHub doesn't notify anyone of their own mention:
     `setup_state.py` reads `AGENTS_NO_APP` (an action, D-19), a launch prints `no app_id:
     needs-decision comments post as <login>, so GitHub won't notify you`, and with
     `notify = true` the gate sends a desktop notification, `#<n> waits on your decision:
     <url>`, at once and every `remind_hours` while the item waits. Either way it records
     waiting items in `shipmill/needs-decision.json`, and `gate --json` reports `mode` (null
     on a HELD, WAITING, or RUNNING tick, which reads no config) and `decisions`
5. **A dedicated checkout.** The session branches and commits where it starts, so never
   use the user's working copy: `git worktree add --detach tmp/shipmill-gate
   origin/<default>` inside the trusted checkout (a worktree outside it would need its
   own trust prompt). If `git check-ignore tmp` prints nothing, add `tmp/` to
   `.git/info/exclude`, which stays local. The gate moves the checkout to the default
   branch's head before each launch
6. **Try it once.** `$CR --repo tmp/shipmill-gate gate <owner/repo> --dry-run` prints the
   decision and changes nothing. With `app_id` set, a tick that would launch also checks
   the key, the installation, and the permissions, writes no helpers and no token cache,
   and prints `would launch as <slug>[bot]`: that line proves the App's setup. A tick
   that would launch nothing checks no App; then `$CR app-install <owner/repo>` printing
   `already installed on <owner/repo>` confirms at least the installation. Don't go on to
   step 7 until `app_id` is set and one of the two has passed
7. **Schedule it.** `$CR --repo tmp/shipmill-gate launchd <owner/repo> --every 15` writes
   `~/Library/LaunchAgents/dev.shipmill.gate.<owner>.<repo>.plist`, loads it, and runs
   it once now. Its log is under `~/Library/Logs/shipmill/`. `--remove` unloads it; on
   Linux, run the same `gate` command from a systemd timer. With an App whose key isn't at
   the default path, add `--app-key <path>`: the job's gate gets it, made absolute, and
   install refuses it when `app_id` is unset or the key is missing or readable by others
8. **Hand over.** Tell the user how to see a session (`claude agents`, `claude attach
   <id>`), that a session waiting on a question holds the repo until they answer, that
   such a session sends a desktop notification at once and every `remind_hours` while it
   waits (`osascript` on macOS, `notify-send` elsewhere), that `max_wait_minutes` (15 by
   default; 0 never stops) stops it with `claude stop` so the repo moves again (`claude attach <id>` still
   shows its question), that with an App a session's work is `<slug>[bot]`'s, which they
   can approve and github-ship-watch's metrics count as a bot, that an open `shipmill-hold` issue stops every launch as well as
   every release but not that stop (D-15), and how to remove the job (`launchd
   <owner/repo> --remove`). In headless mode, tell them instead that a session never
   waits: `tail -f` its log under `shipmill/sessions/`, `claude --resume <uuid>` reopens
   it, and they answer a `needs-decision` item by replying on GitHub

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
