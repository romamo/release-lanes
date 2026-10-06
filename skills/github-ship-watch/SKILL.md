---
name: github-ship-watch
description: Watch a GitHub repo's issue-to-release pipeline on a loop or a schedule, and finish what its standing policy already decided. Reports a failed or stalled release bot, a release missing from PyPI, fixed issues not yet told which version shipped them, issues that triage owes, and, for a repo running shipmill operate, a failed operate run, an unhealthy environment, a promotion waiting on approval, open incidents, a closed incident with no postmortem, and a hold; then reruns a flaky release job, starts a stalled lane, posts the shipped notices, and hands new issues to github-issue-triage when asked. Drafts a due postmortem as a pull request, and runs a weekly retro when the user schedules one. Use when the user asks to "watch the repo", "keep it shipping", "babysit releases", "check the release went out", sets up /loop or /schedule for a repo, or wants one report across several repos ("watch the fleet"). Not for triaging a backlog by hand (github-issue-triage), landing PRs (github-pr-triage), or setting up shipmill (shipmill-setup).
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
5. **An open `shipmill-hold` issue stops the factory on purpose.** A person pulled the stop switch: report the hold (the HOLD row: who opened it and when), never start a lane or close the issue. A plan under the hold, or under `[autonomy] release = "propose"`, proposes instead of releasing, so it never reads as BOT_STALLED; the proposal issue is for a person to act on
6. **The watch never deploys, promotes, or rolls back.** `shipmill operate` acts on environments under the repo's autonomy; the watch reports what operate found and repairs only a stuck operate run, as it does for the release bot. Approving a proposal is the user's call: give them the command

## The script

`scripts/watch_state.py <owner/repo> [--repo-dir PATH]` answers the whole status question deterministically. Run it with `uv run --no-project python` (or `python3`, 3.10+) at the start of every pass and again before the report. Exit 0 means nothing needs action; 1 means a row below is an action; 2 is a git, gh, uvx, or PyPI failure (report it and stop).

