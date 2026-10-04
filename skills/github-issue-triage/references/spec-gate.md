# Spec gate

A feature is designed once, in a spec file the maintainers review line by line, before any implementer starts. Merging the spec is the approval: one review instead of a question on every PR that builds it. Specs live in `docs/specs/NNN-<slug>.md` and are merged through a pull request before their build starts; issue bodies link to the spec, they don't hold it (shipyard's own D-5).

## Feature, contract change, or bug fix

| The issue asks for | Verdict | Gate |
|---|---|---|
| A bug fix that restores documented behaviour, or an internal refactor | **implement** | None |
| A change to a contract (a flag, a format, a default, a public API, stored state) that stays within today's behaviour | **implement** | The [design gate](design-gate.md) |
| New behaviour beyond a bug fix or a contract tweak: a new command, a new layer, a workflow users haven't had | **feature** | This gate: a merged spec first, then **implement** |

When in doubt between a contract tweak and a feature, ask whether a reviewer would want to approve the acceptance criteria before the code exists. If yes, it's a feature.

## The gate

1. **Read what's settled.** `specs.py find <the paths and areas the feature touches>` lists specs that already cover them: extend one of those rather than writing a second. `decisions.py find` on the same terms gives the rules the spec relies on
2. **Write the spec.** `specs.py new <slug>` writes `docs/specs/NNN-<slug>.md` from the template with the next number. Fill in every section from the issue and the code:
   - **Behaviour** names every touched path or area in backticks, so `find` lists the spec for a change there
   - **Acceptance criteria** are checkable statements, `S-NNN-1`, `S-NNN-2`, and so on, each one a test could prove: an exit code, an output, a file written, a refusal. "Works well" isn't a criterion
   - **Decisions relied on** lists the `D-n` entries it follows; a departure from one is a question for the user, as in the design gate
   - **Issues** stays empty until the build issues exist
3. **Check it.** `specs.py check` must pass: the sections, the criterion ids, and the decisions named
4. **Open the spec PR** on a `spec/NNN-<slug>` branch. Its body links the issue as "Spec for #N", never with a closing keyword or "Part of #N": `triage_state.py` would read either as the issue's fix and report IN_PROGRESS, then DONE_NOT_CLOSED once the spec merges
5. **Comment the verdict** on the issue: **feature**, the spec PR's link, and the hold line (see [comments.md](comments.md), Feature). The hold line names the spec PR as `owner/repo#N`, so `triage_state.py` reports the issue BLOCKED while the spec PR is open and UNBLOCKED once it merges
6. **Wait for the merge.** No implementer starts on a feature before its spec merges. A spec PR closed without merging keeps the issue BLOCKED: the spec was refused, so decide again (a new spec, **clarify**, or **won't fix**) and replace the hold line

## When the spec merges

The issue reads UNBLOCKED. Then:

- Edit the issue body to link the merged spec, and fill the spec's Issues section with the build issues in the next docs change (a feature may split into several issues; file them now)
- Comment **implement** on each build issue, naming the spec, and dispatch implementers as usual
- Each implementer brief carries the spec's criteria, from `specs.py criteria NNN` (see [implementer-brief.md](implementer-brief.md)). A departure from a criterion is a "decision for you", never a quiet deviation

## Changing a merged spec

A spec changes through a PR like any other file, reviewed before the build follows it. Never renumber criteria that tests or PRs already cite: add new ids at the end, and mark a dropped one in its text ("S-007-3: dropped in #81, see S-007-5") rather than deleting it.
