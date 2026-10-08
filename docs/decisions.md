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
- Superseded by: D-24

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

- Decided: 2026-10-06, in shipmill/shipmill#147
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

## D-17: Headless gate sessions run as claude -p, tracked by their process

- Decided: 2026-10-06, in shipmill/shipmill#160
- Rule: In [agents] mode = "headless", shipmill gate starts each session with `claude -p`, never `claude --bg`, detached from the tick with its output in a log under the state directory, and decides whether it still runs from the recorded pid and process start time, not from `claude agents`; a headless session never reads as waiting on the user
- Why: The probe for #160 showed a `--bg` session reads `blocked` whenever its last reply asks for something, with no prompt pending, so a session that posts its question on GitHub would still hold the repo as WAITING until max_wait_minutes; `--bg` and `--print` refuse to combine, and a print session exits when its prompt is done, so it has no state in which it waits. The cost is `claude attach`: `claude --resume <id>` and the log replace it
- Applies to: src/shipmill/gate.py, src/shipmill/launchd.py, [agents] mode, gate.json
- Enforced by: tests/test_gate.py and tests/test_launchd.py (with the implementing PR)

## D-18: A Release run recovers an orphaned work branch

- Decided: 2026-10-06, in shipmill/shipmill#175
- Rule: When prepare finds the shipmill/vX.Y.Z work branch on origin and no queued or in-progress run of the same release workflow owns it, it deletes the branch and pushes its own; this needs actions: read on the caller's prepare job, and without that permission prepare keeps the stop, with a warning annotation and a step-summary line naming the branch, and the run stays green
- Why: A run cancelled before its cleanup job gets a runner can't clean up after itself, so recovery has to come from a later run; the permission is new for callers, so a caller that doesn't grant it keeps today's behaviour, visibly, instead of breaking
- Applies to: src/shipmill/land.py, src/shipmill/cli.py, .github/workflows/release.yml, shipmill init, skills/github-ship-watch/scripts/watch_state.py
- Enforced by: tests (with the implementing PR)

## D-19: A gate without an App is unfinished setup

- Decided: 2026-10-07, in shipmill/shipmill#204
- Rule: shipmill-setup sets [agents] app_id before it installs the gate's launchd job, and an [agents] prompt with no app_id reads as an action for a person in setup_state.py (AGENTS_NO_APP) and watch_state.py (GATE_NO_APP), never as done; the gate and launchd commands still run without one
- Why: Without an App a session's comments, PRs, and commits read as the maintainer's own, the maintainer can't approve its PRs, and GitHub doesn't notify them of its mentions; on romamo/treaty a gate went live that way unnoticed because the report counted it as done. Refusing in the gate itself would break gates already installed
- Applies to: skills/shipmill-setup/SKILL.md, skills/shipmill-setup/scripts/setup_state.py, skills/github-ship-watch/scripts/watch_state.py, [agents] app_id, shipmill status
- Enforced by: the setup_state.py and watch_state.py tests (with the implementing PR)

## D-20: Setup writes [agents] mode explicitly

- Decided: 2026-10-07, in shipmill/shipmill#205
- Rule: shipmill-setup asks for [agents] mode every time it sets up the gate and writes the answer as mode = "interactive" or mode = "headless"; an [agents] prompt with no mode key reads as an action in setup_state.py (AGENTS_NO_MODE), while the config's default stays interactive
- Why: A setup that skipped the question installed an interactive gate on a machine nobody watches (romamo/treaty); the default keeps existing gates working, and the report makes the missing answer visible
- Applies to: skills/shipmill-setup/SKILL.md, skills/shipmill-setup/scripts/setup_state.py, [agents] mode
- Enforced by: the setup_state.py tests (with the implementing PR)

## D-21: Every gate session asks through the needs-decision protocol