| State | Repair under "watch" |
|---|---|
| INCIDENT_OPEN | Report first: the issue, how long it has been open, and whether a PR links it to close it. Recommend landing the linked hotfix PR, or writing one when none links it. Rollback is operate's (or the user's), never the watch's |
| HOLD | Report next: the issue, who opened it, and when. Not an action (exit 0 by itself); see hard rule 5 |
| POSTMORTEM_DUE | An incident closed as completed (not as not planned or a duplicate) and no `docs/postmortems/*.md` on the default branch names it. Draft the postmortem as a pull request ([Postmortems](#postmortems)), once per incident: an open PR that already names it means the draft waits on the maintainer, so report the PR instead |
| OPERATE_FAILED | The latest run of the workflow calling shipmill's `operate.yml` failed. Read it; rerun a flaky or transient failure once (`gh run rerun <id> --failed`), as for BOT_FAILED. Otherwise report the failing step with the run link |
| UNHEALTHY | An environment's newest `shipmill health` status is a failure. Report the environment, the release, and the check's error. Operate owns the rollback; the watch never deploys over it |
| PROMOTION_DUE | A deploy waits on a person. With a proposal issue, report it with its approve command (`gh workflow run operate.yml -f approve=<environment> -f dry-run=false`; under a hold, the hold closes first). Without one, operate would promote the environment (deploy autonomy `act`), or propose it (`propose`, or a hold), but isn't running on a schedule, so no proposal issue opens: report what operate would do, why it isn't running, and the command that runs it once (or `shipmill init --operate` when no caller exists). Never run either yourself |
| BOT_FAILED | Read the run. If the failure is a known flaky test or a transient push or network error ([github-pr-triage's ci-failures.md](../github-pr-triage/references/ci-failures.md)), `gh run rerun <id> --failed` once. Otherwise report the failing step and its error, with the run link |
| BOT_STALLED | A shipmill bot has a release due and nothing running. Start the lane the plan names: `gh workflow run release.yml -f lane=<lane> -f dry-run=false`. Never `lane=policy` by hand: it skips while main isn't quiet yet |
| WORK_BRANCH_STALE | A `shipmill/v*` work branch is on origin and no run of the release workflow is queued or in progress, so no run owns it: a run was cancelled before its cleanup job got a runner, and every later run stops at the branch (with a "Work branch held" warning) unless its prepare job grants `actions: read`. Check again that no run of the workflow the row names is unfinished: every status `gh run list -w <workflow> --json status --jq '.[].status'` prints is `completed` (a run waiting on an environment still uses the branch). Then delete it only at the commit the row names: `git ls-remote origin refs/heads/<branch>` gives the full `<commit>`, which must start with the row's, and `git push --force-with-lease=refs/heads/<branch>:<commit> origin :refs/heads/<branch>` deletes it and start the lane the plan names, as for BOT_STALLED (hard rules 4 and 5 still hold); with no BOT_STALLED row, the next run releases it. When the caller's prepare job lacks `actions: read`, recommend adding it, so runs replace such a branch themselves |
| NOT_PUBLISHED | Find the publish run for the tag (`gh run list -w <publish workflow> --branch <tag>` or by the release's dispatch). Rerun its failed jobs once if the cause is flaky; a failed release check or build is reported, not retried |
| PUBLISHING | Nothing; the next pass checks again |
| PUBLISHED | Once per release, check that a clean install runs (github-pr-triage's [landing.md](../github-pr-triage/references/landing.md#ecosystems)); retry once on index lag |
| UNANNOUNCED | Post the notices: `../github-pr-triage/scripts/shipped.py <repo> <prev> <tag> --install '<install command>' --post`. Show the plan first if this repo has never had notices |
| ISSUES | Under "watch and triage", run github-issue-triage on the flagged issues. Otherwise list them |
| WORKTREE_STALE | A shipmill worktree (under `.claude/worktrees/` or `tmp/wt-*`) that `shipmill worktrees` keeps, created over 7 days ago: the subject is its path, the detail why it is kept. The gate's prune removes only what landed, so this is work a person finishes (commit, push, open the PR), or removes by hand once it is unwanted (`git worktree remove <path>`). Report only, never remove it from the watch. Not an action (exit 0 by itself) |
| PRS_OPEN, BOT_OK, BOT_NONE, NO_REGISTRY | Report only |

`--json` prints one JSON object per row: `state`, `subject`, `detail`, and `agent`, which says whether the row needs an agent. `shipmill gate` starts a session only on `agent: true` rows (plus PRS_OPEN with `[agents] prs = true`), and refuses a row without the field. The script's `AGENT` set decides it, next to this table; change both together:

| `agent` | States | Why |
|---|---|---|
| `true` | BOT_FAILED, BOT_STALLED, WORK_BRANCH_STALE, NOT_PUBLISHED, UNANNOUNCED, ISSUES | A repair above an agent does: a rerun, a lane start, a stale work branch deleted, the notices, triage |
| `true` | OPERATE_FAILED | An agent reruns a flaky operate run or reports the failure |
| `true` | INCIDENT_OPEN | An agent works the incident: diagnosis, then a hotfix or a revert |
| `false` | PROMOTION_DUE | Only a person approves; an agent would wake every retry window for nothing |
| `false` | UNHEALTHY | Operate owns it and rolls back after `rollback_after` failures; past that, the incident it opens reads INCIDENT_OPEN |
| `false` | HOLD | Report only; the gate checks `shipmill-hold` itself before anything else (D-15) |
| `false` | WORKTREE_STALE | Only a person knows whether kept work is wanted; the gate's prune already removes what landed |
| `false` | PRS_OPEN, POSTMORTEM_DUE, and every other state | Report only, or a repair the watch pass makes itself |

Pass `--grace` to give a slow publish more minutes before it reads NOT_PUBLISHED, and `--tool` when the shipmill bot isn't installed from `shipmill/shipmill@v0`.

WORKTREE_STALE shows only for a shipmill bot: the script runs `uvx --from <tool> shipmill --repo <repo-dir> worktrees --json` after the plan and never reports the main checkout, the `--repo-dir` checkout, or a worktree outside `.claude/worktrees/` and `tmp/wt-*`. That command reads `claude agents --json` for the live sessions, so the watch needs `claude` on `PATH` for such a repo, and a failing `shipmill worktrees` stops the watch with exit 2, as a failing plan does.

HOLD and POSTMORTEM_DUE show for every repo with a shipmill config, since a hold stops releases too and an incident may be labelled by hand. POSTMORTEM_DUE reads the postmortems through the GitHub contents API on the default branch, so a draft counts only once merged. The operations states show only when the config declares environments (read with `tomllib`; on Python 3.10 only plain `[environments.<name>]` tables, and any other form stops the watch with a one-line message); they come from the GitHub deployments and issues `shipmill operate` writes. The incident label is `[operate] incident_label`, `incident` by default; `--incident-label` takes its place (the fleet report passes a fleet entry's label this way).

## Report

One line when exit 0: "Nothing owed: bot OK, <latest tag> published and announced, no issues waiting." (add "held by #N" when a HOLD row shows). Otherwise lead with open incidents and holds, then what needs the user (each with a recommendation), then what the pass repaired with links, then what it left for the next pass. Under /loop, keep quiet passes to that one line.

## Postmortems

Every closed incident gets a postmortem: a file `docs/postmortems/YYYY-MM-DD-<slug>.md` (the day the incident opened) from shipmill's [`docs/postmortems/TEMPLATE.md`](../../docs/postmortems/TEMPLATE.md), merged through a pull request; the first postmortem copies the template into the repo too. The watch drafts it; the maintainer approves it by merging, and nothing merges without the usual gates.

1. Read the record: the incident issue and its comments (`gh issue view <n> --json title,body,createdAt,closedAt,comments`), and the deployment statuses of the environment it names (`gh api repos/<repo>/deployments?environment=<env>`, then each deployment's `statuses`) from the release before the fault to the recovery
2. Draft from the record only: the timeline in UTC from those comments and statuses, the cause and the change that introduced it, what caught it, what would have caught it sooner, and the actions. Keep one `Incident: <owner/repo>#<n>` line per incident the postmortem covers (the issue's URL, or `#<n>` for the repo itself, reads the same); the watch reads that line. Mark anything the record doesn't show as a question for the maintainer, never a guess
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
- In the cloud: `/schedule` a routine whose prompt clones shipmill and follows this file, as `docs/flow.md` shows. A cloud session may not load plugins from the repo's settings
- Don't start a loop or a routine unasked; offer it

## Metrics

`scripts/metrics.py <owner/repo> [--days 30] [--until TIME]` measures the line over a window from what GitHub already holds: the four DORA measures (deploy frequency, lead time for changes, change failure rate, time to restore) plus issue to release (an issue opened to its "Released in" notice), human touch (merges a person merged or approved), and agent share (merges whose own work says an agent made them). Run it with `uv run --no-project python`; it changes nothing. `--json` is for a dashboard, `--markdown` for a post. The script's docstring defines each measure; the choices that matter when reading it:

- A repo that has never deployed counts stable GitHub releases, and its change failures are `release-blocker` issues plus hotfix releases (on a `release/X.Y` branch). Pass `--environment` to count only the environments that matter (a `github-pages` deployment counts otherwise), and `--incident-label` when `[operate] incident_label` isn't `incident`
- Lead time ends at the first stable release whose tag adds the PR's merge commit over the previous stable version, from GitHub's compare (one query per release in the window). It misreads one backport shape: a patch cut from main before the window, when the previous version is a later hotfix on a `release/X.Y` branch without it, has its commits credited to the next minor release (the docstring has the example)
- Human touch reads the merge actor and approvals: an app is a bot, a `[bot]` login is a bot, and `--bot <login>` names a machine user. An agent merging with a person's token counts as that person. With `[agents] app_id` set, the gate's sessions merge and review as the GitHub App's `<slug>[bot]`, which counts as a bot without `--bot`, so the metrics tell agent work from the maintainer's (spec 004)
- Agent share reads what the work itself says, not who clicked merge: a PR is agent-made when its body carries Claude Code's "Generated with [Claude Code]" footer or a commit carries a `Co-Authored-By: Claude` or `Claude-Session:` trailer (case-insensitive; add another agent's mark to `AGENT_MARKS`). Its detail counts the agent-made merges a person approved in a GitHub review, the human gate. Where agents merge with the owner's token, human touch reads 100% and agent share is the number to watch
- A measure with nothing to measure says "no data", never 0. A 0% change failure rate means releases shipped and none failed

The weekly metrics post is an option the user schedules, never a default: `/schedule` a weekly routine that runs `metrics.py <repo> --markdown` and posts the table as one comment on the roadmap issue the user names (`gh issue comment <n> --body-file <file>`). Posting is the routine's only write.

## Fleet

A maintainer with several shipmill repos reads one report instead of one per repo, so an incident or a stalled release in one product doesn't hide among the others. The fleet report only reads: each repo's own watch pass still repairs.

A fleet file lists the repos, in TOML:

```toml
[[repos]]
repo = "shipmill/shipmill"

[[repos]]
repo = "owner/other"
incident_label = "sev"   # optional; the label that repo's incidents carry (default: its config's)
```

`scripts/fleet.py report --fleet <file>` (run it with `uv run --no-project python`, 3.10+) clones each repo afresh (blobless, in a temporary folder it removes), runs `watch_state.py` on the clone, and prints one table: the repo, then each row's state, subject, and detail as `watch_state.py` prints them. The action rows of every repo come first, then the report-only rows. Exit 0 means no repo needs action, 1 that a row is an action, 2 that a repo's check failed or the fleet file is malformed.

- `--metrics` adds `metrics.py`'s measures for each repo over the same 30 days, side by side, one column per repo; "no data" stays no data. A repo's incidents carry its fleet entry's `incident_label`, else its config's `[operate] incident_label`, else `incident`: the watch and the metrics use the same label, read from the clone with `watch_state.py`'s reader
- `--json` prints the same report as one JSON object: the repos, each with its rows, and its metrics with `--metrics`
- A repo whose check fails (not found, no access, a gh error) gets one REPO_ERROR row naming the failed step and the error's first line; the other repos are still reported, and the run exits 2 after printing everything
- The fleet file is refused (exit 2, naming the file and the problem) when it is missing, isn't TOML, has no `[[repos]]`, has an entry without `repo`, has a key other than `repo` and `incident_label`, names a repo not in `owner/name` form, or lists a repo twice. On Python 3.10 only the plain form above is read
- An action row is handed to that repo's own watch pass: run this skill on the repo, with the user's scope words

The fleet watch is an option the user schedules, never a default. Keep the fleet file in a repo the routine clones (for example `.github/fleet.toml` in the user's ops repo), then `/schedule` a routine whose prompt is:

```text
Clone shipmill/shipmill and <the repo holding the fleet file>. Run
`uv run --no-project python skills/github-ship-watch/scripts/fleet.py report --fleet <path to fleet.toml> --metrics`.
On exit 0, report one line: "Fleet: nothing owed". Otherwise report the incidents and
holds first, then every action row with its repo and a recommendation, then each
REPO_ERROR, then the metrics table. Change nothing: a repo that needs repairs gets its
own github-ship-watch pass.
```

In a session, `/loop 1h` the same report.

## Improve the skill

When a pass misses something stuck, or repairs something it shouldn't have, add a state to `watch_state.py` (with a test in shipmill's `tests/test_ship_watch.py`) or a rule here.
