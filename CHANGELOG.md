# Changelog

All notable changes to shipyard, named release-lanes before 0.3.0. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project follows
[Semantic Versioning](https://semver.org/). shipyard releases itself with its own
bot.

## [Unreleased]

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
  about it only once a stage is set to propose or a hold is open. Deploy and rollback
  autonomy take effect once shipyard deploys (#32)

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

[Unreleased]: https://github.com/romamo/shipyard/compare/v0.7.0...HEAD
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
