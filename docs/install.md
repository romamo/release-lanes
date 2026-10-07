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
- [`uv`](https://docs.astral.sh/uv/), to install the CLI. Python 3.10 or newer for the
  skills' scripts
- [Claude Code](https://code.claude.com/) for parts 1 and 3

Install the CLI from PyPI once, and upgrade it after a release:

```bash
uv tool install shipmill
uv tool upgrade shipmill
```

That puts `shipmill` on your `PATH` (`uv tool update-shell` adds uv's tool folder if it
isn't there yet). The commands below use:

```bash
CR=shipmill
```

Without the install, run the CLI from git instead. Each call resolves `@v0` again, which
can take minutes, and leaves a build in uv's cache:

```bash
CR="uvx --from git+https://github.com/shipmill/shipmill@v0 shipmill"
```

The skills run `shipmill` when it's on `PATH` and this uvx form when it isn't. A headless
gate session always runs the uvx form: its allowlist has `Bash(uvx *)` and no bare
`shipmill`.

shipmill is published to PyPI on each stable and hotfix release, as `shipmill`, the same
releases `@v0` follows. It needs Python 3.14: `uv tool install` and `uvx` fetch that
interpreter when you don't have it, while `pip install shipmill` on an older Python fails
to find a version that fits.

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

A linked skill answers its name without the plugin's prefix on this machine in place of
the plugin's, so a gate session whose prompt leaves the prefix out runs the checkout,
behind or on a feature branch. Prompts call the plugin's as `/shipmill:<skill>`, and `shipmill status`
and the setup checklist report each link or copy in `~/.claude/skills` or
`~/.agents/skills` as `SKILL_SHADOWED`, with its fix.

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
gates. The keys are in [The policy](release-lanes.md#the-policy). For a uv
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
- When an environment uses `from` or `health`, `.github/workflows/operate.yml` (which
  `init --operate` writes) granting `deployments: write` and `actions: write`, and
  `issues: write` once an environment has `health` (for incidents) or a stage proposes
- An `[operate]` section with known keys only, `rollback_after` in 1..20 and a non-empty
  `incident_label`
- No `[tool.uv.sources]` entry taken from a local path, which CI and users don't have
- No branch named `shipmill` on origin, which would block the `shipmill/<tag>` work branches
- The `shipmill@shipmill` plugin installs on this host for the repo's folder and its gate
  checkout, as `status` reads them: a WARN with the fix for each one behind the latest
  release (read with `gh`); without `claude` on PATH it says they weren't read

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
   the blocker label, and with an `[agents]` section `needs-decision`). The setup checklist reports what's missing, and `--fix` creates
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
| `/loop 30m /shipmill:github-ship-watch <owner/repo> — watch and triage` | in an open session | the session closes |
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
   prompt = "/shipmill:github-issue-triage {repo} triage the new issues; do not merge"
   prs = false        # true once the prompt says "merge when green"
   retry_hours = 24   # unchanged findings start a new session after this
   notify = true      # a desktop notification when a session waits on you
   remind_hours = 4   # repeat it while the session still waits
   max_wait_minutes = 15 # stop a session that waited this long; 0: never
   # app_id = 123456  # sessions write as this GitHub App; unset: as your gh login
   # mode = "headless" # interactive (default): sessions ask you; headless: `claude -p`, they ask on GitHub
   # plugin_update = true # false (default): the gate changes no plugin install; true: it updates its checkout's, once a day
   ```

   The gate's checkout has a shipmill plugin install of its own, apart from the repo's
   (Claude Code keys a project install on the folder). With `plugin_update = true`, a tick
   about to start a session checks at most once per 24 hours whether that install is behind
   the latest release and, when it is, runs `claude plugin update shipmill@shipmill --scope
   project` in the checkout first; a failed update is printed on the tick and the session
   starts anyway (D-22). Either way `status`, `doctor`, and the setup checklist report an
   install that is behind, in the repo's folder and in the gate's checkout, each with its
   own fix

   Without `app_id`, a session writes as your `gh` login, so its pull requests, merges,
   and commits read as yours. Set it before you schedule the gate, to have them made by a
   GitHub App's bot instead (see
   [Give the sessions their own identity](#give-the-sessions-their-own-identity)): until
   then the setup checklist reads `AGENTS_NO_APP` and github-ship-watch `GATE_NO_APP`,
   both an action for you (D-19)

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
   your answer holds the repo until you give it; on a machine nobody watches,
   [run it headless](#run-it-headless)

### Give the sessions their own identity

Part of setting up the gate (D-19); the gate still runs without one. With a GitHub App, a
session's pull requests, comments, reviews, merges, and commits are made by the App's bot,
`<slug>[bot]`, not by you: you can approve its pull requests, and github-ship-watch's
metrics count its merges as a bot's. The token it uses reaches this one repo only, never
everything your login can.

The quick way is one command:

```bash
$CR app-create --dry-run   # print the plan; create nothing
$CR app-create             # create it: one click on GitHub, then pick the repos
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

For a repo connected later, or in another account:

```bash
$CR app-install                 # the checkout's repo, with [agents] app_id
$CR app-install acme/web beta/x # or name them
```

shipmill never installs the App or adds a repo for you, since which accounts and repos get it
is your choice. For each repo the App doesn't cover yet, it opens the App's **Install App**
page and says per account what to click there: **Install** (then Only select repositories
and the repos) for an account without the App, **Configure** (then add the repos under
Repository access) for one that has it. It then waits until each repo is covered, and exits 1
naming any still missing after 10 minutes.

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

### Run it headless

Optional, for a machine nobody watches. By default a session that asks you something
posts the question on GitHub as the protocol below, then waits for your answer in the
session (D-21); the gate starts nothing else for the repo until you answer or
`max_wait_minutes` stops it, and the item then waits on GitHub. With `mode = "headless"` in `[agents]`, merged like the rest
of the section, each launch runs `claude -p` instead: no permission prompts, no
AskUserQuestion, and a fixed list of tools. It can't wait on you, so it ends, and a
decision for you becomes a comment on the issue or pull request plus the `needs-decision`
label ([the protocol](../skills/github-issue-triage/references/needs-decision.md)). The
comment's first line is `<!-- shipmill:needs-decision -->`, it mentions your `gh` login,
and it gives the options with a recommendation. The item then waits on GitHub, out of the
gate's work, while the rest of the repo keeps moving. Reply on the item (as an owner,
member, or collaborator) and a later tick takes it up again; the session removes the label.
A headless session isn't listed in `claude agents`: follow it with `tail -f` on its log in
`$(git rev-parse --git-common-dir)/shipmill/sessions/`, and `claude --resume <uuid>`
reopens it after it ends.

1. **Create the label.** The setup checklist wants `needs-decision` whenever the config
   has an `[agents]` section, in either mode; `setup_state.py <owner/repo> --fix` creates it

2. **Know the allowlist.** A headless session may use `HEADLESS_TOOLS`, defined in
   `src/shipmill/gate.py`:

   ```
   Read Edit Write Glob Grep Skill Agent SendMessage ListAgents TodoWrite
   Bash(gh *) Bash(git *) Bash(uv *) Bash(uvx *)
   ```

   Any other call is denied without a prompt, and the session posts a `needs-decision`
   comment naming the tool and the command it tried. It is not a sandbox: `git`, `gh`, and
   `uv run` can run code. To widen it, say for a repo whose checks run `npm test`, pass the
   extra tools to the gate (the `=` matters; `--claude-arg --allowedTools` is an argument
   error), and install the job again so it gets them too:

   ```bash
   $CR --repo tmp/shipmill-gate gate <owner/repo> --dry-run --claude-arg=--allowedTools --claude-arg "Bash(npm *)"
   $CR --repo tmp/shipmill-gate launchd <owner/repo> --every 15 --claude-arg=--allowedTools --claude-arg "Bash(npm *)"
   ```

   A headless gate refuses a `--claude-arg` of `--permission-mode`, `--permission-prompts`,
   `--dangerously-skip-permissions`, `--allow-dangerously-skip-permissions`, `--bg`,
   `--background`, or `--session-id` with exit 2: each would bring the prompts back or
   break how the gate tracks the session

3. **Know what it skips.** Headless works only on trusted items (D-16): issues opened by an
   owner, member, or collaborator, or by the App's bot, and pull requests whose branch is
   in the repo rather than a fork. Every other open item reads UNTRUSTED, starts no
   session, and waits for an interactive one; github-ship-watch lists them

4. **Choose how you hear about a question.**
   - **With `app_id`** (recommended): the comment comes from `<slug>[bot]`, so its mention
     notifies you on GitHub like any other. The gate sends no desktop notification
   - **Without it:** the comment is your own, and GitHub doesn't notify you of your own
     mention. The checklist reads `AGENTS_NO_APP` (an action, D-19), and each launch
     prints `no app_id: needs-decision comments post as <login>, so GitHub won't notify
     you`. With `notify = true` the gate sends a desktop notification instead, `#<n>
     waits on your decision: <url>`, at once and every `remind_hours` while the item
     waits, and prints `notified #<n> (waiting <N>h)`

   Either way a tick that reads the state records the waiting items in
   `shipmill/needs-decision.json`, and `gate --json` reports `mode` and a `decisions` list
   (`item`, `since`, `waited_hours`, `notified`, `error`). `mode` is null on a HELD,
   WAITING, or RUNNING tick, which reads no config. `plugin` is the daily plugin check of
   a launch with `plugin_update = true` (`installed`, `latest`, `updated`, `error`), null
   when none ran

## Pause or remove it

| To | Do |
|---|---|
| Pause releases and the gate | open an issue labelled `shipmill-hold`; a hotfix started by hand still runs (D-8, D-15) |
| Stop releases | set `mode = "off"` in `.github/shipmill.toml` |
| Stop the gate | `$CR --repo tmp/shipmill-gate launchd <owner/repo> --remove` |
| Remove the skills | `/plugin uninstall shipmill@shipmill` |
| Remove the CLI | `uv tool uninstall shipmill` |

Everything shipmill knows lives in the repo and on GitHub, so removing it leaves your
tags, releases, issues, and CHANGELOG as they are.

## Clean up old builds

Each uvx run from git can leave a build of shipmill in uv's cache, and each plugin update
leaves the previous version on disk:

- **uv's cache:** `uv cache prune` removes dangling entries and the environments uvx
  cached; `uv cache dir` says where the cache is. A `uv tool install` lives outside the
  cache, so pruning leaves the installed CLI alone
- **The plugin's old versions:** Claude Code keeps each version of the plugin in
  `~/.claude/plugins/cache/shipmill/shipmill/<version>/`. It has no command to prune them:
  when a plugin updates or is uninstalled, it marks the previous version with an
  `.orphaned_at` file and deletes it 14 days later. A version folder that holds
  `.orphaned_at` is safe to delete by hand once no running session started before the
  update; leave the version `~/.claude/plugins/installed_plugins.json` records
