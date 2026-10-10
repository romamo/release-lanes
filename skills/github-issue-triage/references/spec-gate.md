# Spec gate

A feature is designed once, in a spec file the maintainers review line by line, before any implementer starts. Merging the spec is the approval: one review instead of a question on every PR that builds it. Specs live in `docs/specs/NNN-<slug>.md` and are merged through a pull request before their build starts; issue bodies link to the spec, they don't hold it (shipmill's own D-5).

## Feature, contract change, or bug fix

| The issue asks for | Verdict | Gate |
|---|---|---|
| A bug fix that restores documented behaviour, or an internal refactor | **implement** | None |
| A change to a contract (a flag, a format, a default, a public API, stored state) that stays within today's behaviour | **implement** | The [design gate](design-gate.md) |
| A user's request for new behaviour (a new command, a new layer, a workflow users haven't had) that no accepted opportunity covers | **opportunity** | Product intake: grouped into an opportunity the maintainer accepts or declines; no spec yet |
| New behaviour beyond a bug fix or a contract tweak that the maintainer already wants (below) | **feature** | This gate: a merged spec first, then **implement** |

When in doubt between a contract tweak and a feature, ask whether a reviewer would want to approve the acceptance criteria before the code exists. If yes, it's a feature, or an opportunity while the maintainer hasn't wanted it yet.

## Where features come from

A spec is written only for work the maintainer has chosen, so a review of the spec is a review of how, never of whether:

- **An accepted opportunity:** an issue labelled `opportunity` and `planned`. Product intake grouped the users' requests into it and the maintainer accepted it (D-10); `intake_state.py` reads it ACCEPTED until triage's **feature** comment, then HANDED_OFF. The opportunity issue is the feature issue, and the spec's Problem starts from its Problem and Evidence
- **A request an accepted opportunity covers:** the spec is the opportunity's. Run this gate on the opportunity issue, or extend its spec when it has one; the request holds on the opportunity (comments.md, Feature)
- **An issue the maintainer filed** (author association owner, member, or collaborator): the filing is the decision

Any other request for new behaviour is an **opportunity**: triage comments the hand-off (comments.md, Opportunity) and leaves the grouping to product intake. A request whose outcome was declined goes there too, where intake records it against the decline.

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

`specs.py check` refuses a `built` spec that lists no build issues, or whose Verification section leaves out a criterion id, and an `approved` or `built` spec whose Issues lines leave a criterion out or give one to two build issues. It also refuses the template's placeholder text left word for word: a `draft` may keep it, an `approved` spec may not in Problem, Behaviour, Acceptance criteria, or Out of scope, and a `built` spec in any section. `specs.py coverage` (in CI) refuses a `built` spec with a criterion no test names.

## When the spec merges

The issue reads UNBLOCKED. Edit its body to link the merged spec, then split the spec into build issues.

### Split it into build issues

A build issue is sized for one PR. Group the criteria that change the same code into one issue, and give a criterion that needs another's code its own issue that depends on the earlier one. A small spec is one build issue.

1. **Propose the graph.** `specs.py split NNN --repo <owner/repo> --group 1,2 --group 3 --after 2:1` prints one build issue per `--group` of criterion numbers, in the order given, with its title, its body naming its `S-NNN-k` criteria, and a `Depends on <owner/repo>#{Bk}` line for each `--after B:A` (B depends on A, and A comes first). With no `--group`, every criterion goes into one issue. It splits only an `approved` spec that passes `check`, and only the criteria no build issue has yet, so a spec that gained criteria later splits again for just those. `--json` prints the same as one object
2. **File them in order.** `gh issue create --body-file` for each, replacing every `{Bk}` with the number the earlier issue got. `triage_state.py` reads each `Depends on` line as a hold: the issue reads BLOCKED while its dependency is open, UNBLOCKED once it closes, and UNFILLED while a `{Bk}` is left in
3. **Link the dependencies natively.** For each `Depends on` line `split` printed, `chains.py link <owner/repo>#<build issue> --blocked-by <owner/repo>#<dependency>`: it adds GitHub's relation and finds the line already there. Where the forge refuses the relation it prints `text only:` and the line holds alone ([triage-rubric.md](triage-rubric.md), Links between issues)
4. **Put each under the feature issue.** `chains.py link <owner/repo>#<feature> --child <owner/repo>#<build issue>` makes it a sub-issue and adds a `Depends on` line for it to the feature issue's body, so the feature issue reads BLOCKED while the build runs, with `children K/N closed` in its note, and UNBLOCKED when the last build issue closes
5. **Record the assignment.** Fill the spec's Issues section with the lines `split` printed, numbers filled in, in one docs PR: `- owner/repo#N: S-NNN-1, S-NNN-2`. `specs.py check` then holds every criterion of an approved or built spec to exactly one build issue there; an approved spec with no Issues lines hasn't been split yet and passes
6. **Comment implement** on each build issue, naming the spec, and dispatch as below

