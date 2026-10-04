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
2. **Write the spec.** `specs.py new <slug>` writes `docs/specs/NNN-<slug>.md` from the template with the next number and `status: draft`. Fill in every section from the issue and the code, except Verification:
   - **Behaviour** names every touched path or area in backticks, so `find` lists the spec for a change there
   - **Acceptance criteria** are checkable statements, `S-NNN-1`, `S-NNN-2`, and so on, each one a test could prove: an exit code, an output, a file written, a refusal. "Works well" isn't a criterion
   - **Decisions relied on** lists the `D-n` entries it follows; a departure from one is a question for the user, as in the design gate
   - **Issues** stays empty until the build issues exist
3. **Check it.** `specs.py check` must pass: the sections, the criterion ids, and the decisions named
4. **Open the spec PR** on a `spec/NNN-<slug>` branch, with `status: approved` once it's ready for review: merging it with that status is the approval. Its body links the issue as "Spec for #N", never with a closing keyword or "Part of #N": `triage_state.py` would read either as the issue's fix and report IN_PROGRESS, then DONE_NOT_CLOSED once the spec merges
5. **Comment the verdict** on the issue: **feature**, the spec PR's link, and the hold line (see [comments.md](comments.md), Feature). The hold line names the spec PR as `owner/repo#N`, so `triage_state.py` reports the issue BLOCKED while the spec PR is open and UNBLOCKED once it merges
6. **Wait for the merge.** No implementer starts on a feature before its spec merges. A spec PR closed without merging makes the issue SPEC_REFUSED, which needs action: the spec was refused, so decide again in a new triage comment. Revise the spec in a new PR (and name it on a new hold line), postpone, or won't fix. The newer triage comment settles the refused PR, so it stops counting

## Status

The line under the title says where a spec stands; `specs.py check` accepts only these three:

| Status | Means | Set by |
|---|---|---|
| `draft` | Being written; a merged draft is not approved, so its build doesn't start | `specs.py new` |
| `approved` | The spec PR merged with it: the build may start | The spec PR, when it's ready for review |
| `built` | Every build issue is closed or closing, and each criterion was checked on the code | The PR that closes the last build issue |

`specs.py check` refuses a `built` spec that lists no build issues, or whose Verification section leaves out a criterion id. `specs.py coverage` (in CI) refuses a `built` spec with a criterion no test names.

## When the spec merges

The issue reads UNBLOCKED. Then:

- Edit the issue body to link the merged spec, and fill the spec's Issues section with the build issues in the next docs change (a feature may split into several issues; file them now)
- Comment **implement** on each build issue, naming the spec, and dispatch implementers as usual
- Each implementer brief carries the spec's criteria, from `specs.py criteria NNN` (see [implementer-brief.md](implementer-brief.md)). A departure from a criterion is a "decision for you", never a quiet deviation

## Tests name the criteria they prove

A test proves a criterion by its name, `test_s007_2_operate_exits_2_when_...` (Go: `TestS007_2...`), or by a comment line `proves: S-007-2` above it (`#` or `//`; several ids may follow, comma-separated). `specs.py coverage --spec NNN` lists each criterion and the tests that name it, in Python, JS/TS, Go, and Rust test files, read as text. The script finds names; whether a test really asserts its criterion is the reviewer's judgment (github-pr-triage's reviewer brief).

## Verify the whole spec

Tests prove what they assert, which can be narrower than the criterion. When the PR that closes a spec's last build issue is written, its agent checks each criterion against the code on the default branch plus that PR: runs the command, reads the output, opens the file. Then, in the same PR:

- Set `status: built`, and fill in the Issues section with the build issues
- Under Verification, one line per criterion: `- S-NNN-k: <how it was checked>, <the result>`. A criterion that doesn't hold is a "decision for you", not a line in Verification
- Run `specs.py check` and `specs.py coverage --spec NNN`; both must pass

The spec reaches `built` before its last issue closes, so CI holds every later change to its criteria.

## Changing a merged spec

A spec changes through a PR like any other file, reviewed before the build follows it. Never renumber criteria that tests or PRs already cite: add new ids at the end, and mark a dropped one in its text ("S-007-3: dropped in #81, see S-007-5") rather than deleting it.
