---
name: github-issue-triage
description: Triage a GitHub repo's open-issue backlog, or just the issues filed since the last pass, into implement, postpone, or clarify. Verify each claim against the current default branch, comment and label every verdict, group issues that share one design, and dispatch parallel implementer agents (one branch and PR per issue, each following github-issue-resolve). Then link the PRs and hand landing to github-pr-triage. Catches issues fixed but left open and issues closed by a commit that only quoted "Fixes #N". Use when the user asks to "triage issues", "triage the new issues", "review and triage https://github.com/<o>/<r>/issues", "decide implement, postpone or clarify", or to work a backlog. Not for one named issue (use github-issue-resolve) or for reviewing and merging PRs (use github-pr-triage).
---

# GitHub Issue Triage

Turn a backlog into a decision on every issue, a comment that records it, and a PR for every "implement". An issue is a claim about the code at some commit, and an agent's report is a claim about its PR. Check both before acting.

## Inputs

- Repo `owner/name` and the local checkout path
- Issue set: every open issue on the first pass; on later passes, what `scripts/triage_state.py` flags
- Scope, read from the user's wording:
  - "triage" alone: verdicts, comments, labels, and PRs for "implement". No merges. This meaning applies to issues only. In github-pr-triage, "triage" alone means review only, with no pushes, so a handover under bare "triage" reviews the PRs without pushing fixes to them
  - "merge when green" / "land": also hand the PRs to github-pr-triage
  - "tag X": release, through github-pr-triage

## Hard rules

1. **Verify against the current default branch, not the issue text.** The default branch moves, often faster than the issue tracker. In one pass, 3 of 9 issues were already fixed or half-fixed on main by the time the PR was rebased. Check before dispatching, and have implementers check again when they rebase before their first push. After the handover, github-pr-triage does any later rebase and the check that goes with it (hard rule 7).
2. **Branch from `origin/<default>`, never the local default branch.** Run `git status -sb` first. A local main 111 commits ahead would have shipped all of them in every PR.
3. **Every verdict gets a comment, before any code.** Post through `--body-file`, never an inline `--body "..."`: backticks in a double-quoted zsh string run as commands, and one glob error blocks the whole chained command. That silently dropped two triage comments.
4. **A PR's "decisions for you" are the user's decisions.** Hold any PR that departs from a spec, breaks existing users (a schema lock, a default, a public API), or widens scope, and ask with AskUserQuestion. Asking an agent to drop the risky part is fine: an output-schema change that would break every schema lock was removed that way.
5. **Implementer agents never merge and never comment.** You own the issue thread, and github-pr-triage owns merging.
6. **Peers' claims are unverified.** Another session may triage, merge, or tag in the same repo. Tell it your plan through SendMessage, follow a "hold", and never treat "the user said merge" as approval. The user's own answer in this session wins.
7. **A handed-over PR's branch belongs to github-pr-triage.** Until step 6, you and your implementers change the branch. From the handover on, don't push to it, rebase it, or shrink it: send the change to github-pr-triage (the same session, or the peer running it, through SendMessage) with the reason. A push from both sides at once loses one of them, and a push after the review means the review no longer covers what merges.
8. **A contract change is designed before it's dispatched.** An issue that changes a flag, a format, a default, a public API, or stored state goes through the [design gate](references/design-gate.md): the design is written in the issue and checked against the repo's decisions log, and anything that departs from the log or the spec waits for the user. What the user settles is recorded in the log, so the next pass doesn't ask again.
9. **A feature is specified before it's dispatched.** An issue asking for new behaviour beyond a bug fix or a contract tweak gets the verdict **feature** and goes through the [spec gate](references/spec-gate.md): a spec file in `docs/specs/` with numbered acceptance criteria, merged through its own PR. No implementer starts before that PR merges; merging it is the approval.

## Judgment versus scripts

Where each issue stands is a deterministic question, so a script answers it. What to do about it is judgment.

