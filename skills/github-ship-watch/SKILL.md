---
name: github-ship-watch
description: Watch a GitHub repo's issue-to-release pipeline on a loop or a schedule, and finish what its standing policy already decided. Reports a failed or stalled release bot, a release missing from PyPI, fixed issues not yet told which version shipped them, issues that triage owes, and, for a repo running shipyard operate, a failed operate run, an unhealthy environment, a promotion waiting on approval, open incidents, a closed incident with no postmortem, and a hold; then reruns a flaky release job, starts a stalled lane, posts the shipped notices, and hands new issues to github-issue-triage when asked. Drafts a due postmortem as a pull request, and runs a weekly retro when the user schedules one. Use when the user asks to "watch the repo", "keep it shipping", "babysit releases", "check the release went out", or sets up /loop or /schedule for a repo. Not for triaging a backlog by hand (github-issue-triage), landing PRs (github-pr-triage), or setting up shipyard (shipyard-setup).
---

# GitHub Ship Watch

One pass answers "is anything stuck between an issue and a user's install?" and finishes the work the user already decided: the release policy decided to release, so a stalled release starts, a flaky job reruns, and the fixed issues hear about it. Anything that needs a new decision goes in the report.

## Inputs

- Repo `owner/name` and a local checkout (a scheduled cloud session has one)
- Scope from the user's wording, read narrowly:
  - "status": report only, change nothing
  - "watch" (the default): report, plus the repairs in the table below
  - "watch and triage": also run github-issue-triage on what the intake flags, passing on the user's own scope words for it ("merge when green" included only if the user said it)

## Hard rules

1. **Finish decisions; never make new ones.** A watch pass doesn't merge, tag by hand, change a release policy, close or relabel issues, or edit code. Those belong to the user or to the other skills under the user's words. A postmortem draft is a pull request the maintainer merges, never a change the watch lands
2. **One repair per finding per pass.** Rerun a failed job once. If it fails again, the next pass reports it rather than looping on reruns
3. **Read state right before acting.** Another session may be landing PRs or releasing in the same repo. Check `gh run list` before starting a lane, and follow a peer's hold (see github-pr-triage, hard rule 5)
4. **A repo with `release-blocker` open is held on purpose.** A stalled bot behind a blocker isn't stalled: report it, don't start a lane
5. **An open `shipyard-hold` issue stops the factory on purpose.** A person pulled the stop switch: report the hold (the HOLD row: who opened it and when), never start a lane or close the issue. A plan under the hold, or under `[autonomy] release = "propose"`, proposes instead of releasing, so it never reads as BOT_STALLED; the proposal issue is for a person to act on
6. **The watch never deploys, promotes, or rolls back.** `shipyard operate` acts on environments under the repo's autonomy; the watch reports what operate found and repairs only a stuck operate run, as it does for the release bot. Approving a proposal is the user's call: give them the command

## The script

`scripts/watch_state.py <owner/repo> [--repo-dir PATH]` answers the whole status question deterministically. Run it with `uv run --no-project python` (or `python3`, 3.10+) at the start of every pass and again before the report. Exit 0 means nothing needs action; 1 means a row below is an action; 2 is a git, gh, uvx, or PyPI failure (report it and stop).

