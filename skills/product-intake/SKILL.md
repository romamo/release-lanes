---
name: product-intake
description: Turn a GitHub repo's product feedback into opportunity issues the maintainer accepts or declines. Groups feature requests, ideas, and discussions that ask for the same outcome (with thumbs-up reactions as demand) into one opportunity issue each, keeps it updated instead of opening a second, records a decline so the same request is never proposed again, and hands an accepted opportunity to github-issue-triage's spec gate. Use when the user asks to "group the feature requests", "run product intake", "what are users asking for", "turn feedback into opportunities", or "what should we build next". Not for bugs or contract changes (github-issue-triage) or for releases (github-ship-watch).
---

# Product Intake

Feedback in, opportunities out. Many requests ask for one outcome in different words; intake groups them into one **opportunity issue** that states the problem in the users' words, who it affects, the evidence, and what success looks like. The maintainer decides on the opportunity, not on each request: accept (label `planned`) or decline (close as not planned, with the reason in a comment). Nothing is built from an opportunity until it is accepted and its spec merges.

## Inputs

- Repo `owner/name` and a local checkout (for the config and the decisions log)
- Scope from the user's wording, read narrowly:
  - "status" or "what are users asking for": report only, change nothing
  - "intake" (the default): the report, plus the writes `[autonomy] intake` allows
  - "intake and triage": also hand ACCEPTED opportunities to github-issue-triage

## Autonomy

`[autonomy] intake` in `.github/shipyard.toml` sets how far a pass goes; the script prints it on its first line:

| Level | A pass |
|---|---|
| `observe` | Reports the groups it would make and writes nothing |
| `propose` (the default) | Opens and updates opportunity issues, comments on the requests it groups, and labels pure duplicates |

There is no `act`: shipyard refuses it, since an opportunity is a proposal by nature and only the maintainer accepts or declines one. An open `shipyard-hold` changes nothing here, as intake never acts.

## Hard rules

