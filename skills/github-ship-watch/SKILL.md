---
name: github-ship-watch
description: Watch a GitHub repo's issue-to-release pipeline on a loop or a schedule, and finish what its standing policy already decided. Reports a failed or stalled release bot, a release missing from PyPI, fixed issues not yet told which version shipped them, and issues that triage owes; then reruns a flaky release job, starts a stalled lane, posts the shipped notices, and hands new issues to github-issue-triage when asked. Use when the user asks to "watch the repo", "keep it shipping", "babysit releases", "check the release went out", or sets up /loop or /schedule for a repo. Not for triaging a backlog by hand (github-issue-triage), landing PRs (github-pr-triage), or setting up shipyard (shipyard-setup).
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

1. **Finish decisions; never make new ones.** A watch pass doesn't merge, tag by hand, change a release policy, close or relabel issues, or edit code. Those belong to the user or to the other skills under the user's words
2. **One repair per finding per pass.** Rerun a failed job once. If it fails again, the next pass reports it rather than looping on reruns
3. **Read state right before acting.** Another session may be landing PRs or releasing in the same repo. Check `gh run list` before starting a lane, and follow a peer's hold (see github-pr-triage, hard rule 5)
4. **A repo with `release-blocker` open is held on purpose.** A stalled bot behind a blocker isn't stalled: report it, don't start a lane

## The script

`scripts/watch_state.py <owner/repo> [--repo-dir PATH]` answers the whole status question deterministically. Run it with `uv run --no-project python` (or `python3`, 3.10+) at the start of every pass and again before the report. Exit 0 means nothing needs action; 1 means a row below is an action; 2 is a git, gh, uvx, or PyPI failure (report it and stop).

| State | Repair under "watch" |
|---|---|
| BOT_FAILED | Read the run. If the failure is a known flaky test or a transient push or network error ([github-pr-triage's ci-failures.md](../github-pr-triage/references/ci-failures.md)), `gh run rerun <id> --failed` once. Otherwise report the failing step and its error, with the run link |
| BOT_STALLED | A shipyard bot has a release due and nothing running. Start the lane the plan names: `gh workflow run release.yml -f lane=<lane> -f dry-run=false`. Never `lane=policy` by hand: it skips while main isn't quiet yet |
| NOT_PUBLISHED | Find the publish run for the tag (`gh run list -w <publish workflow> --branch <tag>` or by the release's dispatch). Rerun its failed jobs once if the cause is flaky; a failed release check or build is reported, not retried |
| PUBLISHING | Nothing; the next pass checks again |
| PUBLISHED | Once per release, check that a clean install runs (github-pr-triage's [landing.md](../github-pr-triage/references/landing.md#ecosystems)); retry once on index lag |
| UNANNOUNCED | Post the notices: `../github-pr-triage/scripts/shipped.py <repo> <prev> <tag> --install '<install command>' --post`. Show the plan first if this repo has never had notices |
| ISSUES | Under "watch and triage", run github-issue-triage on the flagged issues. Otherwise list them |
| PRS_OPEN, BOT_OK, BOT_NONE, NO_REGISTRY | Report only |

Pass `--grace` to give a slow publish more minutes before it reads NOT_PUBLISHED, and `--tool` when the shipyard bot isn't installed from `romamo/shipyard@v0`.

## Report

One line when exit 0: "Nothing owed: bot OK, <latest tag> published and announced, no issues waiting." Otherwise lead with what needs the user (each with a recommendation), then what the pass repaired with links, then what it left for the next pass. Under /loop, keep quiet passes to that one line.

## Running it on a schedule

- In a session: `/loop 30m /github-ship-watch <owner/repo> — watch and triage`
- In the cloud: `/schedule` a routine whose prompt clones shipyard and follows this file, as `docs/flow.md` shows. A cloud session may not load plugins from the repo's settings
- Don't start a loop or a routine unasked; offer it

## Improve the skill

When a pass misses something stuck, or repairs something it shouldn't have, add a state to `watch_state.py` (with a test in shipyard's `tests/test_ship_watch.py`) or a rule here.
