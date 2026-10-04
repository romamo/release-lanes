---
name: product-intake
description: Turn a GitHub repo's product feedback into opportunity issues the maintainer accepts or declines, and plan milestones from the accepted ones. Groups feature requests, ideas, and discussions that ask for the same outcome (with thumbs-up reactions as demand) into one opportunity issue each, keeps it updated instead of opening a second, records a decline so the same request is never proposed again, takes the requests triage hands over, and hands an accepted opportunity back to github-issue-triage's spec gate. Proposes the next milestone from accepted opportunities by evidence and effort within the config's work-in-progress limit and cadence, for the maintainer to approve. Use when the user asks to "group the feature requests", "run product intake", "what are users asking for", "turn feedback into opportunities", "what should we build next", "plan the next milestone", or "where is the roadmap". Not for bugs or contract changes (github-issue-triage) or for releases (github-ship-watch).
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
5. **Bugs and contract changes aren't feedback.** The script already leaves out issues triage gave an implement, feature, duplicate, or won't fix verdict. If an untriaged issue is a bug, leave it to github-issue-triage rather than grouping it. A request triage handed over with its **opportunity** verdict is feedback: group it like any other
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
| DECLINED | Closed as not planned; the note is the reason, the oldest comment from an hour before the close on | Nothing, except rule 3 for new requests |
| DONE | Closed as completed | Nothing |
| MERGED | Closed as a duplicate of another opportunity | Check the surviving one lists its members; until it does they read NEW_FEEDBACK |

What the script decides for you, so the pass doesn't re-derive it:

- **Feedback** is an open issue with no skip label and no closing triage verdict, plus each open discussion outside Announcements when Discussions are on (`--category` names the ones to count). The skip labels are `SKIP_LABELS` at the top of `intake_state.py`: `bug`, `roadmap` (a tracking issue isn't a request), and shipyard's own. `--skip-label L` (repeatable) adds to them for one run; for a label every pass should skip, add it to `SKIP_LABELS`
- **A member** of an opportunity is any issue or discussion its `## Evidence` section links in this repo (`#N`, `owner/repo#N`, or a github.com link). Membership is what the opportunity's body says, so editing the Evidence section is how a request joins or leaves
- **Evidence** per opportunity: its member count, and the thumbs-up reactions on the members and on the opportunity itself, closed members included
- **The triage marker**: a comment starting `Triage:` with the verdict in bold. Intake's own comments use `Triage: **opportunity**`, so `triage_state.py` reads a grouped request or an opportunity as triaged instead of NEW. Never put the word "implement" in one: `triage_state.py` would read it as NEEDS_PR

`--label`, `--accepted-label`, and `--marker` rename `opportunity`, `planned`, and `Triage:` for a repo that uses other names. `--json` prints JSON lines, the first one `{"autonomy": ...}`.

## Workflow

### From triage

github-issue-triage sends a user's request for a new capability here instead of writing a spec for it: it comments `Triage: **opportunity**` on the request and leaves the grouping to intake. That comment is a hand-off, not a grouping: the script reads the request NEW_FEEDBACK until an opportunity's Evidence section lists it, and `triage_state.py` reads it TRIAGED, so neither skill asks for it twice. Group it in step 2 as usual; intake's own comment on it, linking the opportunity, follows triage's. Triage keeps the rest: a request an accepted opportunity already covers gets triage's **feature** verdict, held on that opportunity, and a bug or contract tweak stays an **implement**. The way back is step 5: an accepted opportunity is what triage writes a spec for.

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

## Roadmap plan

Milestones are the roadmap: shipyard already releases a lane when the milestone named after the next version has no open issues (`milestone = true`), so a planned milestone is a planned release. The accepted opportunities go into milestones, each with its spec PR and build issues, and the maintainer approves each milestone's contents before it exists. Run this section after intake, or alone when the user asks about the roadmap.

### The capacity

`[roadmap]` in `.github/shipyard.toml` sets it; `doctor` validates the section and refuses unknown keys:

```toml
[roadmap]
wip = 5         # issues open at once across the open milestones (1..100)
cadence = 2     # weeks from one milestone's due date to the next (1..26)
```

Without the section, the script plans with these defaults. `[autonomy] intake` covers this section too: under `observe`, report the plan and write nothing.

### The script

`scripts/roadmap_state.py <owner/repo>`, run from the checkout (it reads the config and the specs in `docs/specs/`). Exit 1 when a row below is an action.

| State | Means | Do |
|---|---|---|
| WIP_OVER | The open milestones hold more open issues than `wip` | Report it; recommend finishing or moving issues out. Propose nothing new |
| OVERDUE | An open milestone is past its due date with issues open | Report its progress; moving the date or the scope is the maintainer's call |
| APPROVED | A proposal was approved, and its milestone is missing or lacks an opportunity it lists | Apply it (Approval, below) |
| UNREADABLE | A proposal lacks its marker line | Fix the body from the template, or ask the maintainer what it proposes |
| NEXT | No proposal is open, and accepted opportunities fit the free capacity | Write the proposal (below) |
| UNPLANNED | An accepted opportunity in no milestone and no pending proposal, with its rank | Nothing by itself: NEXT holds the ones that fit |
| PROPOSAL_OPEN | A proposal waits for the maintainer | Nothing; list it in the report |
| MILESTONE | An open milestone's progress and due date | Report it |

What the script decides:

- **Evidence** is an opportunity's requests plus their thumbs-up and its own
- **Load** is the issues an opportunity adds to a milestone: itself plus its spec's build issues (the spec's Issues section, from the `docs/specs/NNN-*.md` its body links), or itself plus one while no spec is merged
- **Rank** orders by evidence per load, then evidence, then the oldest first
- **NEXT** fills the free capacity (`wip` less the open issues already in open milestones) in rank order, passing over one that doesn't fit, and dates the milestone `cadence` weeks after the latest open milestone's due date (or today). One proposal is pending at a time: an open one, or an approved one not yet applied (APPROVED, UNREADABLE), holds its opportunities and the next plan
- **Approval** is the proposal closed as completed with a comment starting "approve" (or "approved")