| State | Repair under "watch" |
|---|---|
| INCIDENT_OPEN | Report first: the issue, how long it has been open, and whether a PR links it to close it. Recommend landing the linked hotfix PR, or writing one when none links it. Rollback is operate's (or the user's), never the watch's |
| HOLD | Report next: the issue, who opened it, and when. Not an action (exit 0 by itself); see hard rule 5 |
| POSTMORTEM_DUE | An incident closed and no `docs/postmortems/*.md` on the default branch names it. Draft the postmortem as a pull request ([Postmortems](#postmortems)), once per incident: an open PR that already names it means the draft waits on the maintainer, so report the PR instead |
| OPERATE_FAILED | The latest run of the workflow calling shipyard's `operate.yml` failed. Read it; rerun a flaky or transient failure once (`gh run rerun <id> --failed`), as for BOT_FAILED. Otherwise report the failing step with the run link |
| UNHEALTHY | An environment's newest `shipyard health` status is a failure. Report the environment, the release, and the check's error. Operate owns the rollback; the watch never deploys over it |
| PROMOTION_DUE | A deploy waits on a person. With a proposal issue, report it with its approve command (`gh workflow run operate.yml -f approve=<environment> -f dry-run=false`; under a hold, the hold closes first). Without one, operate would promote the environment (deploy autonomy `act`), or propose it (`propose`, or a hold), but isn't running on a schedule, so no proposal issue opens: report what operate would do, why it isn't running, and the command that runs it once (or `shipyard init --operate` when no caller exists). Never run either yourself |
| BOT_FAILED | Read the run. If the failure is a known flaky test or a transient push or network error ([github-pr-triage's ci-failures.md](../github-pr-triage/references/ci-failures.md)), `gh run rerun <id> --failed` once. Otherwise report the failing step and its error, with the run link |
| BOT_STALLED | A shipyard bot has a release due and nothing running. Start the lane the plan names: `gh workflow run release.yml -f lane=<lane> -f dry-run=false`. Never `lane=policy` by hand: it skips while main isn't quiet yet |
| NOT_PUBLISHED | Find the publish run for the tag (`gh run list -w <publish workflow> --branch <tag>` or by the release's dispatch). Rerun its failed jobs once if the cause is flaky; a failed release check or build is reported, not retried |
| PUBLISHING | Nothing; the next pass checks again |
| PUBLISHED | Once per release, check that a clean install runs (github-pr-triage's [landing.md](../github-pr-triage/references/landing.md#ecosystems)); retry once on index lag |
| UNANNOUNCED | Post the notices: `../github-pr-triage/scripts/shipped.py <repo> <prev> <tag> --install '<install command>' --post`. Show the plan first if this repo has never had notices |
| ISSUES | Under "watch and triage", run github-issue-triage on the flagged issues. Otherwise list them |
| PRS_OPEN, BOT_OK, BOT_NONE, NO_REGISTRY | Report only |

Pass `--grace` to give a slow publish more minutes before it reads NOT_PUBLISHED, and `--tool` when the shipyard bot isn't installed from `romamo/shipyard@v0`.

HOLD and POSTMORTEM_DUE show for every repo with a shipyard config, since a hold stops releases too and an incident may be labelled by hand. POSTMORTEM_DUE reads the postmortems through the GitHub contents API on the default branch, so a draft counts only once merged. The operations states show only when the config declares environments (read with `tomllib`; on Python 3.10 only plain `[environments.<name>]` tables, and any other form stops the watch with a one-line message); they come from the GitHub deployments and issues `shipyard operate` writes. The incident label is `[operate] incident_label`, `incident` by default.

## Report

One line when exit 0: "Nothing owed: bot OK, <latest tag> published and announced, no issues waiting." (add "held by #N" when a HOLD row shows). Otherwise lead with open incidents and holds, then what needs the user (each with a recommendation), then what the pass repaired with links, then what it left for the next pass. Under /loop, keep quiet passes to that one line.

## Postmortems

Every closed incident gets a postmortem: a file `docs/postmortems/YYYY-MM-DD-<slug>.md` (the day the incident opened) from shipyard's [`docs/postmortems/TEMPLATE.md`](../../docs/postmortems/TEMPLATE.md), merged through a pull request; the first postmortem copies the template into the repo too. The watch drafts it; the maintainer approves it by merging, and nothing merges without the usual gates.

1. Read the record: the incident issue and its comments (`gh issue view <n> --json title,body,createdAt,closedAt,comments`), and the deployment statuses of the environment it names (`gh api repos/<repo>/deployments?environment=<env>`, then each deployment's `statuses`) from the release before the fault to the recovery
2. Draft from the record only: the timeline in UTC from those comments and statuses, the cause and the change that introduced it, what caught it, what would have caught it sooner, and the actions. Keep one `Incident: <owner/repo>#<n>` line per incident the postmortem covers; the watch reads that line. Mark anything the record doesn't show as a question for the maintainer, never a guess
3. Sort each action. A rule the team should now follow is a decision: propose it in the table as `D-n`, the next number `decisions.py` would give, with its rule, why, applies to, and enforced by. Any other action is work: link an open issue that already covers it (`../github-issue-triage/scripts/decisions.py find` for rules, `scripts/retro.py dedupe` for issue titles), or name it as an issue to open
4. Open the PR on a `docs/postmortem-<n>` branch, with the incident linked. Don't open the action issues or record the decisions before the merge: the maintainer may change them in review
5. After the maintainer merges, open each action issue the postmortem names (deduplicated with `retro.py dedupe`), and record each rule with `decisions.py add` in its own docs pull request ([github-issue-triage's design gate](../github-issue-triage/references/design-gate.md), Recording a decision). Then edit the postmortem's table to link them, in the same docs PR

Opening issues and PRs for a postmortem is the only writing the watch does beyond its repairs, and it does that only under "watch". Under "status", report POSTMORTEM_DUE and stop.

## Weekly retro

The retro is an option the user schedules, never a default: `/schedule` a weekly routine that follows this section for the repo and the roadmap issue the user names. It proposes; the user decides.

1. Gather: `scripts/retro.py gather <owner/repo> [--days 7]` runs `metrics.py --json` for this week and the week before it (`--until` this week's start) and lists the week's refused PRs (closed without a merge) and reworked ones (merged after a review requested changes). Pass the repo's `--incident-label`, `--environment`, and `--bot` as for metrics.py
2. Read what the numbers don't hold: the review comments on the refused and reworked PRs, for findings that recurred across two or more PRs; and the week's stuck states, from this week's watch reports, or a `watch_state.py` run when there are none (a state that held across passes, such as BOT_STALLED or PROMOTION_DUE, is stuck)
3. Propose at most three changes, each the smallest one that removes a recurring cause: a skill edit, a check (a test, a lint, a `watch_state.py` state), or a decision. A week with nothing recurring proposes nothing
4. Deduplicate: `scripts/retro.py dedupe <owner/repo> --title "<title>" ...` checks each proposal's title against the open issues. A DUPLICATE gets a comment on that issue only when the week adds evidence; a NEW one opens as an issue with the evidence and links
5. Post one short comment on the roadmap issue with `gh issue comment <n> --body-file <file>`: the metrics beside last week's, what recurred, and the proposals with their issue links

The retro writes only the proposal issues and that one comment. It never edits a skill, a check, or the decisions log itself; each proposal goes through triage like any issue.

## Running it on a schedule

- In a session: `/loop 30m /github-ship-watch <owner/repo> — watch and triage`
- In the cloud: `/schedule` a routine whose prompt clones shipyard and follows this file, as `docs/flow.md` shows. A cloud session may not load plugins from the repo's settings
- Don't start a loop or a routine unasked; offer it

## Metrics

`scripts/metrics.py <owner/repo> [--days 30] [--until TIME]` measures the line over a window from what GitHub already holds: the four DORA measures (deploy frequency, lead time for changes, change failure rate, time to restore) plus issue to release (an issue opened to its "Released in" notice), human touch (merges a person merged or approved), and agent share (merges whose own work says an agent made them). Run it with `uv run --no-project python`; it changes nothing. `--json` is for a dashboard, `--markdown` for a post. The script's docstring defines each measure; the choices that matter when reading it:

- A repo that has never deployed counts stable GitHub releases, and its change failures are `release-blocker` issues plus hotfix releases (on a `release/X.Y` branch). Pass `--environment` to count only the environments that matter (a `github-pages` deployment counts otherwise), and `--incident-label` when `[operate] incident_label` isn't `incident`
- Lead time ends at the first stable release whose tag adds the PR's merge commit over the previous stable version, from GitHub's compare (one query per release in the window). It misreads one backport shape: a patch cut from main before the window, when the previous version is a later hotfix on a `release/X.Y` branch without it, has its commits credited to the next minor release (the docstring has the example)
- Human touch reads the merge actor and approvals: an app is a bot, a `[bot]` login is a bot, and `--bot <login>` names a machine user. An agent merging with a person's token counts as that person
- Agent share reads what the work itself says, not who clicked merge: a PR is agent-made when its body carries Claude Code's "Generated with [Claude Code]" footer or a commit carries a `Co-Authored-By: Claude` or `Claude-Session:` trailer (case-insensitive; add another agent's mark to `AGENT_MARKS`). Its detail counts the agent-made merges a person approved in a GitHub review, the human gate. Where agents merge with the owner's token, human touch reads 100% and agent share is the number to watch
- A measure with nothing to measure says "no data", never 0. A 0% change failure rate means releases shipped and none failed

The weekly metrics post is an option the user schedules, never a default: `/schedule` a weekly routine that runs `metrics.py <repo> --markdown` and posts the table as one comment on the roadmap issue the user names (`gh issue comment <n> --body-file <file>`). Posting is the routine's only write.

## Improve the skill

When a pass misses something stuck, or repairs something it shouldn't have, add a state to `watch_state.py` (with a test in shipyard's `tests/test_ship_watch.py`) or a rule here.
