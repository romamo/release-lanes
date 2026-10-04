# Decisions

Rules the maintainers settled, numbered in order. Triage designs and code reviews check
every change against the entries whose "Applies to" it touches. Change a rule by adding an
entry that supersedes it, never by editing an old one.

## D-1: Only a push cancels the settle wait; lane=policy follows the policy

- Decided: 2026-10-04, in romamo/shipyard#6
- Rule: The settle job's cancelling concurrency group is shared by push runs only, and a hand-started lane=policy run evaluates triggers exactly as a scheduled run does (quiet is never due by hand)
- Why: A dispatch or cron run cancelled a push run's quiet wait and then skipped; making lane=policy count quiet as due would change its meaning and make local shipyard plan previews report quiet as due. Ending a batch by hand means starting the lane
- Applies to: .github/workflows/prepare.yml, src/shipyard/planner.py, workflow_dispatch lane input
- Enforced by: tests/test_workflows.py (with #14); review for the planner

## D-2: Work branches keep the shipyard/ prefix; collisions are detected

- Decided: 2026-10-04, in romamo/shipyard#7
- Rule: Release work branches stay under shipyard/ (WORK_PREFIX); a branch that blocks them is reported by prepare and doctor with a fix, never avoided by renaming the prefix
- Why: Branch protection rules or CI filters may match shipyard/, and a run straddling a tool upgrade could miss its work branch; a new prefix only makes a collision less likely
- Applies to: src/shipyard/land.py, src/shipyard/doctor.py, work branch names
- Enforced by: tests/test_lanes.py and tests/test_setup.py (with #15); review for the prefix

## D-3: User-facing prose calls the release automation shipyard

- Decided: 2026-10-04, in romamo/shipyard#18
- Rule: README, docs, workflow comments, init's generated files, and skill prose call the release workflows and CLI shipyard; 'release bot' appears only as a skill trigger phrase or when it means any release bot (such as a legacy release-bot.yml)
- Why: 'Run by agents and one release bot' read as a service separate from shipyard, when it is shipyard's own prepare.yml and land.yml running the shipyard CLI; people still ask for 'a release bot', so skill descriptions keep the phrase to match
- Applies to: README.md, docs/*.md, .github/workflows/*.yml, src/shipyard/init.py, skills/*/SKILL.md
- Enforced by: review

## D-4: One factory config file: .github/shipyard.toml

- Decided: 2026-10-04, in romamo/shipyard#26
- Rule: Every shipyard layer reads its settings from one file, .github/shipyard.toml, one top-level section per layer; .github/release-policy.toml stays a supported alias with the same release keys, and a repo may not have both
- Why: doctor checks the whole factory in one pass, and users learn one file; the alias keeps every existing repo working without a migration
- Applies to: src/shipyard/policy.py, src/shipyard/doctor.py, src/shipyard/init.py, .github/shipyard.toml, .github/release-policy.toml
- Enforced by: review, until the config loader tests land

## D-5: Specs are files in docs/specs/

- Decided: 2026-10-04, in romamo/shipyard#26
- Rule: A feature's spec is a file docs/specs/NNN-<slug>.md merged through a pull request before its build starts; issue bodies link to the spec, they don't hold it
- Why: a file is reviewed line by line, versioned with the code it describes, and readable by agents from the checkout
- Applies to: docs/specs/*, skills/github-issue-triage/*, spec gate
- Enforced by: review

## D-6: Operations data starts minimal

- Decided: 2026-10-04, in romamo/shipyard#26
- Rule: The operate and incident layers read only health URLs from the config and GitHub deployments; an external tracker or metrics source is added later behind its own config section, never required
- Why: a factory that needs no third-party account works for every repo on day one; richer sources can come once the minimal loop is proven
- Applies to: operate layer, incident layer, deploy_state.py, ops-watch skill
- Enforced by: review

## D-7: Production deploys act by default

- Decided: 2026-10-04, in romamo/shipyard#26
- Rule: A production environment's autonomy defaults to act: it deploys once its source environment has passed health checks for the bake time, with no human approval, unless the config sets propose or the stop switch is on
- Why: the release lanes already gate what ships; a bake time with health checks and automatic rollback is the safety, and the stop switch is the human override
- Applies to: deploy layer, [autonomy] config, environments config
- Enforced by: review

## D-8: A hold stops every release except a hand-started hotfix

- Decided: 2026-10-04, in romamo/shipyard#32
- Rule: While an issue labelled shipyard-hold is open, no lane releases (scheduled, push, or started by hand), except a hotfix a person starts by hand; the skip reason names the hold and says a hotfix can still be started
- Why: A hold usually means an incident, and a hotfix is its cure; making a person close the hold first adds a step at the worst moment, while everything automatic still stops
- Applies to: src/shipyard/autonomy.py, src/shipyard/planner.py, shipyard-hold label
- Enforced by: tests/test_autonomy.py (with #38)

## D-9: Warn about a new permission only where it is needed

- Decided: 2026-10-04, in romamo/shipyard#32
- Rule: doctor warns about a permission the caller's release.yml lacks only when the repo's config or state needs it (issues: write when a stage proposes or a hold is open); setups that don't use the feature see no new warning
- Why: a warning every existing setup gets for a feature it doesn't use trains people to ignore doctor; the failing job still explains the one-line fix when the need arises
- Applies to: src/shipyard/doctor.py, .github/workflows/prepare.yml, caller permissions
- Enforced by: review

## D-10: Product intake never acts; the maintainer accepts or declines

- Decided: 2026-10-05, in romamo/shipyard#69
- Rule: Product intake proposes and reports only ([autonomy] intake is observe or propose, never act): an opportunity becomes planned work only when the maintainer accepts it, and a declined opportunity keeps its recorded reason and is never re-proposed
- Why: what to build is the maintainer's call; the factory's job is to gather the evidence and keep the decision on record, so the same request isn't argued twice
- Applies to: skills/product-intake/*, src/shipyard/autonomy.py, [autonomy] intake, opportunity issues
- Enforced by: tests/test_autonomy.py (intake refuses act, with #76); review
