# Changelog

All notable changes to shipyard, named release-lanes before 0.3.0. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project follows
[Semantic Versioning](https://semver.org/). shipyard releases itself with its own
bot.

## [Unreleased]

### Added

- `shipyard operate` rolls back an environment that fails `[operate] rollback_after`
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
- github-ship-watch reports what `shipyard operate` found, for a repo whose config has
  `[environments.<name>]` tables: OPERATE_FAILED (the operate caller's latest run failed;
  the watch reruns a flaky one once), UNHEALTHY (an environment's newest `shipyard health`
  status is a failure), PROMOTION_DUE (an open "Ready to promote" proposal, with its
  approve command, or a promotion operate would make or propose if it ran on a schedule),
  and INCIDENT_OPEN (an open issue labelled `[operate] incident_label`, default
  `incident`, with its age and the PRs linked to close it). Every repo with a shipyard
  config also gets a HOLD row for an open `shipyard-hold` issue, with who opened it and
  when; a hold alone doesn't make the watch exit 1. Incidents and holds lead the report,
  and the watch never deploys or rolls back itself (#33)
- Under `release = "propose"`, the land workflow closes the lane's "Ready to release"
  issue once the release is tagged, with a comment naming the tag and the run, and says so
  when the issue proposed another version. A new `close-proposal` job takes the caller's
  grant: add `issues: write` to the land job in `.github/workflows/release.yml` (`shipyard
  init` writes it, and `doctor` warns under `release = "propose"` until it is there).
  `shipyard operate` closes a "Ready to promote" or "Ready to deploy" issue once the
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

### Fixed

- The triage skills keep an implementer's or reviewer's worktree until its PR merges, not
  just until it's pushed: a revision goes back to the same agent through SendMessage, and
  an agent whose worktree was removed can't be resumed
- `doctor` accepts the repository hosting shipyard calling its own `operate.yml` locally:
  there `.github/workflows/operate.yml` is the reusable workflow, so the operate check
  reads the workflow that `uses: ./.github/workflows/operate.yml` instead. A blank line no
  longer hides what follows it: a later job's `environment:`, or a workflow input (#53)
- `shipyard propose` and `shipyard operate` no longer open a duplicate proposal issue in
  a repo with more than 500 open issues: every release or deploy proposal carries a
  `shipyard-proposal` label, created on first use, and is found among the open issues with
  it (refusing, rather than guessing, at 100 of them). A proposal opened before the label
  is found once by the old scan and labelled on that update.
  github-ship-watch finds proposals by the label too, falling back to its title search
  while none has it (#41)

## [0.9.0] - 2026-10-04

### Added

- `shipyard gate <owner/repo>` starts a Claude Code background session (`claude --bg`)
  for a repo only when its state needs one: it reads the state with code, skips while a
  session it started is still working or waits on you, skips findings unchanged since
  the last launch, and passes the findings in the prompt. The prompt, whether open PRs
  count as work, and the retry window come from a new `[agents]` section of
  `.github/shipyard.toml`. `--refresh` moves a dedicated, detached checkout to the
  default branch first. The wheel now bundles the skills
- `shipyard launchd <owner/repo>` runs the gate from launchd every few minutes on a
  Mac, with a PATH built from where claude, gh, git, and uvx live outside temporary
  folders. Each pass is a new session, and a quiet tick makes no model call
- shipyard-setup connects the agent side: `setup_state.py` reports releases, the
  `[agents]` section, the plugin in `.claude/settings.json`, and the labels the skills
  read, and `--fix` enables the plugin and creates the labels. The gate on launchd is a
  new choice for where agents run
- `shipyard operate`, run every 10 minutes by the new reusable `operate.yml` through a
  caller that `shipyard init --operate` writes: for each environment it reads the current
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

- Environments: an `[environments.<name>]` table in `.github/shipyard.toml` names a deploy
  `workflow` and exactly one of `lane` or `from`, plus an optional `health` URL and, with
  `from`, `bake_minutes`; a `from` chain must end at a `lane` environment. `land` starts the
  workflow of each environment whose `lane` is the release's, with `tag` and `environment`
  inputs, and lists it as `deploy.yml@staging`; `from` environments wait for promotion.
  `doctor` checks each workflow takes both inputs and that a job sets `environment:`, so
  GitHub records a deployment; `init` writes a commented example (#29)
- `[autonomy]` in `.github/shipyard.toml` sets each stage to `observe`, `propose`, or `act`
  (`release`, `deploy.<environment>`, and `rollback`, all `act` by default), and an open
  issue labelled `shipyard-hold` turns every `act` into `propose`. Under `release =
  "observe"` a due lane is only reported; under `"propose"`, or while a hold is open, the
  new `propose` job of `prepare.yml` opens or updates one "Ready to release vX on <lane>"
  issue per due lane instead of releasing. A lane started by hand still releases under
  propose; under a hold only a hotfix started by hand does. `doctor` prints the effective
  autonomy per stage and warns on an open hold. Proposing needs `issues: write` on the
  prepare job of the calling workflow, which `shipyard init` now writes; `doctor` warns
  about it only once a stage is set to propose or a hold is open. Each `deploy.<name>` must
  name an environment in `[environments]`. Deploy and rollback autonomy take effect once
  shipyard deploys (#32)

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

- `.github/shipyard.toml`, the one config file every shipyard layer will read: `shipyard
  init` writes it, and `doctor` names the file it read. `.github/release-policy.toml` keeps
  working as an alias with the same keys, and `doctor` warns to rename it with `git mv`; a
  repo with both files fails. `init` refuses when either file exists, and `--force` replaces
  the alias with `shipyard.toml`. ship-watch finds either file. Rename to `shipyard.toml`
  only once the shipyard CLI your Release workflow runs (the `tool` input of `prepare.yml`
  and `land.yml`, default `@v0`) is this release or newer (#28)

## [0.6.0] - 2026-10-04

### Added

- "Where the agents run" in the README, and a last hand-over step in `shipyard-setup`
  that asks whether the skills run on demand, on a `/loop`, or as a cloud `/schedule`
  routine, and whether a routine may merge

### Changed

- The `release-lanes-setup` skill is now `shipyard-setup`: `/shipyard-setup` (or
  `/shipyard:shipyard-setup` from the plugin) replaces `/release-lanes-setup`. A checkout
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

- `doctor`'s `work branch` check asks origin for a `shipyard` branch, as `prepare` does,
  instead of reading the clone's fetched refs: a shallow or single-branch clone (such as a
  CI checkout) passed while origin had the branch, and a stale `origin/shipyard` failed
  after it was deleted. It warns when there is no origin or origin can't be reached

## [0.5.1] - 2026-10-04

### Fixed

- A scheduled or hand-started run of the prepare workflow no longer cancels a push run
  waiting out `quiet_minutes`: only a newer push shares the settle job's concurrency
  group, and any other run gets a group of its own (#6)
- `prepare` names a branch `shipyard` on origin that blocks the `shipyard/<tag>` work
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

- `github-pr-triage` starts a shipyard bot by naming the lane: a hand-started
  `lane=policy` run cancels the push run's quiet wait and then skips (#6)

## [0.3.0] - 2026-10-03

### Breaking

- release-lanes is now shipyard: the package, the CLI (`shipyard init`, `shipyard doctor`,
  and the rest), and the repository `romamo/shipyard`. In a repository already set up,
  replace `romamo/release-lanes` with `romamo/shipyard` in `.github/workflows/release.yml`,
  or run `shipyard init --force` and restore your policy. A release commit now waits on a
  `shipyard/` work branch

### Added

- The GitHub workflow skills join `release-lanes-setup`: `github-issue-triage`,
  `github-issue-resolve`, and `github-pr-triage`, with the whole flow in `docs/flow.md`.
  `github-pr-triage` starts a shipyard bot after its last merge
- A Claude Code plugin and marketplace in `.claude-plugin`: `/plugin marketplace add
  romamo/shipyard`, then `/plugin install shipyard@shipyard`. Each release sets the
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

[Unreleased]: https://github.com/romamo/shipyard/compare/v0.9.0...HEAD
[0.9.0]: https://github.com/romamo/shipyard/compare/v0.8.0...v0.9.0
[0.8.0]: https://github.com/romamo/shipyard/compare/v0.7.0...v0.8.0
[0.7.0]: https://github.com/romamo/shipyard/compare/v0.6.0...v0.7.0
[0.6.0]: https://github.com/romamo/shipyard/compare/v0.5.2...v0.6.0
[0.5.2]: https://github.com/romamo/shipyard/compare/v0.5.1...v0.5.2
[0.5.1]: https://github.com/romamo/shipyard/compare/v0.5.0...v0.5.1
[0.5.0]: https://github.com/romamo/shipyard/compare/v0.4.0...v0.5.0
[0.4.0]: https://github.com/romamo/shipyard/compare/v0.3.1...v0.4.0
[0.3.1]: https://github.com/romamo/shipyard/compare/v0.3.0...v0.3.1
[0.3.0]: https://github.com/romamo/shipyard/compare/v0.2.1...v0.3.0
[0.2.1]: https://github.com/romamo/shipyard/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/romamo/shipyard/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/romamo/shipyard/releases/tag/v0.1.0
