# Triage rubric

## Verdicts

| Verdict | When | Comment says |
|---|---|---|
| **implement** | A confirmed bug; or behaviour the docs already promise but the code breaks; or a small additive feature that fits the release phase | The plan in 2 to 4 bullets; "I'll link the PR here" |
| **opportunity** | A user's request for a new capability (a new command, layer, or workflow) that no accepted opportunity covers yet. Product intake groups it with the requests for the same outcome, and the maintainer accepts or declines the group (D-10); triage doesn't group or write a spec for it | That it goes to product intake, and what happens there ([comments.md](comments.md), Opportunity) |
| **feature** | New behaviour beyond a bug fix or a contract tweak (a new command, layer, or workflow) that fits the release phase, and that the maintainer already wants: an accepted opportunity (labelled `planned`), a request an accepted opportunity covers, or an issue the maintainer filed. A small additive feature such as a new flag stays **implement**, through the design gate | The spec PR's link and a hold line naming it; implementers start once it merges ([spec-gate.md](spec-gate.md)) |
| **postpone** | A new capability, a new wire contract, or a design that must land with sibling issues | Why now is the wrong time, the plan, and what it waits on. Add the `postponed` label |
| **clarify** | You can't reproduce it, or it needs a product or spec decision | The exact question, with options and your recommendation |
| **duplicate** | Same root cause as another issue | A link; close it |
| **won't fix** | It works as intended, or it conflicts with the project's principles | Evidence from the code or docs; close it |

A reporter's open questions get answers in the comment. If the code can settle one, investigate: "does a library WARNING bypass redaction via `logging.lastResort`?" was answerable with a probe, and the answer was yes.

## A new capability: opportunity or feature

Bugs, contract tweaks, and small additive features keep their verdicts. For a request for a new capability, read who wants it before writing a spec:

1. **An accepted opportunity covers it** (an issue labelled `opportunity` and `planned` whose outcome is the request's): the verdict is **feature**. The spec is the opportunity's: run the spec gate on the opportunity issue if triage hasn't yet, add the request to the opportunity's Evidence section, and hold the request on the opportunity issue ([comments.md](comments.md), Feature, covered by an opportunity)
2. **The issue is an accepted opportunity**, or the maintainer filed it (`gh api repos/<owner>/<repo>/issues/<n> -q .author_association` says OWNER, MEMBER, or COLLABORATOR): **feature**, through the spec gate. The maintainer's own issue is already their decision
3. **Anything else**, an open or declined opportunity's outcome included: **opportunity**. Intake adds it to the matching opportunity or opens a new one, and records a decline's reason on it; the maintainer's accept is what makes it a feature

`intake_state.py <owner/repo>` (product-intake's script) lists the opportunities and which are ACCEPTED; read the open and planned ones before deciding.

## The release phase sets the bar

Read it from the tags and the manifest version.

- **Pre-1.0 or active development:** implement most confirmed requests.
- **Release candidate or soak:** implement bugs, broken promises, security or privacy fixes, and small additive features a consumer port needs now. Postpone new wire contracts, new commands, and designs spanning several issues. A behaviour change is acceptable only if it's small and is listed under Breaking.
- **Stable:** anything breaking waits for a major version. Implement it behind a new option, or postpone it.

## Verify before deciding

1. Open the cited files on `origin/<default>`, not on the commit the issue names.
2. Reproduce the issue with its own snippet. Record the actual output.
3. Grep the default branch's CHANGELOG `Unreleased` section and recent commits (`git log --oneline -30`) for work on the same symptom. Main may already handle it:
   - fully: comment and close
   - partly: implement only the remainder, and say so
   - differently: keep main's design and graft onto it. An exit code main already had replaced the one a PR invented
4. For a test flake, read the code under test before calling it a test problem. A heartbeat burst and a Windows replace-versus-read race were both real bugs.

## Grouping and splitting

- **One mechanism, several issues:** decide on it once and comment the same plan on each issue. For example, `env=` aliases for both flags and settings, or a data-only output, line-mode stdin, and typed stdin records.
- **Small part, big part:** ship the small part now, and postpone the rest. For example, an unsupported annotation raising `RegistrationError` now, with typed lists of objects later.
- **Spec-governed behaviour:** when the fix would depart from a spec the project implements (a default value, a required field), the verdict is **clarify**. File an issue on the spec repo with the requirement IDs, the exact numbers, and a proposal, then link it.

## Flags from `triage_state.py`

| Flag | Act |
|---|---|
| NEW | Triage it |
| NEEDS_PR | Dispatch an implementer, or explain on the issue why the plan changed |
| IN_PROGRESS | Nothing, unless the PR stalled |
| DONE_NOT_CLOSED | Close it and cite the merged PR |
| SUSPECT_CLOSE | Check the fix is on the default branch. If it isn't, reopen and explain |
| BLOCKED | Nothing, until the upstream issue closes or the spec PR merges |
| UNFILLED | A `Depends on #{Bk}` placeholder from `specs.py split` was never filled in: edit the issue body to name the dependency's number |
| SPEC_REFUSED | The spec PR it waits on closed without merging: the spec was refused. Decide again in a new triage comment: revise the spec in a new PR (with a new hold line), postpone, or won't fix |
| UNBLOCKED | Read the upstream outcome, update the plan on the issue, then resume. A dependency noted `:not_planned` was closed without its work: decide again whether the issue still stands |
| DECIDED | A trusted person answered its needs-decision question: read the reply, remove the `needs-decision` label, and act on it ([needs-decision.md](needs-decision.md), Taking up an answered item) |
| POSTPONED | Skip it |
| REVISIT | A stable release came out after it was postponed. Apply this rubric again under the new phase |