### Propose the next milestone

On a NEXT row, write the proposal with [references/milestone-proposal.md](references/milestone-proposal.md), labelled `milestone-proposal` (create the label once). Its marker line names the milestone and its due date; its Opportunities section lists NEXT's opportunities as `#N`. Name the milestone after the version it should release as, when a lane has `milestone = true`: the version after the newest open milestone's, or the next minor after the latest stable tag.

The script's order is the default. Depart from it only for a reason you write into the proposal: one opportunity needs another first, or a `D-n` rule. An UNPLANNED row larger than `wip` never fits: recommend splitting it into specs the size of a milestone. When the newest proposal was closed as not planned, read its comment first and follow it; never propose the same list again unchanged, ask the maintainer instead.

### Approval

The maintainer may edit the Opportunities list before approving; the script reads the body as it is. On APPROVED (not under `observe`):

```bash
gh api repos/{owner}/{repo}/milestones -f title="{title}" -f due_on="{due}T23:59:59Z"   # only when it doesn't exist
gh issue edit {n} -R {owner}/{repo} --milestone "{title}"                                 # each listed opportunity
```

Then move each opportunity's spec PR and existing build issues into the milestone too, and say so on the proposal. From there github-issue-triage works through the milestone in the proposal's order; a build issue filed later for one of its opportunities joins the same milestone.

## Report

Lead with what needs the maintainer: the OPPORTUNITY_OPEN issues, strongest evidence first, each with its request count, thumbs-up, and a one-line recommendation (accept or decline, and why). Then what the pass wrote (opportunities opened and updated, requests linked), the NO_REASON issues, the ACCEPTED ones waiting for triage, and the feedback left ungrouped with the reason. For the roadmap: an open or new proposal first, then OVERDUE and WIP_OVER, then each open milestone's progress. On a quiet pass, one line: "No new feedback; N opportunities wait for your call."

## Improve the skill

When a pass groups badly (two outcomes in one issue, a request proposed again after a decline), add the lesson to step 2. When a script misreads a state, fix `intake_state.py` or `roadmap_state.py` and add a case to shipyard's `tests/test_intake_state.py` or `tests/test_roadmap_state.py`.
