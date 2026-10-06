# Install shipmill

shipmill has three parts. Each one works without the next, so stop after the part you
need:

| Part | Runs | You get |
|---|---|---|
| 1. The skills | in Claude Code, when you ask | triage, fixes, PR landing, and watch, on demand |
| 2. Release lanes | in the repo's own GitHub Actions | releases cut by policy from the CHANGELOG, no agent needed |
| 3. The agents' schedule | on your machine or in the cloud | triage and landing without you starting them |

The fast path, from the root of your repo:

```bash
claude plugin marketplace add shipmill/shipmill && claude plugin install shipmill@shipmill && claude "/shipmill:shipmill-setup"
```

It adds the marketplace, installs the plugin (both are safe to rerun), and starts Claude
Code with the `shipmill-setup` skill, which inspects the repo, asks you to choose the
lanes, and does parts 2 and 3 on a branch, ending with a pull request. The rest of this
page is what it does, for doing it by hand or checking its work.

## Before you start

- A GitHub repository with a hand-written `CHANGELOG.md`, or one you're willing to start
- [`gh`](https://cli.github.com/) signed in (`gh auth status`) as someone who can push,
  run workflows, and change the repo's settings
- [`uv`](https://docs.astral.sh/uv/): the CLI runs through `uvx`, with nothing installed
  globally. Python 3.10 or newer for the skills' scripts
- [Claude Code](https://code.claude.com/) for parts 1 and 3

The commands below use:

```bash
CR="uvx --from git+https://github.com/shipmill/shipmill@v0 shipmill"
```

## 1. Install the skills

In Claude Code, run these one at a time. The first adds the marketplace, the second
installs the plugin from it:

```
/plugin marketplace add shipmill/shipmill
```

```
/plugin install shipmill@shipmill
```

In the interactive `/plugin` menu, the marketplace source is only `shipmill/shipmill`.

The skills are then `/shipmill:github-issue-triage`, `/shipmill:github-pr-triage`, and so
on; [flow.md](flow.md) shows what to say to each. To turn the plugin on for everyone who
opens the repo, commit this to its `.claude/settings.json`:

```json
{
  "extraKnownMarketplaces": {
    "shipmill": { "source": { "source": "github", "repo": "shipmill/shipmill" }, "autoUpdate": true }
  },
  "enabledPlugins": { "shipmill@shipmill": true }
}
```

For a checkout of shipmill you edit, or for Codex, link the skill folders instead:

```bash
for s in ~/src/shipmill/skills/*/; do ln -sfn "$s" ~/.agents/skills/; done
```

## 2. Set up release lanes

Work on a branch; everything below lands in one pull request.

### Prerequisites

| Needed | Fix |
|---|---|
| `CHANGELOG.md` with an Unreleased section, in keep-a-changelog (`## [Unreleased]`) or dash (`## Unreleased`) style | create it, or rewrite the entries `doctor` names |
| A stable `vX.Y.Z` tag to count from | tag the last release; shipmill never invents one |
| A CI workflow other workflows can call, on the commit they name | add the trigger below and pass `ref: ${{ inputs.ref }}` to `actions/checkout` |
| A publish workflow started by hand with a `tag` input, building that tag | add the input; drop any check that the tag is on main, since rc and stable tags are off it |
| `github-actions[bot]` can push main, `release/*`, and `v*` tags | let it bypass the branch rules, or landing fails |
| No other release automation | remove or disable it in the same pull request, so two tools never tag |

The CI trigger:

```yaml
on:
  workflow_call:
    inputs:
      ref:
        type: string
        required: true
```

Outside a call `inputs.ref` is empty, so pushes and pull requests keep checking out their
own commit.

### Write the files

```bash
$CR init --ci ci.yml
```

This writes `.github/shipmill.toml` in `mode = "dry-run"` and
`.github/workflows/release.yml`, which wires shipmill's reusable workflows to your CI.
Then edit the policy: which lanes, when each releases, what each dispatches, and the
gates. The keys are in the README, under [The policy](../README.md#the-policy). For a uv
project, add `after_stamp = ["uv lock --check"]`.

### Check

```bash
$CR doctor
$CR plan --lane rc --dry-run
$CR plan --lane stable --dry-run
```

`doctor` must pass every line. It checks:

- A CHANGELOG with an Unreleased section, in keep-a-changelog or dash style
- A stable `vX.Y.Z` tag to count from
- A CI workflow that runs on `workflow_call` with a `ref` input and checks that ref out
- A workflow for each `dispatch` entry that runs on `workflow_dispatch` with a `tag` input
- For each environment, a workflow that runs on `workflow_dispatch` with `tag` and
  `environment` inputs, and a job that sets `environment:`
- When an environment uses `from` or `health`, `.github/workflows/operate.yml` (from
  `shipmill init --operate`) granting `deployments: write` and `actions: write`, and
  `issues: write` once an environment has `health` (for incidents) or a stage proposes
- An `[operate]` section with known keys only, `rollback_after` in 1..20 and a non-empty
  `incident_label`
- No `[tool.uv.sources]` entry taken from a local path, which CI and users don't have
- No branch named `shipmill` on origin, which would block the `shipmill/<tag>` work branches

Each `plan` prints JSON; its `version` must be the next version you expect. If it isn't,
fix the bump lists or the tags, not the version.

### Go live

1. Open the pull request and merge it
2. Run the Release workflow by hand in dry-run, and read the release commit it would make
   in the run's summary:

   ```bash
   gh workflow run release.yml -f dry-run=true
   ```

3. Create the labels the skills and gates read (`postponed`, `blocked`, `shipmill-hold`,
   and the blocker label). The setup checklist reports what's missing, and `--fix` creates
   it:

   ```bash
   uv run --no-project python <skills>/shipmill-setup/scripts/setup_state.py <owner/repo> --fix
   ```

   `<skills>` is the plugin's `skills` folder, or a checkout's. `--fix` also turns the
   plugin on in `.claude/settings.json`; commit that in a follow-up pull request. It turns
   on the repo setting that deletes a pull request's branch when it merges, too: stacked
   pull requests are retargeted only when the branch under them is deleted

4. Set `mode = "release"` in `.github/shipmill.toml` and merge it

From here, releases are cut by policy on each push to main, on the lanes' schedules, and
by hand from the Release workflow.

## 3. Choose where the agents run

Releasing needs no agent. Triage, landing, and the shipped notices do, and run wherever
you start them:

| Option | Runs | Stops when |
|---|---|---|
| On demand | in your session, when you invoke a skill | the task is done |
| `/loop 30m /github-ship-watch <owner/repo> — watch and triage` | in an open session | the session closes |
| The gate | a new background session on your machine, only when the repo needs one | you remove the job |
| A `/schedule` routine | in the cloud, with your machine off | you delete it |

Start on demand. For anything that runs for days, prefer the gate: each launch is a fresh
session, and a quiet tick makes no model call. A cloud routine may not load the plugin,
so give it the prompt in [flow.md](flow.md#keeping-it-running), which clones shipmill.

### The gate

`shipmill gate` reads the repo's state and starts a Claude Code background session only
when something needs an agent, no session it started is still working or waiting on you,
and the findings changed since its last launch. Something needs an agent when
github-ship-watch's `watch_state.py` marks a row `agent: true`: a failed or stalled release
bot, a release missing from PyPI or not announced, issues triage owes, a failed operate
run, or an open incident; open PRs count too with `prs = true`. A promotion waiting on
you or an unhealthy environment starts nothing. [Agent modes](design/agent-modes.md) has
the details.

1. **Say what a session may do**, in `.github/shipmill.toml`, merged before the job
   starts. Start without merges for the first week:

   ```toml
   [agents]
   prompt = "/github-issue-triage {repo} triage the new issues; do not merge"
   prs = false        # true once the prompt says "merge when green"
   retry_hours = 24   # unchanged findings start a new session after this
   notify = true      # a desktop notification when a session waits on you
   remind_hours = 4   # repeat it while the session still waits
   max_wait_minutes = 15 # stop a session that waited this long; 0: never
   # app_id = 123456  # sessions write as this GitHub App; unset: as your gh login
   ```

   Without `app_id`, a session writes as your `gh` login, so its pull requests, merges,
   and commits read as yours. Set it to have them made by a GitHub App's bot instead
   (see [Give the sessions their own identity](#give-the-sessions-their-own-identity))

2. **Give it its own checkout.** A session branches and commits where it starts, so never
   your working copy. Inside the repo's trusted checkout:

   ```bash
   git worktree add --detach tmp/shipmill-gate origin/main
   ```

   If `git check-ignore tmp` prints nothing, add `tmp/` to `.git/info/exclude`

3. **Try it once.** This prints the decision and changes nothing:

   ```bash
   $CR --repo tmp/shipmill-gate gate <owner/repo> --dry-run
   ```

4. **Schedule it.** On a Mac:

   ```bash
   $CR --repo tmp/shipmill-gate launchd <owner/repo> --every 15
   ```

   This writes `~/Library/LaunchAgents/dev.shipmill.gate.<owner>.<repo>.plist`, loads it,
   and runs it once now; the log is under `~/Library/Logs/shipmill/`. On Linux, run
   `$CR --repo tmp/shipmill-gate gate <owner/repo> --refresh` from a systemd timer

5. **Watch a session** with `claude agents` and `claude attach <id>`. A session waiting on
   your answer holds the repo until you give it

### Give the sessions their own identity

Optional. With a GitHub App, a session's pull requests, comments, reviews, merges, and
commits are made by the App's bot, `<slug>[bot]`, not by you: you can approve its pull
requests, and github-ship-watch's metrics count its merges as a bot's. The token it uses
reaches this one repo only, never everything your login can.

The quick way is one command:

```bash
shipmill app-create --dry-run   # print the plan; create nothing
shipmill app-create             # create it: one click on GitHub, then pick the repos
```

It finds the accounts your `gh` login administers (it needs the `read:org` scope: `gh auth
refresh -s read:org`) and the repos in them holding `.github/shipmill.toml`, then plans:

| Your gated repos | The App |
|---|---|
| all in your personal account, or none yet | yours, private |
| any in an org | yours, public, so it installs on the org |

It asks who should own it, your personal account first as the default; pick an org you
administer to have the org own it, or pass `--owner`.

A private App can be installed only on the account that owns it; a public one on any
account, but only you hold its key, so nobody else can use it. The App is named
`shipmill-<owner>`, or `shipmill-<login>` when that is taken. `--owner`, `--public`,
`--private`, `--name`, and `--repos` change the plan. The command opens GitHub's create
page with the permissions below filled in and the webhook off; you click **Create GitHub
App**, then pick the repos on the install page it opens. It saves the key to
`~/.config/shipmill/app-<app_id>.pem` with mode `0600`, waits for the installations, and
prints the `app_id = <id>` line for step 4. Then go on at step 4.

By hand instead:

1. **Create the App** once, in GitHub's settings (Developer settings, GitHub Apps, New
   GitHub App). Clear Webhook's Active box: it needs no
   webhook. Give it these repository permissions and no others:

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

2. **Install it** only on the repos the gate works on ("Only select repositories")

3. **Save its private key** where the gate looks for it, readable by you alone. Generate
   one on the App's page, then:

   ```bash
   mkdir -p ~/.config/shipmill
   mv ~/Downloads/<app>.*.private-key.pem ~/.config/shipmill/app-<app_id>.pem
   chmod 600 ~/.config/shipmill/app-<app_id>.pem
   ```

   The gate refuses a key readable by group or others. The key never goes in the repo; a
   key elsewhere goes to `gate --app-key <path>`, and to `launchd --app-key <path>`, which
   puts it in the job's arguments

4. **Set `app_id`** in `[agents]` to the App ID on the App's settings page (not its client
   ID), and merge it

5. **Prove it** with the gate's dry run (step 3 above). When the tick would launch, it
   also checks the key, signs the App's JWT with `openssl`, checks the installation and
   every permission above, and prints `would launch as <slug>[bot]`; anything missing
   exits 2 naming it. It writes nothing. A tick that would launch nothing checks no App

Each launch then runs those checks again, writes a `gh` wrapper and
`git-credential-shipmill` (mode `0700`, holding no token) to
`$(git rev-parse --git-common-dir)/shipmill/bin/`, and starts the session with that folder
first on its `PATH` and `<slug>[bot]` as its git author. Each `gh` or `git push` call mints
or reuses a token through `shipmill app-token`, cached in `shipmill/app-token.json`
(mode `0600`) while it has at least 10 minutes left. A failed check starts no session and
never falls back to your login. The gate's own reads still use your `gh` login. Remove
`app_id` and sessions launch as before, as your `gh` login

## Pause or remove it

| To | Do |
|---|---|
| Pause releases and the gate | open an issue labelled `shipmill-hold`; a hotfix started by hand still runs (D-8, D-15) |
| Stop releases | set `mode = "off"` in `.github/shipmill.toml` |
| Stop the gate | `$CR --repo tmp/shipmill-gate launchd <owner/repo> --remove` |
| Remove the skills | `/plugin uninstall shipmill@shipmill` |

Everything shipmill knows lives in the repo and on GitHub, so removing it leaves your
tags, releases, issues, and CHANGELOG as they are.
