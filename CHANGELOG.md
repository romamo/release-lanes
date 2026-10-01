# Changelog

All notable changes to release-lanes. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project follows
[Semantic Versioning](https://semver.org/). release-lanes releases itself with its own
bot.

## [Unreleased]

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

[Unreleased]: https://github.com/romamo/release-lanes/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/romamo/release-lanes/releases/tag/v0.1.0
