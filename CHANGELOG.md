# Changelog

All notable changes to shipmill, named release-lanes before 0.3.0. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project follows
[Semantic Versioning](https://semver.org/). shipmill releases itself with its own
bot.

## [Unreleased]

### Added

- Spec 010: each stable and hotfix release publishes shipmill to PyPI through a new
  `publish.yml`, with trusted publishing and a smoke test of the built wheel (#216)
- `.github/workflows/publish.yml` builds the release tag it is given, checks that the tag is
  `vX.Y.Z` and matches `pyproject.toml`, runs `shipmill --help` from the wheel in a fresh
  environment and checks the wheel holds the skills' state scripts, then uploads with `uv
  publish` through trusted publishing in the `pypi` environment. The stable and hotfix lanes
  dispatch it, and `docs/install.md` says shipmill is on PyPI and needs Python 3.14 (#224)

### Fixed

- The README, `docs/install.md`, and the github-ship-watch and github-pr-triage skills no
  longer tell you to run a bare `shipmill <command>`, which nothing puts on `PATH`: they
  use `$CR` or the full `uvx --from git+https://github.com/shipmill/shipmill@v0 shipmill`
  form. `docs/install.md` shows an alias for the short name that keeps tracking `@v0`,
  and says why `uv tool install` drifts; the github-ship-watch description, always in an
  agent's context, says how to run the CLI. A test fails on a bare command in the README,
  the top-level docs, or a skill (#215)

## [0.29.0] - 2026-10-07

### Changed

- Decision D-21: every gate session, interactive or headless, asks its questions through
  the needs-decision protocol, so a question nobody answers waits on GitHub instead of
  holding the gate (#206)
- Every `shipmill gate` session asks through the needs-decision protocol, interactive
  too: the interactive prompt ends with a `Gate session:` paragraph naming your `gh`
  login, so the session posts its question as a marked comment with the `needs-decision`
  label before it asks in the session, and an unattended question waits on GitHub instead
  of reading as work. An answer given in the session is posted on the item and the label
  comes off. An interactive launch now reads your login with `gh api user` and starts
  nothing when that fails. With `app_id` set, an interactive gate checks the App before it
  reads the state, as headless does, and reads it with `--bot-login <slug>[bot]`, so a
  reply on GitHub to the bot's question wakes the item. `setup_state.py` wants the
  `needs-decision` label whenever the config has an `[agents]` section (D-21) (#206)
- `shipmill status` prints a short linked summary instead of blocks: a heading with the
  repo, its link, and the verdict, with no list of reasons, since each reason is a line of
  its own; then the repo (in sync, at which version, release ok or the problem), every
  issue and pull request waiting on a decision or listed by state with its own link
  instead of a label search, the open issues and pull requests counted with the list's
  link, the gate as `OK, launchd every N min` or its problem, the mode, the GitHub App
  (`active`, `not active`, or the problem), and shipmill's version (`up to date` or the
  update commands); empty lines are dropped, and `--rows`, `--json`, and the exit code
  are unchanged (spec 009, #212)

### Fixed

- `specs.py` honours a dropped criterion, one whose text starts `dropped in #N`:
  `coverage` prints it as dropped instead of demanding a test, `split` leaves it out of
  new build issues, and `check` needs no Issues line for it, so a built spec that drops
  criteria can pass (#219)

## [0.28.0] - 2026-10-07

### Added

- `shipmill status` says whether the factory works or is stuck: a verdict (STUCK, WAITS ON
  YOU, WORKING, IDLE) with its reasons, then the local `main` against GitHub's and the
  version each is at, the open issues and pull requests (waiting on you with a GitHub link,
  to triage, to build), the gate's job, last run, last decision, mode and App, and one
  shipmill line with the CLI and plugin versions; `--rows` prints the old table (spec 009)

## [0.27.0] - 2026-10-07

### Changed

- A gate without a GitHub App is unfinished setup (D-19): shipmill-setup makes the App step
  required before it schedules the gate, `setup_state.py` reads `AGENTS_NO_APP` for any
  `[agents]` prompt without `app_id`, in either mode, and exits 1 on it (it was headless
  only and counted as done), and github-ship-watch's `watch_state.py`, and so `shipmill
  status`, reports a new `GATE_NO_APP` row, an action for a person that never starts a
  gate session. The gate and `launchd` still run without one (#204)
- shipmill-setup asks for the gate's `[agents] mode` every time, with the App question, and
  writes `mode = "interactive"` or `mode = "headless"` (D-20); `setup_state.py` reads a new
  `AGENTS_NO_MODE` row, exit 1, for a prompt with `app_id` and no `mode` key (after
  `AGENTS_NO_APP`, one row for the section). The config's default stays interactive, and
  shipmill's own config now says `mode = "interactive"` (#205)
- Decisions D-19 and D-20: a gate set up without an App, or without an explicit
  `[agents] mode`, is unfinished setup and reads as an action, never as done (#204, #205)

## [0.26.0] - 2026-10-06

### Added

- `shipmill status` prints github-ship-watch's report for the checkout's repo, as a table or
  with `--json` its JSON lines, and exits 1 when a row needs action, so a person without an
  agent or a CI job can ask "is anything stuck?" (spec 008)

### Changed

- `shipmill gate` reads a crashed `watch_state.py` (exit 1, no rows) as a failure with the end
  of its stderr, where it read it as QUIET
- Spec 008 after a second review: status needs a checkout even with a named repo, shows the
  script's whole stderr and passes it through on success, and the gate shares the guard
  that reads a crashed `watch_state.py` as a failure, not as QUIET
- Spec 008 settled after review: `status` exits 2, not 1, when `watch_state.py` crashes
  without printing a row; `watch_command` gains a `json` parameter for the table; the
  origin and the repo check reuse `_origin_repo` and `check_checkout`

## [0.25.0] - 2026-10-06

### Added

- Spec 008 for `shipmill status`, which prints github-ship-watch's report for the checkout's
  repo and exits 1 when something needs action
- github-ship-watch compares the host's `shipmill@shipmill` plugin (`claude plugin list`)
  with the latest shipmill release: SHIPMILL_VERSION lists the installs that apply to the repo (user scope, the
  checkout, and the gate's working directory) and a marketplace on an old repo name, and
  SHIPMILL_OUTDATED names an install behind the release with the command that updates it.
  The gate's checkout was found pinned at 0.14.0 while 0.24.0 was out (#196)
- github-ship-watch reports BRANCH_DELETE_OFF when the repo's `delete_branch_on_merge` is
  off, with the command that turns it on: merged branches piled up unnoticed after a repo
  move, since only setup checked it. An action for a person, never an agent's (#194)

### Fixed

- github-ship-watch's SHIPMILL_OUTDATED no longer reports a plugin install keyed on a linked
  git worktree, such as the gate's checkout: Claude Code loads the main checkout's install
  there, so the leftover entry was never used and its update command couldn't clear it (#198)

## [0.24.0] - 2026-10-06

### Added

- `[agents] mode = "headless"` for `shipmill gate` (D-17): a launch runs `claude -p` with
  `--permission-prompts none`, the `HEADLESS_TOOLS` allowlist, `--disallowedTools
  AskUserQuestion`, and a new `--session-id`, and its prompt ends with a paragraph that sends
  a decision for your `gh` login to the needs-decision protocol. The session runs detached,
  its output in `sessions/<uuid>.log` under the state directory, and the gate tracks it in
  `gate.json` by pid and process start time: RUNNING while it runs, never WAITING. The state
  read passes `--trusted-only` (D-16), and with `app_id` set `--bot-login <slug>[bot]`; a
  headless gate refuses `--claude-arg` flags that would bring prompts back or break the
  tracking. `mode` defaults to `"interactive"`, which launches exactly as before, and
  `shipmill launchd` now writes `AbandonProcessGroup` (run it once more for an installed
  job) (S-005-1, S-005-2, S-005-3, S-005-5, S-005-6, S-005-11, S-005-19, S-005-20, S-005-21)
  (#162)
- A headless `shipmill gate` without `app_id` notifies you of each item that waits on your
  needs-decision reply, since GitHub doesn't notify you of a comment under your own login:
  on a tick that reads the state, at once and then every `remind_hours`, recorded in
  `needs-decision.json` beside `waiting.json` and under its rules. With `app_id` the bot's
  mention notifies on GitHub and the gate sends nothing; a headless launch without it says
  so. `--json` gains `mode` and a `decisions` list (S-005-12, S-005-13, S-005-14,
  S-005-15) (#163)
- shipmill-setup's checklist knows headless mode: with `[agents] mode = "headless"` it wants
  the `needs-decision` label (`--fix` creates it, color `d876e3`), and without `app_id` the
  agents row reads `AGENTS_NO_APP`, counted as done, saying GitHub won't notify you of a
  question posted as your own login. Interactive and no-`[agents]` setups see no new row,
  and an `[agents]` `mode` no longer reads as the release mode. agent-modes.md, the
  shipmill-setup skill, and install.md document headless mode: `HEADLESS_TOOLS` and how to
  widen it (`--claude-arg=--allowedTools --claude-arg "Bash(npm *)"`), the trust filter, the
  label, and notifications with and without an App (S-005-16, S-005-18) (#164)

### Changed

- Spec 005's S-005-8 counts a `needs-decision` question only from a trusted author: an
  owner, member, or collaborator, or with `--bot-login` the bot's own marker comment. An
  outsider's marker comment can no longer re-park an answered item, and a maintainer's
  question after a bot's triage comment no longer reads as its reply. The code follows in
  #184

### Fixed

- `shipmill launchd` writes each `--claude-arg` into the job as `--claude-arg=<flag>`. It
  wrote `--claude-arg` and the flag as two arguments, which the job's gate refused ("expected
  one argument") on every tick, since a separate value that starts with a dash reads as an
  option. Run `shipmill launchd` once more for a job installed with `--claude-arg` (#192)
- A `needs-decision` question now counts only from a trusted author, in `triage_state.py`
  and the pull requests `watch_state.py` reads: a comment whose first line is the marker,
  by an owner, member, or collaborator, or with `--bot-login` by the bot. An outsider's
  marker comment no longer re-parks an answered item, a bot comment without the marker is
  no question, and with `--bot-login` a maintainer's marker question newer than the bot's
  leaves the item labelled with no question, so it waits instead of reading as answered
  (S-005-8) (#184)
- github-ship-watch no longer reports `WORK_BRANCH_STALE` for a work branch whose run is
  still alive behind ten or more newer runs: with a work branch on origin, it asks GitHub
  for the release workflow's unfinished runs by status (queued, in progress, waiting,
  pending, requested), as `prepare` does, instead of reading the newest ten, and a failed
  query stops the watch (exit 2) rather than reading as no owner. The row's re-check before
  deleting the branch asks by status the same way. A pass with no work branch makes no
  extra calls (#185)

## [0.23.0] - 2026-10-06

### Added

- The needs-decision protocol for headless sessions: a question for the user becomes a
  comment whose first line is `<!-- shipmill:needs-decision -->` and a `needs-decision`
  label, and the item waits on GitHub (`references/needs-decision.md`, linked from a headless
  rule in github-issue-triage, github-issue-resolve, and github-pr-triage).
  `triage_state.py` reads such an issue as NEEDS_DECISION, no action, until an OWNER,
  MEMBER, or COLLABORATOR replies, and gains `--bot-login` and `--trusted-only` (an outside
  author's issue reads UNTRUSTED). `watch_state.py` passes both through, leaves waiting
  issues and pull requests out of ISSUES and PRS_OPEN, and lists them in a NEEDS_DECISION
  row (an action, `agent: false`) and, with `--trusted-only`, outsiders' issues and fork pull
  requests in an UNTRUSTED row, so `shipmill gate` reads a repo whose only work waits as
  QUIET (S-005-7, S-005-8, S-005-9, S-005-10, S-005-17) (#161)
- D-18: a Release run that finds a work branch no active run owns deletes it and pushes its
  own, given `actions: read`; without it, the stop stays but is reported (#175)
- github-ship-watch reports what else is going on in the repo, and every status report lists
  it: the open issues triage owes nothing on yet, by state (ISSUES_OPEN); each queued or
  running workflow run (RUNS_ACTIVE); the gate's triage mode from `[agents]` (TRIAGE_MODE);
  the Claude Code sessions on the host working the repo (AGENT_SESSION); and the gate's
  launchd loop with its last decision (LOOP). All are report-only, so the exit code and the
  gate's fingerprint are unchanged (#182)

### Fixed

- A work branch left on origin by a Release run cancelled before its cleanup job got a
  runner no longer stalls the lane: the next run's `prepare` deletes it and pushes its own
  when no other queued or in-progress run of the Release workflow could own it (D-18). This
  takes `actions: read` on the caller's prepare job, which `shipmill init` now writes; the
  reusable prepare job takes the caller's grant instead of setting its own, so a caller
  without it still runs. Every stop at a held work branch is now visible: a warning
  annotation and a run-summary line naming the branch and its commit, saying to grant
  `actions: read` when that is the cause; the run stays green. github-ship-watch reports
  `WORK_BRANCH_STALE` for a `shipmill/v*` branch no active run owns, and deletes it and
  starts the lane (#175)

## [0.22.0] - 2026-10-06

### Changed

- `docs/design/agent-modes.md` records the #160 probe of a headless `claude -p` session on
  Claude Code 2.1.291: it outlives the launchd job that started it, a call outside the
  allowlist is denied with no prompt, AskUserQuestion is absent, and it exits on its own
  even when its last reply asks a question (S-005-4)

## [0.21.0] - 2026-10-06

### Changed

- github-issue-triage asks a design question on the issue too, as a **Decision needed**
  block at the top of its verdict comment: the question in one plain sentence, the options
  numbered with the recommended one first and what each means for users, and how to answer
  (reply with a number). A reply on the issue counts as the answer, so the maintainer
  doesn't have to attach to the gate session; a held PR's decisions are asked the same way
- Spec 005 now runs headless gate sessions as `claude -p` instead of `claude --bg`, after
  the #160 probe found a `--bg` session reads `blocked` whenever its last reply asks for
  something: the gate starts the session detached, logs its output under the state
  directory, tracks it by pid, and never reads it as waiting; `shipmill launchd` sets
  `AbandonProcessGroup` so a session outlives its tick (D-17)

## [0.20.0] - 2026-10-06

### Added

- `shipmill app-install [owner/name ...]` guides installing the gate's App on more repos:
  for each repo the App doesn't cover it opens the App's Install App page and says per
  account whether to click Install or Configure and which repos to pick, then waits until
  each is covered; it never installs or adds anything itself. `shipmill app-create`'s
  install step uses the same guide (#172)
- Spec 007 for `shipmill app-install`, which guides installing the gate's App on more repos
  and accounts, and checks that it took

### Changed

- shipmill's own gated sessions write as the `shipmill-romamo` GitHub App (`[agents] app_id`)
- `shipmill app-create` makes the App yours by default (your personal account), private
  unless a gated repo is in an org, then public so it installs there; an interactive run
  asks who should own it, your account first, and `--owner` picks an org you administer
  (#167)
- `shipmill app-create` names the App `shipmill-<owner>` by default (`shipmill-agent` under
  the `shipmill` account), falling back to `shipmill-<login>`, so each account's bot is told
  apart on its pull requests; when both are taken it exits 2 naming free alternatives

### Fixed

- `shipmill app-create` sets the App's homepage to `https://github.com/shipmill/shipmill`,
  not the first gated repo's URL

## [0.19.0] - 2026-10-06

### Added

- Spec 006 for `shipmill app-create`, which makes the gate's GitHub App in one click and
  picks its owner and visibility from where the gated repos are
- `shipmill app-create` makes the gate's GitHub App in one click: it finds the accounts your
  `gh` login administers and the repos in them holding `.github/shipmill.toml`, plans a
  private App when they are all in one account and a public one when they span several
  (`--owner`, `--public`, `--private`, `--name`, `--repos` change it; `--dry-run` prints the
  plan), opens GitHub's create page with the permissions filled in, saves the key to
  `~/.config/shipmill/app-<app_id>.pem` with mode `0600`, waits for the installations, and
  prints the `app_id` line to commit; exit 1 names a repo still missing the App. The setup
  skill and `docs/install.md` use it first, the manual steps as the fallback (#155)
- Spec 005 for headless gate sessions: `[agents] mode = "headless"` starts sessions that
  can't prompt, a decision for the user becomes a `needs-decision` comment and label the
  gate waits on, and headless work is limited to trusted authors' items, with D-16 (#134)
- Spec 005's build issues: #160 to #164 (#134)

### Fixed

- D-15's `Decided` line names the issue it was decided in (#147), so `specs.py check`
  accepts a spec that cites it
- `shipmill app-create` prints each line as it happens, so a piped or backgrounded run shows
  the plan and the URL at once; its local page no longer posts to GitHub by itself, which
  lost the manifest when GitHub asked to sign in first (*We didn't find an App Manifest*),
  but shows the plan and each permission with its use and a **Create on GitHub** button to
  click again after signing in; and a taken default name moves to a free variant, while a
  taken `--name` exits 2 naming the free ones (#157)

## [0.18.0] - 2026-10-06

### Added

- `shipmill gate` prunes on each tick as `shipmill worktrees --prune` does, held or not:
  after the waiting step and before the hold check, it removes each worktree that provably
  landed with the local branch it held, prints `pruned <path> (<branch>)` for each, and
  lists their paths under a new `pruned` key in `--json`. `--dry-run` removes nothing,
  prints `would prune <path> (<branch>)`, and lists them under `pruned`. A prune error
  (`claude agents --json` or `gh pr list` failing, or git refusing a removal) exits 2 and
  starts no session; the gate's own checkout is never a candidate (#124)
- Spec 004's build issues: #141 to #144 (#136)
- `[agents] app_id` names the GitHub App gated sessions write as, and `shipmill gate
  --app-key <path>` its private key (default `~/.config/shipmill/app-<app_id>.pem`). On a
  launch with `app_id` set, the gate checks the key's mode, signs the App's JWT with
  `openssl`, and checks the App is installed on the repo with every permission spec 004
  lists, exiting 2 naming what failed (D-14) (#141)
- github-ship-watch's `watch_state.py` reports a `WORKTREE_STALE` row, report-only
  (`agent: false`, never an action), for each worktree under `.claude/worktrees/` or
  `tmp/wt-*` that `shipmill worktrees` keeps and that is over 7 days old, naming its path
  and why it is kept. It runs `shipmill --repo <repo-dir> worktrees --json` for a shipmill
  bot, so the watch needs `claude` on `PATH` there, and a failing command exits 2 as a
  failing plan does. shipmill-setup's gate section documents the prune and `shipmill
  worktrees`, and github-issue-resolve, github-pr-triage, and github-issue-triage say that
  the worktrees a session leaves behind once it exits are the gate's prune's to remove.
  Spec 002 is built (#125)
- `shipmill app-token <owner/repo> --app-id <id> [--app-key <path>]` prints a GitHub App
  installation token limited to that one repository and spec 004's permissions. It caches
  the token in `$(git rev-parse --git-common-dir)/shipmill/app-token.json` (mode `0600`,
  replaced atomically), reuses it while it has at least 10 minutes left, and exits 2 on a
  malformed cache, naming it. `--git-credential get` answers git's credential protocol for
  `https://github.com` with `username=x-access-token`; another host gets no answer, and
  `store`, `erase`, and other operations print nothing. `python -m shipmill` now runs the
  CLI, and the session's `gh` and `git-credential-shipmill` helpers, which hold no token,
  can be written for the gate's launch (#142)
- With `[agents] app_id` set, `shipmill gate` starts its session as the App's bot. After
  the App's checks it reads the bot account's id, writes the `gh` and git credential
  helpers to `$(git rev-parse --git-common-dir)/shipmill/bin/`, and launches `claude --bg`
  with `--settings` env that puts that folder first on `PATH`, makes `<slug>[bot]` and
  `<id>+<slug>[bot]@users.noreply.github.com` the git author and committer, and, through
  `GIT_CONFIG_*`, replaces the host's github.com credential helpers with the App's and
  sends `git@github.com:` and `ssh://git@github.com/` remotes over https. The env holds no
  token, and the gate's own reads keep the host's `gh` login. Any failure exits 2 before a
  session is stopped or started. The decision line ends ` as <slug>[bot]`, `--json` gains
  `identity` (`null` without an App), and `--dry-run` runs the checks, writes nothing, and
  prints `would launch as <slug>[bot]`. With `app_id` unset the launch is unchanged.
  `shipmill launchd --app-key <path>` passes the key, made absolute, to the job's gate,
  and refuses it when `app_id` is unset or the key is missing or readable by others (#143)
- shipmill-setup's gate section and `docs/install.md` give the GitHub App's setup: creating
  it with no webhook and spec 004's permission table, installing it only on the repos the
  gate works on, the private key at `~/.config/shipmill/app-<app_id>.pem` with `chmod 600`
  (or `--app-key` on `gate` and `launchd`), `app_id` in `[agents]`, the token cache and
  helpers, the dry run that proves it, and that without `app_id` sessions launch as before.
  `docs/design/agent-modes.md` describes the session's identity, `--settings` env, and the
  `app-token.json` and `bin/` state, and github-ship-watch says the App's `<slug>[bot]`
  counts as a bot in the metrics. Spec 004 is built (#144)

### Changed

- `[agents] max_wait_hours` is now `max_wait_minutes` (0..10080) and defaults to 15, so
  `shipmill gate` stops a session that has waited on you for 15 minutes unless the config
  says otherwise (D-15, superseding D-13); 0 still never stops one. A config that still
  sets `max_wait_hours` is refused naming the key. Waits read in minutes under an hour
  (`stopped <id> after 15m waiting`), and `--json` reports `waited_minutes` in place of
  `waited_hours` (#147)

## [0.17.0] - 2026-10-06

### Added

- Spec 003 for notifying when a gated session waits on you, and stopping it after an
  optional `max_wait_hours`, with D-13: a hold doesn't keep the gate from stopping a stuck
  session (#119)
- Spec 003's build issues: #128 to #130 (#119)
- The `[agents]` section takes `notify` (default true), `remind_hours` (1..168, default 4),
  and `max_wait_hours` (0..168, default 0), refusing a bad value naming the key; the gate
  doesn't act on them yet (#128)
- `shipmill gate` sends a desktop notification when a session it started waits on you
  (`osascript` on macOS, `notify-send` elsewhere), and again every `remind_hours` while it
  waits; `notify = false` turns it off. The wait is kept in `waiting.json` next to
  `gate.json`, a failed send prints `notify failed for <id>: <error>` without changing the
  decision, notifying runs on held ticks too, `--dry-run` prints `would notify <id>`, and
  `--json` lists each blocked session under `waiting`. A tick with a blocked session now
  reads the `[agents]` section (#129)
- `shipmill gate` stops a session that has waited on you for `max_wait_hours` (when not 0)
  with `claude stop`, prints `stopped <id> after <N>h waiting: <name>`, sends a last
  notification, and decides the rest of the tick without it, held or not (D-13); a failed
  `claude stop` exits 2 and launches nothing, and `--dry-run` prints `would stop <id>`.
  Spec 003 is built (#130)
- `shipmill worktrees [--json]` lists every worktree of the repository as REMOVABLE, once
  its branch provably landed on the default branch (by ancestry, patch id, or a squash of
  its whole diff), or KEPT with the first check it fails: the main or current checkout, not
  under `.claude/worktrees/` or `tmp/wt-*`, missing, locked, detached, on the default
  branch, under a day old, a live Claude Code session in it, another worktree inside it,
  uncommitted changes, commits not landed, or an open pull request (#122)
- `shipmill worktrees --prune` removes each REMOVABLE worktree with `git worktree remove`
  (never `--force`) and then the local branch it held, printing those rows as REMOVED (also
  the `verdict` in `--json`); no other worktree or branch, local or remote, is touched.
  `--prune --dry-run` prints them as WOULD_REMOVE and removes nothing, and `--dry-run` alone
  exits 2. The first removal or branch deletion git refuses stops the prune with exit 2,
  naming the worktree and git's error and what it removed before (#123)
- Spec 004 for gated sessions writing as a GitHub App (`[agents] app_id`), with D-14:
  with an App set, a session starts as the App or not at all (#137)

## [0.16.0] - 2026-10-05

### Added

- shipmill-setup's `setup_state.py` reports whether GitHub deletes a pull request's branch
  when it merges: `BRANCH_DELETE_ON`, or `BRANCH_DELETE_OFF` (exit 1), which `--fix` turns
  on through the repo setting `delete_branch_on_merge`. github-pr-triage's stacked merges
  rely on it to retarget a stacked PR. github-issue-triage now deletes the branch of a PR
  it closes because the new plan dropped it (#117)
- Spec 002 for pruning landed worktrees, and D-12: whoever creates a worktree removes it,
  and `shipmill gate`'s prune owns what an exited session left (#116)
- Spec 002's build issues: #122 to #125 (#116)

### Changed

- shipmill's own `.github/shipmill.toml` has an `[agents]` section, so `shipmill gate` triages
  this repo's new issues and lands its green pull requests

## [0.15.0] - 2026-10-05

### Added

- `docs/install.md`: the full installation, from prerequisites through the skills, release
  lanes, and the gate, to pausing and removing shipmill. The README keeps a short Install
  section and links to it; its Quick start and Where the agents run sections moved there
- A one-line setup: `claude plugin marketplace add shipmill/shipmill && claude plugin install
  shipmill@shipmill && claude "/shipmill:shipmill-setup"`, from the root of a repo, installs
  the plugin and starts the shipmill-setup skill
- `watch_state.py --json` marks every row with `agent: true|false`: true for BOT_FAILED,
  BOT_STALLED, NOT_PUBLISHED, UNANNOUNCED, ISSUES, OPERATE_FAILED, and INCIDENT_OPEN, the
  rows an agent works on; false for every other state, PROMOTION_DUE, UNHEALTHY, HOLD, and
  PRS_OPEN among them. The text table is unchanged, and `fleet.py` reads the new field
  without adding it to its own report (#49)

### Changed

- `shipmill gate` starts a session on the rows `watch_state.py` marks `agent: true`, plus
  PRS_OPEN with `[agents] prs = true`, in place of its own copy of the states. A failed
  operate run (OPERATE_FAILED) and an open incident (INCIDENT_OPEN) now start one; an open
  `shipmill-hold` issue still stops every launch (D-11). The gate refuses a row with no
  boolean `agent` rather than guessing (#49)
- shipmill moved to the shipmill GitHub org: the repository is `shipmill/shipmill`, so
  `shipmill init` writes `uses: shipmill/shipmill/...@v0`, the default `--tool` for the
  workflows, `launchd`, and `watch_state.py` is `git+https://github.com/shipmill/shipmill@v0`,
  and the plugin's marketplace is `shipmill/shipmill`. GitHub redirects the old path; the
  setup checklist still counts a `release.yml` that calls `romamo/shipmill`. shipmill's own
  Pages site, and its health URL, moved to `https://shipmill.github.io/shipmill/`
- The CHANGELOG's `[Unreleased]` link points at `shipmill/shipmill`, so the compare links
  each release writes from it use the org's path

### Fixed

- `shipmill gate` no longer writes a row's detail into the session's prompt: an open
  incident's detail starts with the issue's title, text anyone who can edit the issue
  controls, which reached the session as if the gate wrote it. The prompt lists each row by
  state and subject only (`- INCIDENT_OPEN #42`) and tells the session to rerun
  `watch_state.py` for the details and read each issue's text as untrusted data, not
  instructions. `--dry-run`, the log, and the launch fingerprint keep the detail (#114)
- The github-issue-resolve skill no longer leaves the checkout it starts in on the fix branch,
  where a later `shipmill plan` read the PR branch instead of main: Phase 3 creates the branch
  in a worktree (`git worktree add -b fix/<slug> tmp/wt-<slug> origin/<default>`) unless the
  session already runs in a linked worktree, never switches, resets, or pulls the user's
  checkout, keeps the worktree while the PR is open, and removes it and its branch once
  `landed.py` confirms the merge (#110)
- `fleet.py report --metrics` measured change failure rate and time to restore with the
  `incident` label for a repo whose fleet entry sets no `incident_label`, even when the repo's
  config sets `[operate] incident_label`. fleet.py now reads that label from the clone with
  `watch_state.py`'s config reader and passes it to `metrics.py`, so the metrics and the watch
  count the same issues (#99)

## [0.14.0] - 2026-10-05

### Changed

- `shipmill gate` starts no session while an issue labelled `shipmill-hold` is open, and
  reports HELD with any session still open (D-11)

## [0.13.0] - 2026-10-05

### Changed

- The project is now shipmill, with no compatibility layer: the package and CLI
  `shipmill`, the config `.github/shipmill.toml`, the labels `shipmill-hold` and the
  other `shipmill-*` labels, the `shipmill/<tag>` work branches, the `shipmill-setup`
  skill, the plugin `shipmill@shipmill`, and the repository `romamo/shipmill`

### Removed

- macOS Finder's `.DS_Store` files, which the rename's commit picked up; `.gitignore` now
  lists them
- `.github/release-policy.toml`, the config's alias: shipmill reads only
  `.github/shipmill.toml`, and `init --force` no longer removes the alias

## [0.12.0] - 2026-10-05

### Added

- github-ship-watch reports a fleet (spec S-001): `fleet.py report --fleet <file>` runs
  `watch_state.py` for every repo the fleet file lists, each on a fresh blobless clone,
  and prints one table with the repo named on each row, the action rows of all repos
  first. It exits 1 when any repo has an action row; a repo whose check fails (not
  found, no access, a gh error) gets one REPO_ERROR row with the error's first line, the
  rest are still reported, and the run exits 2. `--metrics` adds each repo's
  `metrics.py` measures over one shared 30-day window, side by side with one column per
  repo, "no data" kept as no data. `--json` prints the report as one object, each repo
  with its rows (and its metrics with `--metrics`). The skill's new Fleet section
  documents the fleet file, the report, and a routine prompt that runs it. The fleet
  file is `[[repos]]` tables naming each repo as `owner/name`, with an optional
  `incident_label`, refused (exit 2, naming the file and the problem) when it is
  missing, isn't TOML, has no repos, has an entry without `repo` or with an unknown key,
  names a repo not in owner/name form, or lists a repo twice.
  `watch_state.py --incident-label` takes the place of the config's
  `[operate] incident_label`, so a fleet entry's label is the one its watch reads (#91)

## [0.11.0] - 2026-10-04

### Added

- A merged spec becomes a task graph: `specs.py split NNN` proposes its build issues,
  one per `--group` of criteria, each with a title, a body naming its `S-NNN-k`
  criteria, and `Depends on` lines (`--after B:A`) to fill in as the issues get numbers
  (`--json` for the same as one object). `specs.py check` reads the Issues section as
  `- owner/repo#N: S-NNN-1, S-NNN-2` and holds every criterion of an approved or built
  spec to exactly one build issue there. `triage_state.py` reads a `Depends on
  owner/repo#N` (or same-repo `#N`) line in an issue's body as a hold: BLOCKED while the
  dependency is open, UNBLOCKED once it closes, and UNFILLED (an action state) while a
  `#{Bk}` placeholder from `split` is left unfilled; `--wip N` reports the room left
  under a WIP limit. The triage skill files the build issues as sub-issues of the
  feature issue, dispatches them in dependency order within `[roadmap] wip` once the
  config has it (#70), stacks a
  dependent PR on its dependency's open one, and verifies the whole spec when the last
  build issue closes (#71)
- github-ship-watch learns from failures. `watch_state.py` reports POSTMORTEM_DUE (an
  action) for an issue labelled `[operate] incident_label` (default `incident`) closed as
  completed, not as not planned or a duplicate, that no `docs/postmortems/*.md` on the
  default branch names in an `Incident:` line (`owner/repo#N`, the issue's URL, or `#N`),
  read through the GitHub contents API. The skill drafts the postmortem from the
  incident's comments and deployment statuses as a pull request the maintainer merges,
  from the new `docs/postmortems/TEMPLATE.md` (timeline, cause, what caught it, what would
  have caught it sooner, actions); after the merge each rule becomes a `D-n` entry through
  `decisions.py` and each other action an issue. A weekly retro, run only when the user
  schedules it, compares this week's metrics with last week's, reads the refused and
  reworked PRs, recurring review findings, and stuck states, and posts a short comment on
  the roadmap issue with at most three proposals, each opened as an issue. The new
  `scripts/retro.py` gathers the retro (`gather`) and checks proposal titles against the
  open issues (`dedupe`), and `metrics.py --until` ends the window at a date or time, so
  the week before can be measured (#72)
- A `product-intake` skill groups product feedback (open issues triage left to it, open
  discussions when Discussions are on, and their thumbs-up reactions) into one
  `opportunity` issue per outcome, with the problem, who is affected, the evidence, and
  what success looks like. The maintainer accepts one with the `planned` label or declines
  it by closing it as not planned with the reason in a comment; a later request for a
  declined outcome is linked into that opportunity's Evidence, never proposed again, and an
  accepted one goes to github-issue-triage's spec gate as a feature. Its
  `scripts/intake_state.py` reports NEW_FEEDBACK, OPPORTUNITY_OPEN, ACCEPTED, HANDED_OFF,
  DECLINED, NO_REASON, DONE, MERGED, and OVERLAP; an issue labelled `bug`, `roadmap`, or
  one of shipmill's own labels isn't feedback, and `--skip-label` adds more. `[autonomy] intake` sets how far it
  goes: `propose` (the default) or `observe`; `act` is refused, and `doctor` prints it (#69)
- The `product-intake` skill plans the roadmap: milestones are the roadmap, and it
  proposes the next one from accepted opportunities, ranked by evidence (requests and
  thumbs-up) per load (the opportunity plus its spec's build issues), within a new
  `[roadmap]` config section: `wip`, the issues open at once across the open milestones
  (default 5), and `cadence`, the weeks between milestone due dates (default 2). The
  proposal is a `milestone-proposal` issue the maintainer approves by closing it with an
  "approve" comment. Its `scripts/roadmap_state.py` reports MILESTONE, OVERDUE, WIP_OVER,
  UNPLANNED, PROPOSAL_OPEN, APPROVED, UNREADABLE, and NEXT. `doctor` validates
  `[roadmap]` and prints it when present (#70)
- A deploy gets its first health check right after it succeeds, not at the next scheduled
  `shipmill operate` run up to 10 minutes later: the deploy workflow in the README and the
  shipmill-setup skill ends with an `operate` job that runs `gh workflow run operate.yml -f
  dry-run=false` with `actions: write`, and the 10-minute schedule stays the backstop.
  `doctor` notes when an environment's deploy workflow starts the operate caller, and warns
  when it doesn't for an environment with `from` or `health` (#84)

### Changed

- github-issue-triage sends a user's request for a new capability to product intake
  instead of the spec gate: the new **opportunity** verdict (`Triage: **opportunity**`,
  the marker intake uses) hands it over, `triage_state.py` reads it TRIAGED and
  `intake_state.py` reads it NEW_FEEDBACK until an opportunity groups it. A spec is
  written only for an accepted opportunity (label `planned`), a request one already
  covers (held on that opportunity), or an issue the maintainer filed; bugs, contract
  tweaks, and small additive features keep their verdicts. The rubric, the comment
  templates, the spec gate, github-issue-resolve, product-intake, and `docs/flow.md`
  say so, and tests hold both scripts to the new templates (#78)

### Fixed

- `changelog_guard.py move` no longer leaves an entry headless when a union kept the
  PR's `###` heading line above the other side's lines: an entry right under a release
  heading goes under the nearest `###` heading above it, joining that heading in
  Unreleased or creating it in Keep a Changelog order, and `move` refuses a bullet with
  no `###` heading anywhere above it instead of moving it as a block (#82)
- `triage_state.py` no longer reads an incident closed as completed by hand as
  SUSPECT_CLOSE: an incident closes once the environment is healthy again, and
  github-ship-watch's POSTMORTEM_DUE follows it up. The label is `incident` by default;
  the script can't read the shipmill config, so pass `[operate] incident_label` with the
  new `--incident-label` when it differs (#83)
- A deploy workflow started by hand without `--ref <tag>` recorded its deployment under the
  branch, which names no release tag, so `shipmill operate` couldn't tell what ran there.
  The deploy workflow in the README and the shipmill-setup skill now starts with a `ref`
  job that fails such a run, with the right `gh workflow run ... --ref <tag>` command, and
  `doctor` warns when an environment's deploy workflow has no step comparing `github.ref`
  with `refs/tags/<tag>` (#80)

## [0.10.1] - 2026-10-04

### Fixed

- Spec and metrics follow-ups: `specs.py check` and `coverage` report "no specs" and
  exit 0 when `docs/specs/` doesn't exist yet, and `check` refuses the template's
  placeholder text left in an `approved` spec (Problem through Out of scope) or a
  `built` one (any section); `triage_state.py` reads a plain `#N` right after a hold
  phrase ("waits on #N") as the same repo; `metrics.py` pages an issue's comments back to its first "Released
  in" notice, so a notice from before the window keeps a later one out of issue to
  release, and its docstring and SKILL.md name the backport shape lead time misreads
  (#68)

## [0.10.0] - 2026-10-04

### Added

- `shipmill operate` rolls back an environment that fails `[operate] rollback_after`
  health checks in a row (default 3) to the previous tag that reached `success` there,
  under `rollback` autonomy and the hold: act dispatches, propose (or the hold) has the
  incident propose it for `operate --approve-rollback <environment>`, observe reports.
  It opens one issue per environment and bad tag, labelled `[operate] incident_label`
  (default `incident`), with the failing check and the rollback, and comments on it
  when the environment is healthy again or when the rollback fails its checks too, where
  it stops. An open incident holds the `[gates] blocker_lanes` like the blocker label.
  A failed check now writes a `failure` status each time, up to `rollback_after` in a
  row, to count them. `doctor` validates `[operate]` and wants `issues: write` on the
  operate job once an environment has `health` (#31)
- github-ship-watch reports what `shipmill operate` found, for a repo whose config has
  `[environments.<name>]` tables: OPERATE_FAILED (the operate caller's latest run failed;
  the watch reruns a flaky one once), UNHEALTHY (an environment's newest `shipmill health`
  status is a failure), PROMOTION_DUE (an open "Ready to promote" proposal, with its
  approve command, or a promotion operate would make or propose if it ran on a schedule),
  and INCIDENT_OPEN (an open issue labelled `[operate] incident_label`, default
  `incident`, with its age and the PRs linked to close it). Every repo with a shipmill
  config also gets a HOLD row for an open `shipmill-hold` issue, with who opened it and
  when; a hold alone doesn't make the watch exit 1. Incidents and holds lead the report,
  and the watch never deploys or rolls back itself (#33)
- Under `release = "propose"`, the land workflow closes the lane's "Ready to release"
  issue once the release is tagged, with a comment naming the tag and the run, and says so
  when the issue proposed another version. A new `close-proposal` job takes the caller's
  grant: add `issues: write` to the land job in `.github/workflows/release.yml` (`shipmill
  init` writes it, and `doctor` warns under `release = "propose"` until it is there).
  `shipmill operate` closes a "Ready to promote" or "Ready to deploy" issue once the
  environment runs its tag or a later one, whoever deployed it (#40)
- github-ship-watch's `metrics.py` measures a repo over a window (`--days 30` by
  default) from GitHub alone: deploy frequency (successful deployments per environment,
  or stable releases for a repo without deployments), lead time for changes (a merged
  PR's first commit to the first stable release that shipped its merge commit, median
  and p90), change failure rate (incidents per deploy, or `release-blocker` issues and
  hotfix releases per release), time to restore (an incident to operate's "healthy
  again" comment or its close), issue to release (an issue to its "Released in" notice),
  human touch (merges a person merged or approved; an agent merging with a person's
  token counts as the person), and agent share (merged PRs whose body carries Claude
  Code's footer or whose commits carry a `Co-Authored-By: Claude` or `Claude-Session:`
  trailer, and how many of those a person approved in a review). An empty measure reads
  "no data", never 0. A table by default, `--json`, and `--markdown` for a weekly post on
  the roadmap issue, which the user schedules (#56)
- A spec gate in github-issue-triage: an issue asking for new behaviour gets the verdict
  **feature** and a spec file `docs/specs/NNN-<slug>.md` (template in
  `docs/specs/TEMPLATE.md`) with numbered acceptance criteria `S-NNN-k`, merged through
  its own PR before any implementer starts. `specs.py` writes the next spec (`new`),
  validates them (`check`), lists the specs a path or area touches (`find`), and prints a
  spec's criteria for the implementer brief (`criteria`). `triage_state.py` reads a hold
  on a pull request of the same repo: BLOCKED while it is open, UNBLOCKED once it
  merges, and the new action state SPEC_REFUSED (exit 1) once it closes unmerged, until
  a newer triage comment decides again (#54)
- Acceptance tests from specs: a test proves a spec's criterion by naming it,
  `test_s007_2_...` (Go `TestS007_2...`) or a `proves: S-007-2` comment line, and
  `specs.py coverage [--spec NNN]` lists each criterion of the built specs with the
  Python, JS/TS, Go, and Rust tests that prove it, exiting 1 when one has none. A spec
  carries `status: draft|approved|built` and a Verification section; `check` refuses a
  built spec that lists no issues or leaves a criterion out of Verification. CI runs
  `check` and `coverage`, and the reviewer brief checks that each listed test really
  asserts its criterion (#55)

### Fixed

- The triage skills keep an implementer's or reviewer's worktree until its PR merges, not
  just until it's pushed: a revision goes back to the same agent through SendMessage, and
  an agent whose worktree was removed can't be resumed
- `doctor` accepts the repository hosting shipmill calling its own `operate.yml` locally:
  there `.github/workflows/operate.yml` is the reusable workflow, so the operate check
  reads the workflow that `uses: ./.github/workflows/operate.yml` instead. A blank line no
  longer hides what follows it: a later job's `environment:`, or a workflow input (#53)
- `shipmill propose` and `shipmill operate` no longer open a duplicate proposal issue in
  a repo with more than 500 open issues: every release or deploy proposal carries a
  `shipmill-proposal` label, created on first use, and is found among the open issues with
  it (refusing, rather than guessing, at 100 of them). A proposal opened before the label
  is found once by the old scan and labelled on that update.
  github-ship-watch finds proposals by the label too, falling back to its title search
  while none has it (#41)
- A config error for an unknown key in `[lanes]`, `[bump]` or `[autonomy]` lists the allowed
  keys as the values the file accepts (`'dev', 'hotfix', 'rc', 'stable'`), not as enum
  reprs like `<Lane.DEV: 'dev'>` (#59)
- Proposal follow-ups: `land` leaves open a release proposal for a later version than the
  one it released, as `operate` does for a deploy proposal; github-ship-watch always runs
  its title search beside the label lookup, so an unlabelled deploy proposal shows while a
  labelled one exists; and a `shipmill-proposal` label another run created meanwhile no
  longer fails the run (#65)

## [0.9.0] - 2026-10-04

### Added

- `shipmill gate <owner/repo>` starts a Claude Code background session (`claude --bg`)
  for a repo only when its state needs one: it reads the state with code, skips while a
  session it started is still working or waits on you, skips findings unchanged since
  the last launch, and passes the findings in the prompt. The prompt, whether open PRs
  count as work, and the retry window come from a new `[agents]` section of
  `.github/shipmill.toml`. `--refresh` moves a dedicated, detached checkout to the
  default branch first. The wheel now bundles the skills
- `shipmill launchd <owner/repo>` runs the gate from launchd every few minutes on a
  Mac, with a PATH built from where claude, gh, git, and uvx live outside temporary
  folders. Each pass is a new session, and a quiet tick makes no model call
- shipmill-setup connects the agent side: `setup_state.py` reports releases, the
  `[agents]` section, the plugin in `.claude/settings.json`, and the labels the skills
  read, and `--fix` enables the plugin and creates the labels. The gate on launchd is a
  new choice for where agents run
- `shipmill operate`, run every 10 minutes by the new reusable `operate.yml` through a
  caller that `shipmill init --operate` writes: for each environment it reads the current
  deployment from GitHub, checks its `health` URL (2xx within 10 s; a JSON body's `version`
  must name the deployed release), and records the result as deployment statuses, written
  only when the state changes. It promotes a `from` environment once its source has been
  healthy on a tag for `bake_minutes`, and redeploys a `lane` environment that missed its
  lane's newest tag, each at most once per tag (a deployment of it or a run of the
  workflow on it counts) and under `deploy.<environment>` autonomy:
  act dispatches, propose (or the hold) opens a "Ready to promote" issue that `operate
  --approve <environment>` deploys once, observe reports. `land` now starts environment
  workflows on the tag, so each deployment's ref names it. `doctor` checks the operate
  caller and its permissions once an environment uses `from` or `health` (#30)

## [0.8.0] - 2026-10-04

### Added

- Environments: an `[environments.<name>]` table in `.github/shipmill.toml` names a deploy
  `workflow` and exactly one of `lane` or `from`, plus an optional `health` URL and, with
  `from`, `bake_minutes`; a `from` chain must end at a `lane` environment. `land` starts the
  workflow of each environment whose `lane` is the release's, with `tag` and `environment`
  inputs, and lists it as `deploy.yml@staging`; `from` environments wait for promotion.
  `doctor` checks each workflow takes both inputs and that a job sets `environment:`, so
  GitHub records a deployment; `init` writes a commented example (#29)
- `[autonomy]` in `.github/shipmill.toml` sets each stage to `observe`, `propose`, or `act`
  (`release`, `deploy.<environment>`, and `rollback`, all `act` by default), and an open
  issue labelled `shipmill-hold` turns every `act` into `propose`. Under `release =
  "observe"` a due lane is only reported; under `"propose"`, or while a hold is open, the
  new `propose` job of `prepare.yml` opens or updates one "Ready to release vX on <lane>"
  issue per due lane instead of releasing. A lane started by hand still releases under
  propose; under a hold only a hotfix started by hand does. `doctor` prints the effective
  autonomy per stage and warns on an open hold. Proposing needs `issues: write` on the
  prepare job of the calling workflow, which `shipmill init` now writes; `doctor` warns
  about it only once a stage is set to propose or a hold is open. Each `deploy.<name>` must
  name an environment in `[environments]`. Deploy and rollback autonomy take effect once
  shipmill deploys (#32)

## [0.7.0] - 2026-10-04

### Fixed

- `changelog_guard.py move` carries each entry a rebase dropped into a released section
  under its own `###` heading in Unreleased, creating the heading in Keep a Changelog
  order, and leaves the released section as the base has it; it no longer leaves a bullet
  with no heading. `check` also fails on such a headless bullet and on a duplicate heading
  under Unreleased, and `union` keeps a blank line between a release heading and the
  heading it meets. The landing loop runs `move`, `check`, and the planner's dry run after
  every rebase, and prints the Unreleased section for the lander to read (#25)
- `triage_state.py` reads a busy repo such as pypa/pip: a page GitHub rejects for its
  resource limits is asked again at half the size, down to 10 issues, and one still
  rejected there fails with one error line instead of gh's line per node (#24)

### Added

- `.github/shipmill.toml`, the one config file every shipmill layer will read: `shipmill
  init` writes it, and `doctor` names the file it read. `.github/release-policy.toml` keeps
  working as an alias with the same keys, and `doctor` warns to rename it with `git mv`; a
  repo with both files fails. `init` refuses when either file exists, and `--force` replaces
  the alias with `shipmill.toml`. ship-watch finds either file. Rename to `shipmill.toml`
  only once the shipmill CLI your Release workflow runs (the `tool` input of `prepare.yml`
  and `land.yml`, default `@v0`) is this release or newer (#28)

## [0.6.0] - 2026-10-04

### Added

- "Where the agents run" in the README, and a last hand-over step in `shipmill-setup`
  that asks whether the skills run on demand, on a `/loop`, or as a cloud `/schedule`
  routine, and whether a routine may merge

### Changed

- The `release-lanes-setup` skill is now `shipmill-setup`: `/shipmill-setup` (or
  `/shipmill:shipmill-setup` from the plugin) replaces `/release-lanes-setup`. A checkout
  linked into `~/.agents/skills` re-runs the link loop from the README and removes the
  dangling `release-lanes-setup` link. "Set up release lanes" and "add a release bot"
  still find it

### Fixed

- The design gate finds an open decisions PR by the log file it changes, not by
  `DECISIONS` in its title, which missed a PR titled "Record D-1 and D-2"
- `triage_state.py` pages past the caps of its GitHub query instead of reading a capped
  list as complete: open issues beyond the newest 100, an issue's comments beyond the
  newest 50 (an older verdict or "On hold" line), cross-references beyond the oldest 50
  (a later linked PR), and tags back to the newest stable one. An issue with more than 100
  labels exits 2 as bad input. A repo under the caps still takes one query (#19)

## [0.5.2] - 2026-10-04

### Fixed

- `doctor`'s `work branch` check asks origin for a `shipmill` branch, as `prepare` does,
  instead of reading the clone's fetched refs: a shallow or single-branch clone (such as a
  CI checkout) passed while origin had the branch, and a stale `origin/shipmill` failed
  after it was deleted. It warns when there is no origin or origin can't be reached

## [0.5.1] - 2026-10-04

### Fixed

- A scheduled or hand-started run of the prepare workflow no longer cancels a push run
  waiting out `quiet_minutes`: only a newer push shares the settle job's concurrency
  group, and any other run gets a group of its own (#6)
- `prepare` names a branch `shipmill` on origin that blocks the `shipmill/<tag>` work
  branch, instead of failing with git's raw `cannot lock ref` error, and `doctor` checks
  for it as `work branch` (#7)
- `github-pr-triage` launches reviewer agents from the repo's root and has each confirm
  its remote: from a nested clone, worktree isolation copied the wrong repository and a
  reviewer's fix commits were lost with its worktree
- `triage_state.py` reads an issue's newest triage comment, not its oldest, so a
  re-decision changes NEEDS_PR and clears REVISIT. It compares comment and tag times as
  instants, so a tag date with a UTC offset no longer misorders against a comment's `Z`
  time
- The triage skills record a pass's decisions in one docs PR opened before dispatch, not
  in each implementing PR: decision ids are sequential, so two PRs that each added an
  entry both took the same `D-n`. github-pr-triage renumbers a colliding entry when it
  lands a batch

## [0.5.0] - 2026-10-03

### Added

- A design gate and a decisions log for the triage skills. An issue that changes a flag,
  a format, a default, a public API, or stored state gets its design written into the
  issue and checked against the repo's settled decisions before an implementer starts;
  a departure waits for the user. `decisions.py` keeps the log (`DECISIONS.md` or
  `docs/decisions.md`): `add` numbers and links entries, `find` lists the rules a diff's
  paths touch, `check` validates it. Implementer and reviewer briefs carry the matching
  rules, and github-pr-triage holds a PR that departs from one

## [0.4.0] - 2026-10-03

### Added

- The `github-ship-watch` skill: one pass reports a failed or stalled release bot, a
  release missing from PyPI, fixed issues not yet told which version shipped them, and
  issues triage owes, then finishes what the policy already decided. Its
  `watch_state.py` exits 1 when anything needs action, for `/loop` and `/schedule`

### Fixed

- `shipped.py` fetches tags with `--force`, so it no longer fails once the bot has moved
  a major tag such as `v0`

## [0.3.1] - 2026-10-03

### Fixed

- `github-pr-triage` starts a shipmill bot by naming the lane: a hand-started
  `lane=policy` run cancels the push run's quiet wait and then skips (#6)

## [0.3.0] - 2026-10-03

### Breaking

- release-lanes is now shipmill: the package, the CLI (`shipmill init`, `shipmill doctor`,
  and the rest), and the repository `romamo/shipmill`. In a repository already set up,
  replace `romamo/release-lanes` with `romamo/shipmill` in `.github/workflows/release.yml`,
  or run `shipmill init --force` and restore your policy. A release commit now waits on a
  `shipmill/` work branch

### Added

- The GitHub workflow skills join `release-lanes-setup`: `github-issue-triage`,
  `github-issue-resolve`, and `github-pr-triage`, with the whole flow in `docs/flow.md`.
  `github-pr-triage` starts a shipmill bot after its last merge
- A Claude Code plugin and marketplace in `.claude-plugin`: `/plugin marketplace add
  romamo/shipmill`, then `/plugin install shipmill@shipmill`. Each release sets the
  plugin's version

## [0.2.1] - 2026-10-03

### Fixed

- A rejected push now reports git's error, and a rejected tag push is retried: the first
  release by the bot (0.2.0) landed on main, but its tag push was refused and the
  reason was lost. The `land` step also fails when the tool fails, where a pipe through
  `tee` hid the failure and reported success

## [0.2.0] - 2026-10-03

### Added

- `doctor` fails a `[tool.uv.sources]` entry taken from a local path or installed
  editable, and the setup skill covers a Python package's build and smoke test

## [0.1.0] - 2026-10-01

### Added

- Release lanes driven by a hand-written CHANGELOG: `dev` and `rc` pre-releases cut from
  main without touching it, `stable` releases promoted from an rc that soaked without a
  blocker, and `hotfix` releases of chosen pull requests from `release/X.Y`
- Triggers per lane (quiet time after merges, schedule windows in any time zone, a finished
  milestone) and gates (a `release-blocker` label, freeze windows); every lane can be
  started by hand
- Version bumps from the pending entries' headings or from the paths changed since the last
  stable tag
- Reusable `prepare.yml` and `land.yml` workflows that run the caller's CI on the release
  commit before tagging, then sync a stable release made off main back into main
- `init` and `doctor` commands, and a setup skill for agents

[Unreleased]: https://github.com/shipmill/shipmill/compare/v0.29.0...HEAD
[0.29.0]: https://github.com/shipmill/shipmill/compare/v0.28.0...v0.29.0
[0.28.0]: https://github.com/shipmill/shipmill/compare/v0.27.0...v0.28.0
[0.27.0]: https://github.com/shipmill/shipmill/compare/v0.26.0...v0.27.0
[0.26.0]: https://github.com/shipmill/shipmill/compare/v0.25.0...v0.26.0
[0.25.0]: https://github.com/shipmill/shipmill/compare/v0.24.0...v0.25.0
[0.24.0]: https://github.com/shipmill/shipmill/compare/v0.23.0...v0.24.0
[0.23.0]: https://github.com/shipmill/shipmill/compare/v0.22.0...v0.23.0
[0.22.0]: https://github.com/shipmill/shipmill/compare/v0.21.0...v0.22.0
[0.21.0]: https://github.com/shipmill/shipmill/compare/v0.20.0...v0.21.0
[0.20.0]: https://github.com/shipmill/shipmill/compare/v0.19.0...v0.20.0
[0.19.0]: https://github.com/shipmill/shipmill/compare/v0.18.0...v0.19.0
[0.18.0]: https://github.com/shipmill/shipmill/compare/v0.17.0...v0.18.0
[0.17.0]: https://github.com/shipmill/shipmill/compare/v0.16.0...v0.17.0
[0.16.0]: https://github.com/shipmill/shipmill/compare/v0.15.0...v0.16.0
[0.15.0]: https://github.com/shipmill/shipmill/compare/v0.14.0...v0.15.0
[0.14.0]: https://github.com/romamo/shipmill/compare/v0.13.0...v0.14.0
[0.13.0]: https://github.com/romamo/shipmill/compare/v0.12.0...v0.13.0
[0.12.0]: https://github.com/romamo/shipmill/compare/v0.11.0...v0.12.0
[0.11.0]: https://github.com/romamo/shipmill/compare/v0.10.1...v0.11.0
[0.10.1]: https://github.com/romamo/shipmill/compare/v0.10.0...v0.10.1
[0.10.0]: https://github.com/romamo/shipmill/compare/v0.9.0...v0.10.0
[0.9.0]: https://github.com/romamo/shipmill/compare/v0.8.0...v0.9.0
[0.8.0]: https://github.com/romamo/shipmill/compare/v0.7.0...v0.8.0
[0.7.0]: https://github.com/romamo/shipmill/compare/v0.6.0...v0.7.0
[0.6.0]: https://github.com/romamo/shipmill/compare/v0.5.2...v0.6.0
[0.5.2]: https://github.com/romamo/shipmill/compare/v0.5.1...v0.5.2
[0.5.1]: https://github.com/romamo/shipmill/compare/v0.5.0...v0.5.1
[0.5.0]: https://github.com/romamo/shipmill/compare/v0.4.0...v0.5.0
[0.4.0]: https://github.com/romamo/shipmill/compare/v0.3.1...v0.4.0
[0.3.1]: https://github.com/romamo/shipmill/compare/v0.3.0...v0.3.1
[0.3.0]: https://github.com/romamo/shipmill/compare/v0.2.1...v0.3.0
[0.2.1]: https://github.com/romamo/shipmill/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/romamo/shipmill/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/romamo/shipmill/releases/tag/v0.1.0