1. **Grouping is judgment; the states are facts.** `scripts/intake_state.py` says what is grouped and where each opportunity stands. Which requests ask for the same outcome is your call, made from the requests' own words
2. **One opportunity per outcome, updated in place.** Before opening one, read every opportunity the script lists, open and closed. A request that fits an existing one is added to its Evidence section; never open a second issue for the same outcome
3. **A declined outcome is never proposed again.** A new request for a declined outcome goes into the declined opportunity's Evidence section, and the request gets a comment with the recorded reason. Don't reopen it and don't open a new one: reconsidering is the maintainer's call, made by reopening the issue themselves
4. **Never accept, decline, or close for the maintainer.** Intake writes opportunities and comments; the `planned` label and the close are the maintainer's. Don't close requests either, a pure duplicate included: it gets the `duplicate` label and a link
5. **Bugs and contract changes aren't feedback.** The script already leaves out issues triage gave an implement, feature, duplicate, or won't fix verdict. If an untriaged issue is a bug, leave it to github-issue-triage rather than grouping it
6. **GitHub data only** (D-6's spirit): issues, discussions, and reactions. No external tracker, survey, or analytics source

## The script

`scripts/intake_state.py <owner/repo>` answers the status question. Run it with `uv run --no-project python` (or `python3`, 3.10+) from the repo's checkout, at the start of a pass and again before the report. Exit 0 means nothing needs action, 1 that a row below does, 2 a gh failure or malformed config (report it and stop).

| State | Means | Do |
|---|---|---|
| NEW_FEEDBACK | An open request or discussion no opportunity lists | Group it (Workflow, step 2) |
| OVERLAP | Listed by two opportunities that are open or declined | Keep it in the one that fits and remove it from the other's Evidence |
| ACCEPTED | Labelled `planned`, not yet handed to triage | Hand it to github-issue-triage's [spec gate](../github-issue-triage/references/spec-gate.md) (step 5) |
| NO_REASON | Closed as not planned with no comment | Ask the maintainer for the reason, and record it as a comment on the issue once they give it |
| OPPORTUNITY_OPEN | Waiting for the maintainer | Nothing; list it in the report with its evidence |
| HANDED_OFF | Accepted, and triage gave it a newer verdict | Nothing: triage and the spec gate own it |
| DECLINED | Closed as not planned; the note is the reason | Nothing, except rule 3 for new requests |
| DONE | Closed as completed | Nothing |
| MERGED | Closed as a duplicate of another opportunity | Check the surviving one lists its members; until it does they read NEW_FEEDBACK |

What the script decides for you, so the pass doesn't re-derive it:

- **Feedback** is an open issue with no skip label (`bug`, shipyard's own labels; `--skip-label` replaces the list) and no closing triage verdict, plus each open discussion outside Announcements when Discussions are on (`--category` names the ones to count)
- **A member** of an opportunity is any issue or discussion its `## Evidence` section links in this repo (`#N`, `owner/repo#N`, or a github.com link). Membership is what the opportunity's body says, so editing the Evidence section is how a request joins or leaves
- **Evidence** per opportunity: its member count, and the thumbs-up reactions on the members and on the opportunity itself, closed members included
- **The triage marker**: a comment starting `Triage:` with the verdict in bold. Intake's own comments use `Triage: **opportunity**`, so `triage_state.py` reads a grouped request or an opportunity as triaged instead of NEW. Never put the word "implement" in one: `triage_state.py` would read it as NEEDS_PR

`--label`, `--accepted-label`, and `--marker` rename `opportunity`, `planned`, and `Triage:` for a repo that uses other names. `--json` prints JSON lines, the first one `{"autonomy": ...}`.

## Workflow

### 1. Take stock

```bash
uv run --no-project python <skill>/scripts/intake_state.py <owner/repo>
gh label list -R <owner/repo>
```

Read each NEW_FEEDBACK item in full (`gh issue view <n> --json title,body,comments`, or the discussion page), and the body of every opportunity the script lists, closed ones included. Run `decisions.py find` (github-issue-triage's script) with the areas the requests touch: a request that breaks a settled `D-n` rule is still an opportunity, but its Problem section says which rule it runs into.

### 2. Group

Two requests belong together when they want the **same outcome** for the user, even if they propose different solutions. "Export as CSV" and "let me open the report in a spreadsheet" are one outcome; "faster export" is another. Prefer a group the maintainer can accept or decline in one sentence. For each NEW_FEEDBACK item:

- **Fits an open or accepted opportunity**: add it to that opportunity's Evidence
- **Fits a declined opportunity**: rule 3. Add it to the declined opportunity's Evidence, and comment the recorded reason on the request
- **Fits a done opportunity**: comment on the request that it shipped, naming the release when the opportunity says it; add it to the Evidence only if it asks for more than what shipped
- **A new outcome**: a new opportunity, even with one request behind it; the evidence count tells the maintainer how strong it is
- **Not a request** (a bug, a question, a support request): leave it for github-issue-triage, or label it `bug` when that is plainly what it is

### 3. Write

Under `propose`, write each group with the templates in [references/opportunity.md](references/opportunity.md), every body through `--body-file` in the repo's `tmp/`. Create the `opportunity` and `planned` labels once if they're missing. Then:

- Open the opportunity issue with the `opportunity` label, and comment intake's triage line on it
- Comment on each request it groups, linking the opportunity
- Add the `duplicate` label to a request only when it is a pure duplicate: the same outcome, nothing the opportunity doesn't already say

Updating an opportunity edits its body: add the request to Evidence, refresh the counts, and keep the rest unless the new request changes who is affected or what success means. Re-run the script; the grouped items now read as members.

Under `observe`, write nothing: put each proposed group in the report with its requests and evidence.

### 4. The maintainer's call

Accepting is the `planned` label, declining is closing the opportunity as not planned with the reason in a comment. When a reason is a rule rather than a one-off ("shipyard stays GitHub-only"), recommend recording it in the decisions log through the [design gate](../github-issue-triage/references/design-gate.md) (Recording a decision); the log entry, not the opportunity, is what later designs are checked against. Don't add the entry yourself unless the maintainer says so.

### 5. Hand an accepted opportunity to triage

An ACCEPTED opportunity is a **feature**: under "intake and triage", run github-issue-triage on it, which runs the spec gate. The opportunity issue becomes the feature issue: the spec PR's body says "Spec for #N" with the opportunity's number, and triage's **feature** comment with its hold line makes the script read it HANDED_OFF. The spec's Problem section starts from the opportunity's Problem and Evidence. Without "and triage", list it in the report as waiting for triage.

## Report

Lead with what needs the maintainer: the OPPORTUNITY_OPEN issues, strongest evidence first, each with its request count, thumbs-up, and a one-line recommendation (accept or decline, and why). Then what the pass wrote (opportunities opened and updated, requests linked), the NO_REASON issues, the ACCEPTED ones waiting for triage, and the feedback left ungrouped with the reason. On a quiet pass, one line: "No new feedback; N opportunities wait for your call."

## Improve the skill

When a pass groups badly (two outcomes in one issue, a request proposed again after a decline), add the lesson to step 2. When the script misreads a state, fix `intake_state.py` and add a case to shipyard's `tests/test_intake_state.py`.