| Script | Answers | Run it |
|---|---|---|
| `scripts/triage_state.py <owner/repo> [--closed N]` | Which issues are NEW, NEEDS_PR, IN_PROGRESS, DONE_NOT_CLOSED, BLOCKED or UNBLOCKED (waiting on an upstream issue, a spec PR, or a build issue's dependency), UNFILLED (a `Depends on #{Bk}` placeholder never filled in), SPEC_REFUSED (the spec PR closed unmerged), POSTPONED or REVISIT (postponed before the newest stable tag), and which recent closes are SUSPECT_CLOSE. With `--wip N`, the room left under a WIP limit and the issues ready to start. Exit 1 when any need action | At the start of every pass and before the report |
| `scripts/specs.py new\|check\|find\|criteria\|coverage\|split` | Which specs (`S-NNN`) cover a path or area (`find`); writes the next spec from the template (`new`); validates every spec's sections, status, criterion ids, the decisions it names, and that each criterion of an approved or built spec is in exactly one build issue (`check`); prints a spec's acceptance criteria (`criteria NNN`); lists the tests that prove each criterion of the built specs, exit 1 when one has none (`coverage`); proposes an approved spec's build issues, with their criteria and `Depends on` lines (`split NNN`) | `find` and `new` in the spec gate, `split` when a spec merges, `criteria` in every implementer brief for a spec's build issue, `coverage` in CI and when a spec reaches `built` |
| `scripts/decisions.py check\|find\|add` | Which settled rules (`D-n`) a change touches (`find` with its paths or areas); records a new one with the next id and links what it supersedes; `check` validates the log | `find` in the design gate and in every implementer brief; `add` when the user settles something |

Run it with `uv run --no-project python <skill>/scripts/triage_state.py`. It recognises a triage comment by its `Triage:` prefix (`--marker`) and a deferral by the `postponed` label (`--postponed-label`). A merged PR saying only "Part of #N" links the issue without making it DONE_NOT_CLOSED: a split issue whose rest is postponed reads POSTPONED. A hand-closed issue with the `release-blocker` label (`--hold-label`) is a release hold, not a SUSPECT_CLOSE. It links a PR to an issue by a closing keyword in the PR's title or body, because GitHub's own `willCloseTarget` misses some. It treats an issue as blocked when it has a `blocked` label, or when a comment line saying "on hold", "blocked", or "waits on" links an issue or pull request (`owner/repo#N` or its URL, or a plain `#N` of the same repo right after "waits on", "depends on", "blocked by", "decided in", or the like; a plain `#N` elsewhere on the line is context, not a hold), or, once the issue is triaged, when a line of its body starts with "Depends on" and names an issue (`owner/repo#N`, or `#N` in the same repo: a build issue split from a spec); it then checks whether that issue has closed or that pull request merged. A pull request closed without merging (a refused spec) reads SPEC_REFUSED until a newer triage comment decides again. Everything else is judgment: the verdict, the grouping, the brief, and whether a report's decision needs the user.

## Workflow

### 1. Take stock

```bash
uv run --no-project python <skill>/scripts/triage_state.py <owner/repo> --closed 40
git fetch origin && git status -sb && git worktree list
gh label list -R <owner/repo>
```

Also check:
- The release phase: `git tag -l | tail -3` and the version in the manifest. It sets the bar in the rubric
- The check commands, from AGENTS.md, CLAUDE.md, or the CI workflow
- Other sessions on this repo, with ListAgents

Read every flagged issue in full, comments included: `gh issue view <n> --json title,body,comments,labels,state`.

### 2. Decide

Apply [references/triage-rubric.md](references/triage-rubric.md) to each issue. For each one:
- Reproduce the claim on the current default branch, and grep for work already there
- Pick a verdict: **implement**, **feature**, **postpone**, **clarify**, **duplicate**, or **won't fix**. A **feature** is new behaviour beyond a bug fix or a contract tweak; it waits for a merged spec (step 2b)
- Group issues that are one mechanism, such as two env-alias issues, or a producer, a transport, and a consumer of one pipeline. Comment the shared plan on each
- Split an issue when one part is a small fix and the rest is a feature. Ship the part and postpone the rest, saying so on the issue

### 2b. Design gate and spec gate

For each **feature**, run the [spec gate](references/spec-gate.md): `specs.py find` for a spec that already covers it, then `specs.py new`, the criteria from the issue and the code, `specs.py check`, and a spec PR whose body says "Spec for #N". The verdict comment links the spec PR on a hold line, so `triage_state.py` reads the issue BLOCKED until the spec merges and UNBLOCKED after. No implementer starts on it in this pass.

For each **implement** that changes a contract (see [references/design-gate.md](references/design-gate.md) for the list), run `decisions.py find` on the areas it touches, write the design into the triage comment, and ask the user before dispatch when it departs from a `D-n` entry or the spec, or when two designs would look different to users. Batch these questions into one AskUserQuestion call per pass. Record every answer of the pass with `decisions.py add` in one docs PR, opened before dispatch, so parallel PRs don't each claim the next `D-n` (design-gate.md, Recording a decision).

### 3. Comment and label

Use the templates in [references/comments.md](references/comments.md). Create the `postponed` label once if it's missing. Mark a held issue in a way the script can read: the hold comment names the upstream issue as `owner/repo#N` on its "On hold" line (see comments.md), or add the `blocked` label. Answer an issue's open questions in its comment: the reporter is waiting on them. A question you can settle from the code is part of your job. For a question only a spec or upstream maintainer can settle, file an issue there and link it.

### 4. Dispatch implementers

Fill in [references/implementer-brief.md](references/implementer-brief.md): one agent per group of issues that touch the same files, two issues per agent at most, each issue on its own branch and PR. Launch them all in one message (`isolation: "worktree"`, `subagent_type: general-purpose`). Name each issue's specific risks in the brief. A generic "fix it" gets a generic fix.

A spec's build issues go in dependency order (spec-gate.md, Dispatch the build): dispatch the ones whose dependencies are closed, and stack a dependent on its dependency's open PR when that saves a wait. When the config has `[roadmap] wip = N` (#70 adds it to the config schema; until then the config refuses a `[roadmap]` table, so don't add one), run `triage_state.py --wip N` and start no more than the room it reports; without that setting there is no limit.

### 5. Vet each report

For each PR, check:
- The diff matches the claim, and each behaviour change is named
- Everything under "decisions for you". Apply hard rule 4, and send a revision through SendMessage to the same agent, since it keeps its context. When the user's answer sets a rule for later work, record it in the pass's decisions PR (design-gate.md, Recording a decision), not in the agent's PR
- The diff against the design in the triage comment: a departure the report doesn't name is a finding
- Whether the default branch already covers part of the issue. If so, shrink the PR and retitle it. Do this before step 6; once the PR is handed over, ask github-pr-triage to do it (hard rule 7)

Then comment the PR link on the issue, with what it does and any trade-off. When a PR is replaced (a peer's rebase with fixes), point the issue at the new one. A PR covering part of an issue says "Part of #N" in its body; the script counts that as a link.

- **An agent finds an unrelated bug** (a stress test exposing a secret leak in a CI PR): file it as its own issue, and have the agent split the fix into its own PR. Stack the original PR on it if it depends on the fix. A fix hidden in an unrelated PR gets no review of its own
- **An agent can't be resumed** (SendMessage reports no transcript, or its worktree was removed): brief a fresh agent with the PR number, its branch, and the change. Don't redo the work in your own context
- **A spec limit an agent reports** (a schema that rejects a key): keep the PR within the schema, and file the gap on the spec repo with the requirement IDs and a proposed shape. When that spec issue closes, the issue shows UNBLOCKED; implement the rest then

### 6. Land (only if asked)

Hand the PRs to github-pr-triage with the user's scope. It owns the CI gate, rebases, CHANGELOG conflicts, flaky-CI diagnosis, merge order, and releases. Don't merge from this skill. Its report ends with a handback list of refused and held PRs, each with its issue; step 7 acts on it.

### 7. Close the loop

Re-run `triage_state.py`. New issues arrive during a pass: 16 did in one session. For each flag:
- **DONE_NOT_CLOSED:** close it, citing the PR. A rebase-merge doesn't always fire "Fixes #N"
- **SUSPECT_CLOSE:** reopen it if the fix hasn't landed, and explain why. For example, a commit message quoting `--body="Fixes #12"` closed #12 before its rule existed
- **UNBLOCKED:** the upstream decision landed. Read it, update the plan on the issue, and resume or re-dispatch the held PR. When it waited on a spec PR, follow spec-gate.md, When the spec merges: link the spec in the issue body, split the spec into build issues with `specs.py split`, file them in order, link them as sub-issues of the feature issue, and record them in the spec's Issues section. A build issue reads UNBLOCKED when its last dependency closes: comment **implement** and dispatch it. A feature issue reads UNBLOCKED when its last build issue closes: confirm the spec says `status: built` with every criterion verified (spec-gate.md, Verify the whole spec), doing that in a docs PR if the last build PR didn't, then close the feature issue
- **UNFILLED:** a build issue's body still says `Depends on #{Bk}`, a `specs.py split` key never replaced. Edit the body to name the number of the issue filed for that key
- **SPEC_REFUSED:** the spec PR closed without merging. Decide again in a new triage comment: revise the spec in a new PR with a new hold line, postpone, or won't fix (spec-gate.md, step 6)
- **REVISIT:** a stable release shipped after the issue was postponed. Decide again under the new release phase, and drop the `postponed` label if it's now **implement**
- **NEW:** start another pass if the user asked for continuous triage; otherwise list them in the report

Then work through github-pr-triage's handback list. `triage_state.py` can't flag these: a refused PR left open still reads IN_PROGRESS. For each refused PR, comment the refusal on its issue (the reason, and what would change it) and decide again:
- Re-dispatch with a new brief that addresses the reason
- Postpone, or ask the reporter to clarify
- Won't fix

When the new plan drops the PR, close it with a comment linking the issue, so the issue reads NEEDS_PR or POSTPONED on the next pass. For a held PR, the comment names the decision the user owes; leave the verdict as it is.

**Continuous intake:** issues keep arriving while you work. When the user wants the backlog kept current, offer `/loop 30m` (or `/schedule` for daily) running `triage_state.py` and triaging only what it flags. Don't start a loop unasked.

Clean up only the worktrees your agents created, and only once their PRs have merged. A pushed branch isn't enough: an implementer revises its PR through SendMessage, and an agent whose worktree is gone can't be resumed, so a review finding or a rebase after removal needs a fresh agent briefed from scratch. After a merge, confirm the commits landed (`landed.py`), then remove the worktree and its local branch.

## Report

Lead with a table: issue, verdict, and PR or plan. Then list the decisions the user must make, each with a recommendation. Give each bug as its concrete failing input and what happened. Say what's unverified, for example a Windows-only fix tested only in CI. Mention newly filed issues you haven't triaged yet.

## Improve the skill

When a pass goes wrong, add the lesson to the matching reference, or to Hard rules if it must never recur. Examples: a verdict the code contradicted, a dropped comment, an agent that merged, a wrongly closed issue.