### Dispatch the build

- **Ready:** a build issue whose dependencies are all closed reads NEEDS_PR (or UNBLOCKED, once its last dependency closes). Dispatch it as usual
- **Ready to stack:** a build issue whose only open dependencies have an open PR may start before that PR merges. Its implementer branches from the dependency's PR branch and opens its PR with that branch as the base, saying "Stacked on #PR" in the body; github-pr-triage lands the stack in order. Don't stack on a dependency with no PR yet
- **WIP limit:** when the config has `[roadmap] wip = N` (#70 adds it to the config schema; until it lands the config refuses an unknown `[roadmap]` table, so don't add one), at most N issues are in progress (an open PR or a running implementer) at once. `triage_state.py --wip N` reports the room left and the ready issues, oldest first; build issues are filed in build order, so the oldest go first. With no `[roadmap] wip`, there is no limit
- Each implementer brief carries the criteria its build issue delivers, from the issue body or `specs.py criteria NNN` (see [implementer-brief.md](implementer-brief.md)). A departure from a criterion is a "decision for you", never a quiet deviation
- **The last build issue:** the brief for the one whose PR closes the last open build issue also carries "Verify the whole spec" below, and its PR body says `Fixes #<feature>` too, so the feature issue closes with it
- **A step only a person can do** (a server change, a key rotation, a payment): file it as its own issue labelled `human`, assigned to that person, with a `## Check` section, and link it into the build with `chains.py link` like any build issue. No implementer is dispatched for it; triage hands it off when its dependencies close and checks the result ([triage-rubric.md](triage-rubric.md), A person's item)

## Tests name the criteria they prove

A test proves a criterion by its name, `test_s007_2_operate_exits_2_when_...` (Go: `TestS007_2...`), or by a comment line `proves: S-007-2` above it (`#` or `//`; several ids may follow, comma-separated). `specs.py coverage --spec NNN` lists each criterion and the tests that name it, in Python, JS/TS, Go, and Rust test files, read as text. The script finds names; whether a test really asserts its criterion is the reviewer's judgment (github-pr-triage's reviewer brief).

## Verify the whole spec

Tests prove what they assert, which can be narrower than the criterion. When the PR that closes a spec's last build issue is written, its agent checks each criterion against the code on the default branch plus that PR: runs the command, reads the output, opens the file. Then, in the same PR:

- Set `status: built`. The Issues section already lists the build issues and their criteria (Split it into build issues, step 5); `check` refuses a built spec without them
- Under Verification, one line per criterion: `- S-NNN-k: <how it was checked>, <the result>`. A criterion that doesn't hold is a "decision for you", not a line in Verification
- Run `specs.py check` and `specs.py coverage --spec NNN`; both must pass

The spec reaches `built` before its last issue closes, so CI holds every later change to its criteria.

## Changing a merged spec

A spec changes through a PR like any other file, reviewed before the build follows it. Never renumber criteria that tests or PRs already cite: add new ids at the end, and mark a dropped one in its text ("S-007-3: dropped in #81, see S-007-5") rather than deleting it.

A criterion whose text starts `dropped in #N` is dropped: `coverage` prints it as dropped and needs no test for it, `split` leaves it out of new build issues (and refuses a `--group` that names it), and `check` needs no Issues line for it, though one assigned before it was dropped may stay. A built spec still names it under Verification, as `- S-NNN-k: dropped in #N`. A criterion that only mentions "dropped" later in its text is not dropped and still needs its test.