- Decided: 2026-10-07, in shipmill/shipmill#206
- Rule: A shipmill gate session, interactive or headless, posts each question as a needs-decision marker comment with the needs-decision label before anything else; an interactive session also asks in the session, and when the answer comes there it removes the label and goes on; setup creates the needs-decision label whenever the config has an [agents] section
- Why: An interactive gate session nobody attached to improvised plain question comments without the marker or the label (romamo/treaty), so watch_state.py kept counting the items as work and the gate stuck in its UNCHANGED cooldown; a session can't tell whether anyone is attached, and posting first takes the item out of the gate's work at once. Switching to headless after a wait would need new gate behaviour to restart sessions
- Applies to: skills/github-issue-triage/references/needs-decision.md, skills/github-issue-triage/SKILL.md, skills/github-issue-resolve/SKILL.md, skills/github-pr-triage/SKILL.md, skills/shipmill-setup/scripts/setup_state.py, src/shipmill/gate.py, needs-decision label
- Enforced by: the setup_state.py and gate prompt tests (with the implementing PR)

## D-22: The gate updates its checkout's plugin once a day, only when the config allows it

- Decided: 2026-10-07, in shipmill/shipmill#233
- Rule: With [agents] plugin_update = true, shipmill gate checks at most once per 24 hours (the last check recorded under the repo's git directory) whether its checkout's project install of shipmill@shipmill is behind the latest release and, when it is, updates that install before it starts a session; a failed update is reported on the tick and the session still starts. The key defaults to false, and then the gate changes no install. Either way, shipmill status, doctor, and setup_state.py report a behind install in the repo's folder and in the gate's checkout as separate rows, each with a fix that works when run in that folder
- Why: Gate sessions ran a plugin install in tmp/shipmill-gate that was 0.25.0 while 0.32.1 was out, and the update status printed resolved to that nested install, so the repo's own install stayed stale and the line never cleared (romamo/treaty). A check before every session would cost a claude call per tick; a default of false keeps gates already installed from changing installs without a config change
- Applies to: src/shipmill/gate.py, src/shipmill/agents.py, src/shipmill/init.py, src/shipmill/doctor.py, skills/github-ship-watch/scripts/watch_state.py, skills/shipmill-setup/scripts/setup_state.py, [agents] plugin_update, shipmill status
- Enforced by: the gate, watch_state.py, and setup_state.py tests (with the implementing PR)
- Superseded by: D-23

## D-23: The gate updates every install keyed on its checkout, project or local scope

- Decided: 2026-10-08, in shipmill/shipmill#240
- Rule: With [agents] plugin_update = true, shipmill gate checks at most once per 24 hours (the last check recorded under the repo's git directory) whether any install of shipmill@shipmill keyed on its checkout, at project or local scope, is behind the latest release and, when one is, updates it before it starts a session; a failed update is reported on the tick and the session still starts. The key defaults to false, and then the gate changes no install. Either way, shipmill status, doctor, and setup_state.py report a behind install in the repo's folder and in the gate's checkout as separate rows, each with a fix that works when run in that folder
- Why: shipmill's own gate checkout holds a local-scope install (0.25.0) that D-22's project-only rule never updated, so its sessions kept old skills while the other gated repos, on project scope, got the latest; the maintainer chose to cover any install keyed on the checkout
- Applies to: src/shipmill/gate.py, src/shipmill/plugin.py, src/shipmill/agents.py, src/shipmill/doctor.py, skills/github-ship-watch/scripts/watch_state.py, skills/shipmill-setup/scripts/setup_state.py, [agents] plugin_update, shipmill status
- Enforced by: the gate and plugin tests (with the implementing PR)
- Supersedes: D-22

## D-24: A worktree whose tip was a merged PR's head has landed

- Decided: 2026-10-08, in shipmill/shipmill#244
- Rule: Whoever creates a worktree removes it once its PR merges; once the creating session has exited, shipmill gate's prune owns it and removes only what the spec's checks prove landed, where a branch also counts as landed when its tip is, or was before a force-push, the head of a merged pull request on that branch; a commit made after that tip keeps the worktree
- Why: Review often rebases, amends, or drops part of a PR before a squash merge, so the worktree's own commit is in no merged history and its worktree read as not landed forever (treaty PR #75); the merged PR was that commit's review, so work dropped there has landed in effect, and a later commit still protects real unlanded work
- Applies to: src/shipmill/worktrees.py, src/shipmill/gate.py, skills/*/SKILL.md, worktrees, docs/specs/002-*
- Enforced by: the worktrees tests (with the implementing PR)
- Supersedes: D-12
