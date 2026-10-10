# Design: the product layer

Status: map and direction, 2026-10-10 (shipmill 0.45.0). Nothing here is new behaviour; specs
carve the gaps below into buildable pieces, the first being shipmill/shipmill#334, specified
in [spec 017](../specs/017-work-chains.md). Where this note and a merged spec differ, the
spec wins.

shipmill's product layer is the part of the factory that decides **what** gets built and
**when** it ships: feedback, opportunities, specs, the task graph, milestones and releases.
Each stage was built on its own (#69, #54, #71, #70), and each works. What is missing is the
connective tissue: work done by people rather than agents, links the forge already holds,
the step after a milestone ships, and a view across repos. This note names the stages and
the objects that move through them, says who moves each one, and ranks the gaps, so the
layer grows on purpose rather than issue by issue.

## What stays true

- **The forge is the state.** Every object below is an issue, a label, a milestone, a marker
  comment, a file or a tag. shipmill keeps nothing of its own (#26, rule 1; D-6's spirit for
  product data: no external tracker)
- **Scripts answer facts, agents judge.** Each stage has a `*_state.py` that says where every
  object stands and exits 1 when one needs action; a skill decides what to do about it
- **The maintainer decides what and when, the spec decides how.** Accepting an opportunity
  (D-10), merging a spec (D-5) and approving a milestone are the three human approvals on the
  path from feedback to release. Agents propose all three, and never take them
- **Questions wait on the forge.** Anything an agent can't decide becomes a `needs-decision`
  item (D-21), not a stalled session
- **Forge-neutral by capability.** A relation the forge holds natively is read through the
  code host port (#322) as a capability; text conventions work on every forge

## Objects

| Object | On the forge | Created by | Done when |
|---|---|---|---|
| Request | An issue or discussion asking for an outcome | A user | Grouped into an opportunity, or left as a bug |
| Opportunity | An issue labelled `opportunity`; its `## Evidence` section lists the requests | Product intake | The maintainer labels it `planned` (accepted) or closes it as not planned (declined) |
| Feature | The accepted opportunity, or an issue the maintainer filed, carrying a spec | Triage's **feature** verdict | Its last build issue closes (`Fixes #feature` on the last build PR) |
| Spec | `docs/specs/NNN-<slug>.md`, `status: draft`, `approved`, `built` | Triage's spec gate | Its PR merges (approved), then its build verifies it (built) |
| Build item | An issue holding a group of the spec's criteria, with `Depends on` lines, a sub-issue of the feature | `specs.py split` | Its PR merges |
| Fix | An issue triaged **implement** (a bug or a contract tweak) | Triage | Its PR merges |
| Decision | A `needs-decision` marker comment and label on any item | Any agent session | A trusted person replies; the next session removes the label |
| Human item | (missing) An item only a person can do: a deploy, a credential, a payment, a vote | Today: by hand, assigned to the person | Today: the person closes it, if they remember |
| Milestone proposal | An issue labelled `milestone-proposal` with a marker naming the milestone and its due date | Product intake's roadmap plan | The maintainer closes it with "approve" |
| Milestone | A forge milestone named after the version it releases | The roadmap plan, once approved; or by hand | Its last issue closes |
| Release | A tag and a CHANGELOG section cut on a lane | The release workflow, by policy | Published; fixed issues told by shipped notices |

The feature issue is already an epic in all but name: it has sub-issues, `Depends on` lines on
each build item, and closes with the last one. What it lacks is a life outside the spec gate
(below, gap 5).

## States and who moves them

Four actors move objects: **users** (anyone filing), **agents** (intake, triage, resolve, PR
triage, ship-watch sessions), **people with authority** (the maintainer and trusted
collaborators, D-16), and **shipmill's code** (the release planner and the gate).

| Stage | States (script) | Agent moves | Person moves | Code moves |
|---|---|---|---|---|
| Feedback | NEW_FEEDBACK, OVERLAP, OPPORTUNITY_OPEN, ACCEPTED, DECLINED, NO_REASON, HANDED_OFF, DONE, MERGED (`intake_state.py`) | Groups requests, opens and updates opportunities | Accepts (`planned`) or declines with a reason | Nothing |
| Spec | The feature reads BLOCKED on its spec PR, then UNBLOCKED or SPEC_REFUSED (`triage_state.py`); the spec's status line (`specs.py check`) | Writes the spec, opens its PR, splits it | Merges the spec PR, or closes it | `specs.py check` and `coverage` in CI |
| Build | NEEDS_PR, IN_PROGRESS, BLOCKED, UNFILLED, UNBLOCKED, DONE_NOT_CLOSED (`triage_state.py`) | Implements, reviews, lands within `[roadmap] wip` | Nothing, unless asked | Merges on green under the repo's autonomy |
| Decisions | NEEDS_DECISION, then DECIDED (`triage_state.py`, `watch_state.py`) | Asks, then takes up the answer | Answers | The gate starts no session while it waits |
| Roadmap | NEXT, PROPOSAL_OPEN, APPROVED, UNPLANNED, MILESTONE, OVERDUE, WIP_OVER (`roadmap_state.py`) | Proposes the next milestone, applies an approved one | Approves, edits or refuses the proposal | Nothing |
| Release | A lane is due when a trigger fires and no gate holds it; `milestone = true` fires when the version's milestone has closed issues and none open (`planner.py`) | Watches, reruns, tells reporters | Holds or starts a lane by hand | Cuts the release |

Read end to end, the path is:

```text
request ──intake──▶ opportunity ──maintainer: planned──▶ feature ──triage──▶ spec PR
   ──maintainer: merge──▶ build items (Depends on, sub-issues) ──agents──▶ merged PRs
   ──roadmap──▶ milestone proposal ──maintainer: approve──▶ milestone "X.Y.0"
   ──last issue closes──▶ lane due ──planner──▶ release X.Y.0 ──ship-watch──▶ shipped notices
```

## How it is used today

Checked on 2026-10-10 across the five repos the maintainer runs shipmill in:

- No repo sets `[roadmap]`, and no repo but shipmill has an opportunity issue; the roadmap
  plan has never run outside its tests
- Two repos set `milestone = true` on their stable lane. In one, the version shipped and its
  milestone is still open with no next milestone, so the lane has no milestone trigger left;
  in the other, the last open issue in the milestone is a question to the maintainer, and
  "close the milestone" is a step the maintainer has to remember after a soak ends. The
  milestone works as a manual hold, not as a plan
- In a third repo, three of the maintainer's own steps (a key rotation, a web server change,
  a pricing call) were filed as issues assigned to the maintainer, with nothing to say when they
  became doable and nothing to start the next step once they were done

## Gaps, ranked

Ranked by how often the gap costs the maintainer a step today, then by how much else waits
on it. Forge-specific work waits for the code host port (#322), which has priority until
2026-10-26; until then this is design work.

| # | Gap | Why it ranks here | Tracked in |
|---|---|---|---|
| 1 | **Human items have no lifecycle.** UNBLOCKED always means "dispatch an implementer"; an item only a person can do is never announced when it unblocks, never verified, and its close starts nothing | Seen in two repos in two days; every chain with a deploy, a credential or a decision in it stops here | #334 |
| 2 | **A milestone's loop doesn't close.** Nothing closes a milestone once its version ships, opens the next one, or moves leftover issues; the roadmap plans only accepted opportunities, so fixes and maintainer-filed features reach a milestone only by hand | Both repos that use `milestone = true` show it; small: one milestone write on the port beside the read it has | #336 |
| 3 | **No single queue for the maintainer.** Decisions, human items, opportunities awaiting a call, milestone proposals and spec PRs to merge are spread across repos; `fleet.py report` lists watch rows, and `shipmill status` covers one repo | Every approval in the layer is the maintainer's, so the layer moves only as fast as they are found | #337 |
| 4 | **Native relations aren't read.** Forge dependencies and sub-issues hold nothing; sub-issues are written by `specs.py split` and never read back | Links set in the forge's UI silently do nothing; GitHub first in spec 017, other forges behind the port (spec 016) | #334 |
| 5 | **No epic outside the spec gate.** A parent whose children are fixes, human items or items in other repos has no state, progress or close | Comes almost free once gaps 1 and 4 land | #334 |
| 6 | **Planning stops at the repo.** A chain, a milestone or a roadmap for a product that spans repos has no home; the gate and triage see one repo | Real (one product spans three repos) but rare among users so far; it needs gaps 1 to 4 first | Chains: #334; a plan across repos: #338 |
| 7 | **The layer is configured nowhere.** Setup never offers `[roadmap]`, the `opportunity` and `planned` labels, or `milestone = true` with a first milestone | Not a missing feature; until it is fixed, gaps 2 and 3 have no users | #336, under Success |

## Direction for each gap

These are the shapes the specs should start from, not the specs.

**1. Human items.** Settled in spec 017: an issue labelled `human` is a person's item, done
by its assignees. Its body may carry a `## Check` section: what proves it done. On unblock,
a triage session mentions the assignees once with what to do. "Done" is the person
commenting `done` or closing it; a session then runs the check where it can and either
closes or confirms the item, or hands it back with what failed. Its close unblocks the next
item like any other. There are no reminders yet; a repeat mention can come later behind its
own option.

**2. Closing the milestone loop.** When a lane with `milestone = true` releases version X,
the same run closes milestone X and moves any issue left in it (there should be none) to the
next one. The roadmap plan then reads a missing next milestone as NEXT even with no accepted
opportunity, so fixes triaged **implement** can be proposed into it too, ranked after the
opportunities. A hold stays a hold (D-8): an open issue parked in a milestone to stop a
release reads as a hold in `watch_state.py`, with its reason, instead of as unfinished work.

**3. The maintainer's queue.** `fleet.py report --queue` (or `shipmill queue` over the same
fleet file) lists only what waits on a person, across repos, oldest first: NEEDS_DECISION
items, human items assigned to the reader, OPPORTUNITY_OPEN, PROPOSAL_OPEN, open spec PRs,
and NO_REASON declines. It reads the state scripts that already exist and adds no state.

**4. Native relations.** Settled in spec 017 for GitHub: `triage_state.py` reads issue
dependencies and sub-issues as holds, the same as a `Depends on` line; both count, and the
item waits until every one is closed. `chains.py link` writes the relation and the text line
together. GitLab's linked issues (`blocks`, `is_blocked_by`) and epics come through the code
host port (#322) later, behind the same reading.

**5. Epics.** Settled in spec 017: an issue with sub-issues is a parent; `triage_state.py`
reports its progress (`children K/N closed`) and reads it UNBLOCKED, to close, when the last
child closes. The spec gate's feature issue is then one kind of parent, not a special case.

**6. Across repos.** Chains first: each repo's triage already parses `owner/repo#N` holds,
so a cross-repo chain advances as long as every repo in it runs a gate, and D-16 applies in
each. A plan across repos (one milestone name in several repos, released together) is a
later question, and depends on the queue (gap 3) to be visible at all.

## Questions for the maintainer

- Whether a release may close its milestone by itself, or proposes the close (gap 2)
- Whether fixes belong in the roadmap plan, or milestones stay for features only (gap 2)
- Whether a plan across repos is wanted in shipmill at all, or stays a view over per-repo
  plans (gap 6)
