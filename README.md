# shipmill

**The open-source AI software factory for GitHub.** Agents triage your issues, fix them,
and land the pull requests; release lanes cut, publish, deploy, and roll back by a policy
you write. Everything runs on GitHub, and you stay in charge.

[![PyPI](https://img.shields.io/pypi/v/shipmill)](https://pypi.org/project/shipmill/)
[![Python](https://img.shields.io/pypi/pyversions/shipmill)](https://pypi.org/project/shipmill/)
[![CI](https://github.com/shipmill/shipmill/actions/workflows/ci.yml/badge.svg)](https://github.com/shipmill/shipmill/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue)](https://github.com/shipmill/shipmill/blob/main/LICENSE)

```
issue ─▶ triage ─▶ fix ─▶ review & land ─▶ release ─▶ deploy ─▶ watch
          agent     agent      agent         policy     policy    agent
```

## Why shipmill

Coding agents can write a fix. Getting that fix to users is the rest of the work: deciding
which issues deserve one, reviewing the pull request, waiting for green CI, cutting a
release, publishing it, telling the reporter, and noticing when production breaks.
shipmill does that rest, end to end, on any GitHub repository.

- **Issue to release, not just issue to PR.** One pipeline from a new issue to a published,
  deployed, and announced release
- **Policy, not prompts, decides what ships.** A single file, `.github/shipmill.toml`, says
  which lanes release and when (`dev`, `rc`, `stable`, `hotfix`), what gates hold them, and
  how far automation may go
- **You stay in control.** Each stage runs at `observe`, `propose`, or `act`; one
  `shipmill-hold` issue, opened from your phone, stops everything
- **No server, no state of its own.** GitHub is the database: issues, labels, pull requests,
  tags, deployments; nothing lives outside your repository
- **Releases you can trust.** CI runs on the exact release commit before it is tagged; a
  stable release ships the code a pre-release already soaked; a failing deploy rolls back
  and opens an incident

## How it works

| Stage | Done by | What happens |
|---|---|---|
| Intake | `product-intake` skill | Feature requests and discussions grouped into opportunities you accept or decline |
| Triage | `github-issue-triage` skill | A verdict on every issue (implement, feature, postpone, clarify), a comment, labels, one PR per fix; a feature gets a spec first |
| Fix | `github-issue-resolve` skill | One issue verified against main, fixed with a regression test, opened as a PR |
| Land | `github-pr-triage` skill | Each PR reviewed in its own worktree, small problems fixed, merged only on green CI |
| Release | shipmill workflows | The release cut on its lane when the policy says one is due, CI run on it, tagged, published |
| Deploy | `shipmill operate` | Health checks, bake time, promotion between environments, automatic rollback |
| Watch | `github-ship-watch` skill | A stalled release, a missing upload, an unannounced fix, an untriaged issue: reported and finished |

The skills run in [Claude Code](https://claude.com/claude-code), on demand or unattended on
a schedule. The release side is plain GitHub Actions plus the `shipmill` CLI, and works
without the agents.

## Quickstart

You need a GitHub repository with a `CHANGELOG.md` that has an `## [Unreleased]` section,
the [`gh`](https://cli.github.com/) CLI signed in, [`uv`](https://docs.astral.sh/uv/), and
Claude Code. From the root of your repository:

```bash
claude plugin marketplace add shipmill/shipmill && claude plugin install shipmill@shipmill && claude "/shipmill:shipmill-setup"
```

The `shipmill-setup` skill inspects the repository, asks which lanes to run, and wires
everything on a branch, ending with a pull request. Releases start in dry-run mode until you
switch them on.

Then ask what needs you:

```bash
uv tool install shipmill  # once; `uv tool upgrade shipmill` after a release
shipmill status
```

Without the install, `uvx --from git+https://github.com/shipmill/shipmill@v0 shipmill status`
runs it from git, resolving it again on each call.

It answers STUCK, WAITS ON YOU, WORKING, or IDLE, with a link to each item that waits on you.

Prefer to set it up by hand, or want only the release lanes without agents? Follow
[docs/install.md](https://github.com/shipmill/shipmill/blob/main/docs/install.md).

## Release lanes at a glance

| Lane | Version | Released from | Typical trigger |
|---|---|---|---|
| `dev` | `1.5.0.dev412` | main's head | after every batch of merges |
| `rc` | `1.5.0rc2` | main's head | each workday morning |
| `stable` | `1.5.0` | an rc that soaked without a blocker | weekly, a milestone, or by hand |
| `hotfix` | `1.4.1` | `release/1.4` plus the pull requests you name | by hand only |

Every pull request adds its own CHANGELOG entry; the entries decide the next version.
Installers skip `dev` and `rc` unless asked, so a user who needs a fix today pins the
pre-release while everyone else gets stable releases that real users already ran.

## Documentation

| Read | For |
|---|---|
| [Install](https://github.com/shipmill/shipmill/blob/main/docs/install.md) | Prerequisites, every setup step, and how to pause or remove shipmill |
| [Flow](https://github.com/shipmill/shipmill/blob/main/docs/flow.md) | What to say to run each stage, alone or all at once |
| [Release lanes](https://github.com/shipmill/shipmill/blob/main/docs/release-lanes.md) | The policy file, environments, operate, autonomy, versions, the CLI, and limits |
| [Decisions](https://github.com/shipmill/shipmill/blob/main/docs/decisions.md) | The rules shipmill follows and why |
| [Changelog](https://github.com/shipmill/shipmill/blob/main/CHANGELOG.md) | What changed in each release |

## Status

shipmill is alpha and releases itself with its own workflows, so every release you see on
[PyPI](https://pypi.org/project/shipmill/) went through the pipeline described above. Expect
the policy keys to change between minor versions; the CHANGELOG says how.

## Contributing

Issues and pull requests are welcome. A new feature starts as a spec in
[`docs/specs/`](https://github.com/shipmill/shipmill/tree/main/docs/specs); every pull
request adds its own entry under `## [Unreleased]` in the CHANGELOG. The checks CI runs are
listed in [CLAUDE.md](https://github.com/shipmill/shipmill/blob/main/CLAUDE.md).

## License

[MIT](https://github.com/shipmill/shipmill/blob/main/LICENSE)
