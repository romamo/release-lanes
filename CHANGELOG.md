# Changelog

All notable changes to shipyard, named release-lanes before 0.3.0. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project follows
[Semantic Versioning](https://semver.org/). shipyard releases itself with its own
bot.

## [Unreleased]

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

[Unreleased]: https://github.com/romamo/shipyard/compare/v0.3.1...HEAD
[0.3.1]: https://github.com/romamo/shipyard/compare/v0.3.0...v0.3.1
[0.3.0]: https://github.com/romamo/shipyard/compare/v0.2.1...v0.3.0
[0.2.1]: https://github.com/romamo/shipyard/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/romamo/shipyard/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/romamo/shipyard/releases/tag/v0.1.0
