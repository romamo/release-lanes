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
