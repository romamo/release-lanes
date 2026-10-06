# Decisions

Rules the maintainers settled, numbered in order. Triage designs and code reviews check
every change against the entries whose "Applies to" it touches. Change a rule by adding an
entry that supersedes it, never by editing an old one.

## D-1: Only a push cancels the settle wait; lane=policy follows the policy

- Decided: 2026-10-04, in romamo/shipmill#6
- Rule: The settle job's cancelling concurrency group is shared by push runs only, and a hand-started lane=policy run evaluates triggers exactly as a scheduled run does (quiet is never due by hand)
- Why: A dispatch or cron run cancelled a push run's quiet wait and then skipped; making lane=policy count quiet as due would change its meaning and make local shipmill plan previews report quiet as due. Ending a batch by hand means starting the lane
- Applies to: .github/workflows/prepare.yml, src/shipmill/planner.py, workflow_dispatch lane input
- Enforced by: tests/test_workflows.py (with #14); review for the planner

## D-2: Work branches keep the shipmill/ prefix; collisions are detected

- Decided: 2026-10-04, in romamo/shipmill#7
- Rule: Release work branches stay under shipmill/ (WORK_PREFIX); a branch that blocks them is reported by prepare and doctor with a fix, never avoided by renaming the prefix
- Why: Branch protection rules or CI filters may match shipmill/, and a run straddling a tool upgrade could miss its work branch; a new prefix only makes a collision less likely
- Applies to: src/shipmill/land.py, src/shipmill/doctor.py, work branch names
- Enforced by: tests/test_lanes.py and tests/test_setup.py (with #15); review for the prefix

## D-3: User-facing prose calls the release automation shipmill

- Decided: 2026-10-04, in romamo/shipmill#18
- Rule: README, docs, workflow comments, init's generated files, and skill prose call the release workflows and CLI shipmill; 'release bot' appears only as a skill trigger phrase or when it means any release bot (such as a legacy release-bot.yml)
- Why: 'Run by agents and one release bot' read as a service separate from shipmill, when it is shipmill's own prepare.yml and land.yml running the shipmill CLI; people still ask for 'a release bot', so skill descriptions keep the phrase to match
- Applies to: README.md, docs/*.md, .github/workflows/*.yml, src/shipmill/init.py, skills/*/SKILL.md
- Enforced by: review

## D-4: One factory config file: .github/shipmill.toml

- Decided: 2026-10-04, in romamo/shipmill#26
- Rule: Every shipmill layer reads its settings from one file, .github/shipmill.toml, one top-level section per layer
- Why: doctor checks the whole factory in one pass, and users learn one file
- Applies to: src/shipmill/policy.py, src/shipmill/doctor.py, src/shipmill/init.py, .github/shipmill.toml
- Enforced by: review, until the config loader tests land

## D-5: Specs are files in docs/specs/

- Decided: 2026-10-04, in romamo/shipmill#26
- Rule: A feature's spec is a file docs/specs/NNN-<slug>.md merged through a pull request before its build starts; issue bodies link to the spec, they don't hold it
- Why: a file is reviewed line by line, versioned with the code it describes, and readable by agents from the checkout
- Applies to: docs/specs/*, skills/github-issue-triage/*, spec gate
- Enforced by: review

## D-6: Operations data starts minimal

- Decided: 2026-10-04, in romamo/shipmill#26
- Rule: The operate and incident layers read only health URLs from the config and GitHub deployments; an external tracker or metrics source is added later behind its own config section, never required
- Why: a factory that needs no third-party account works for every repo on day one; richer sources can come once the minimal loop is proven
- Applies to: operate layer, incident layer, deploy_state.py, ops-watch skill
- Enforced by: review

## D-7: Production deploys act by default

- Decided: 2026-10-04, in romamo/shipmill#26
- Rule: A production environment's autonomy defaults to act: it deploys once its source environment has passed health checks for the bake time, with no human approval, unless the config sets propose or the stop switch is on
- Why: the release lanes already gate what ships; a bake time with health checks and automatic rollback is the safety, and the stop switch is the human override
- Applies to: deploy layer, [autonomy] config, environments config
- Enforced by: review

## D-8: A hold stops every release except a hand-started hotfix

- Decided: 2026-10-04, in romamo/shipmill#32
- Rule: While an issue labelled shipmill-hold is open, no lane releases (scheduled, push, or started by hand), except a hotfix a person starts by hand; the skip reason names the hold and says a hotfix can still be started
- Why: A hold usually means an incident, and a hotfix is its cure; making a person close the hold first adds a step at the worst moment, while everything automatic still stops
- Applies to: src/shipmill/autonomy.py, src/shipmill/planner.py, shipmill-hold label
- Enforced by: tests/test_autonomy.py (with #38)

## D-9: Warn about a new permission only where it is needed

- Decided: 2026-10-04, in romamo/shipmill#32
- Rule: doctor warns about a permission the caller's release.yml lacks only when the repo's config or state needs it (issues: write when a stage proposes or a hold is open); setups that don't use the feature see no new warning
- Why: a warning every existing setup gets for a feature it doesn't use trains people to ignore doctor; the failing job still explains the one-line fix when the need arises
- Applies to: src/shipmill/doctor.py, .github/workflows/prepare.yml, caller permissions
- Enforced by: review

## D-10: Product intake never acts; the maintainer accepts or declines

- Decided: 2026-10-05, in romamo/shipmill#69
- Rule: Product intake proposes and reports only ([autonomy] intake is observe or propose, never act): an opportunity becomes planned work only when the maintainer accepts it, and a declined opportunity keeps its recorded reason and is never re-proposed
- Why: what to build is the maintainer's call; the factory's job is to gather the evidence and keep the decision on record, so the same request isn't argued twice
- Applies to: skills/product-intake/*, src/shipmill/autonomy.py, [autonomy] intake, opportunity issues
- Enforced by: tests/test_autonomy.py (intake refuses act, with #76); review

## D-11: A hold stops the gate from starting sessions

- Decided: 2026-10-04, in romamo/shipmill#45
- Rule: While an issue labelled shipmill-hold is open, shipmill gate starts no Claude Code session for the repo (it reports HELD); a session already running is left to finish, and the gate names it so a person can stop it
- Why: A hold usually means an incident; an agent that triages, implements, or merges while one is open adds changes at the worst moment, and the stop switch should stop every automatic actor, not only releases (D-8)
- Applies to: src/shipmill/gate.py, shipmill-hold label
- Enforced by: tests/test_gate.py (with the implementing PR)
- Superseded by: D-13

## D-12: Whoever creates a worktree removes it

- Decided: 2026-10-05, in shipmill/shipmill#116
- Rule: Whoever creates a worktree removes it once its PR merges; once the creating session has exited, shipmill gate's prune owns it and removes only what the spec's checks prove landed
- Why: Sessions that exit before their PR merges leave worktrees and local branches no later pass touches, since each pass removes only what it created; one owner for the orphans, acting only on proof, collects them without risking unlanded work
- Applies to: src/shipmill/gate.py, skills/*/SKILL.md, worktrees
- Enforced by: review

## D-13: A hold stops the gate from starting sessions, not from stopping a stuck one

- Decided: 2026-10-05, in shipmill/shipmill#119
- Rule: While an issue labelled shipmill-hold is open, shipmill gate starts no Claude Code session for the repo (it reports HELD); a session already running is left to finish, and the gate names it so a person can stop it. The one exception: a session that has waited on the user for [agents] max_wait_hours is stopped, held or not
- Why: A hold exists to stop automatic actors from adding changes; stopping a session that waits on a question adds none, and leaving it would keep the repo stalled after the hold closes
- Applies to: src/shipmill/gate.py, shipmill-hold label, [agents] max_wait_hours
- Enforced by: tests/test_gate.py (with the implementing PR)
- Supersedes: D-11
- Superseded by: D-15

## D-14: Agent sessions write as the App or not at all

- Decided: 2026-10-05, in shipmill/shipmill#137
- Rule: When [agents] app_id is set, shipmill gate starts a session only as that GitHub App, with a token limited to the gate's repo; any failure to set up the App's identity exits 2 instead of falling back to the host's gh login
- Why: A silent fallback would make agent work look like the maintainer's again, defeating the metrics and the reviews that rely on telling the two apart, and would hand the session the maintainer's full token
- Applies to: src/shipmill/gate.py, [agents] app_id, shipmill app-token
- Enforced by: tests/test_gate.py (with the implementing PR)

## D-15: A session waiting on the user is stopped after 15 minutes unless configured

- Decided: 2026-10-06, by the maintainer after a gate session waited about 10 hours
- Rule: While an issue labelled shipmill-hold is open, shipmill gate starts no Claude Code session for the repo (it reports HELD); a session already running is left to finish, and the gate names it so a person can stop it. A session that has waited on the user for [agents] max_wait_minutes is stopped, held or not; the key counts minutes, defaults to 15, and 0 never stops one
- Why: The gate runs one session per repo, so a session waiting on a question stops triage, landing, and shipped notices for every issue; with no limit by default, one unanswered question stalled the repo for 10 hours. A stopped session keeps its conversation, so `claude attach <id>` still shows the question, and the next tick can start fresh work
- Applies to: src/shipmill/agents.py, src/shipmill/gate.py, shipmill-hold label, [agents] max_wait_minutes
- Enforced by: tests/test_gate_waiting.py, tests/test_config.py
- Supersedes: D-13

## D-16: Unattended agent sessions work only on trusted authors' items

- Decided: 2026-10-06, in shipmill/shipmill#148
- Rule: A shipmill gate session that nobody can watch ([agents] mode = "headless", and any later unattended mode) is started only for issues opened by an OWNER, MEMBER, or COLLABORATOR or by the gate's own App bot, and for pull requests whose head is in the repo; everything else waits for an interactive session
- Why: An unattended session reads issue text with the whole workspace in reach and is allowed git, gh, and uv run, which can each run code; the tool allowlist only keeps prompts from blocking it, so who opened the item is the control that keeps an outsider's text from steering it
- Applies to: src/shipmill/gate.py, skills/github-issue-triage/scripts/triage_state.py, skills/github-ship-watch/scripts/watch_state.py, [agents] mode
- Enforced by: tests/test_gate.py and the triage_state.py and watch_state.py tests (with the implementing PR)
