# Issue comments

Write each body to a file and post it with `gh issue comment <n> --body-file <file>`. Keep the files in the repo's `tmp/` folder. Start every triage comment with `Triage:` followed by the bolded verdict, because `triage_state.py` finds triage comments by that prefix. Follow the user's writing rules: no trailing periods on list items, and em dashes only rarely.

## Implement

```markdown
Triage: **implement**{, for the next release candidate | after rcN}.{ Waits on your decision below.}

{Only while a design question is open (design-gate.md, step 3), and only in a session the user started by hand; the block goes first, before the plan. A gate session posts the block as its own needs-decision comment instead, right after this one:}
**Decision needed:** {the question in one sentence, in words a user of the tool knows}

1. {option} (recommended): {what it means for users}
2. {option}: {what it means for users, what it costs}

Reply here with a number or your own answer{, or in the gate session}; nothing is built until you do.

The plan:

- {change 1}
- {change 2}

{Only for a contract change (design-gate.md):}
**Design.** {The contract change exactly: flag, field and type, default.} {Why this over the alternative.} {Compatibility: who breaks, and the migration.} {Relies on D-n.} {How it's tested.}

{What is deliberately left out, and where it went (a postponed issue, 1.1).} I'll link the PR here.
```

Write the decision block for the person who owes the answer, not for the implementer: no file, function, or permission names in the question or the options unless the answer turns on them, and then say what each one means. Name the effect instead ("repos without ship-watch stay stuck until someone notices"). Keep the options to the ones you'd accept, two or three, the recommended one first. The same block, under the same heading, asks a PR's "decisions for you" on the pull request (design-gate.md, step 3).

In a gate session (its prompt carries the gate's `Gate session:` or `Headless:` paragraph), the block never sits in the verdict: the verdict keeps "Waits on your decision below" and the question follows as its own comment, the [needs-decision protocol](needs-decision.md)'s, with the marker as its first line and the `needs-decision` label on the issue (D-21). Without the marker and the label, the scripts read the issue as work and the gate keeps starting sessions for it.

An answer the user gives in the session (to this block, to a PR's "decisions for you", or to a needs-decision question) goes on the item before you act on it. Posted as the App (`shipmill gh` with `[agents] app_id` set), its first line is `Decision by @<login>, relayed by <agent>`, so the thread tells the person's decision from agent text; `<login>` is the person who answered and `<agent>` names the tool (`Claude Code`):

```markdown
Decision by @{login}, relayed by {agent}

{The option chosen, or the user's own words}
```

Once the PR is up:

```markdown
The fix is in #{PR}: {what it does, in one or two sentences}. {Any trade-off or behaviour change, e.g. "a real permission error takes up to 2 s to surface on Windows".} {The answer to the issue's open question, if it had one.} This issue closes when #{PR} merges.
```

## Opportunity

A request for a new capability that no accepted opportunity covers goes to product intake (triage-rubric.md, A new capability). The verdict is the marker intake uses itself, so `triage_state.py` reads the issue TRIAGED and `intake_state.py` reads it NEW_FEEDBACK until an opportunity's Evidence section lists it. Don't use the word "implement" in this comment, and don't put an issue number right after a hold phrase ("waits on #40"): `triage_state.py` would read NEEDS_PR or BLOCKED.

```markdown
Triage: **opportunity**. {What the request asks for, in one sentence}: a new capability, not a bug fix or a contract tweak. It goes to product intake, which groups it with the requests for the same outcome into one opportunity issue{, likely #{open opportunity}}; the maintainer accepts or declines that opportunity, and an accepted one gets a spec before anything is built. Follow this issue for the link to the opportunity.
```

## Feature

A feature waits for its spec ([spec-gate.md](spec-gate.md)). Keep "On hold" and the spec PR's `owner/repo#N` in one sentence of one line, so `triage_state.py` reports BLOCKED until the spec merges and UNBLOCKED after. Don't use the word "implement" in this comment: the script reads the latest triage comment's verdict from it.

```markdown
Triage: **feature**. {What the new behaviour is, in one or two sentences, and why it isn't a bug fix or a contract tweak.}

The spec is in #{spec PR} (`docs/specs/{NNN}-{slug}.md`), with its acceptance criteria. Merging it is the approval; the build starts after that.

On hold: the build waits on {owner/repo}#{spec PR}, the spec
```

A request an accepted opportunity covers is built under that opportunity: its spec is the opportunity's, so the request holds on the opportunity issue, which closes when the build does. Add the request to the opportunity's Evidence section too.

```markdown
Triage: **feature**. This asks for the outcome of #{opportunity}, which the maintainer accepted; it's built under that issue{, whose spec is in #{spec PR}}, and this one closes with it.

On hold: built under {owner/repo}#{opportunity}, the accepted opportunity
```

Once the spec merges, edit the issue body to link it and split the spec into build issues ([spec-gate.md](spec-gate.md), Split it into build issues). Comment the Implement template on each build issue, naming the spec and the criteria it delivers.

## Postpone

```markdown
Triage: **postponed to {1.1 | after 1.0}**. {Why not now: the release phase, a new wire contract, or a shared design with #A and #B.}

The plan:

- {design point}
- {design point}

{Answers to the issue's open questions, "unless someone objects".}
```

Then add the label: `gh issue edit <n> --add-label postponed`.

## Clarify

```markdown
Triage: **needs a decision**. {What conflicts, with exact values and the requirement or doc IDs.}

Options:

1. {option}: {consequence}
2. {option}: {consequence}

I'd go with {n} because {reason}.{ Filed upstream as <owner/repo#N>.}
```

## Hold (blocked on something outside the repo)

Keep "On hold" and the upstream link in one sentence of one line, because `triage_state.py` reads that sentence to report BLOCKED and later UNBLOCKED. A sentence ends at ".", "!", or "?" before a capital letter (not after "e.g.", "i.e.", "cf.", or "vs."), so a hold word elsewhere in a paragraph doesn't hold on the links it names:

```markdown
On hold: {why, e.g. #PR goes below the spec's default of 5 rotated files}, decided in {owner/repo#N}. {What stays ready meanwhile, e.g. "#59 stays open, rebased onto main".}
```

Put the same line on the held PR.

## A person's item

An issue labelled `human` gets no triage verdict: its assignees do it ([triage-rubric.md](triage-rubric.md), A person's item). Each comment below starts with its marker as the first line where it has one, since `triage_state.py` reads the marker only there and only from the `--bot-login` or a trusted author.

**HANDOFF_DUE**, the hand-off: a mention of every assignee, which holds closed, the issue's steps quoted (never rewritten), its `## Check` when it has one, and how to report. Post it once per unblock; nothing mentions the person again while the item reads WITH_PERSON.

```markdown
<!-- shipmill:handoff -->
@{assignee} @{assignee}, it's your turn: {owner/repo#N} closed{, and owner/repo#M}.

What to do, from this issue:

> {the issue's steps, quoted line by line}

{When the issue has one:} When it's done, this must hold:

> {its ## Check, quoted}

Reply `done` here when it's finished, or close the issue.
```

**VERIFY_DUE**, the check holds: say what was checked and what came back, then close the issue as completed (`gh issue close <n> --reason completed`), which unblocks whatever it held. With no `## Check`, say "Closed on @{login}'s report: this issue has no ## Check" instead.

```markdown
Checked: `{the check}` returned {what came back}, as the ## Check asks. Closing as done.
```

**VERIFY_DUE**, the check fails: a new hand-off, so the item reads WITH_PERSON again.

```markdown
<!-- shipmill:handoff -->
@{assignee}, the check didn't pass yet: `{the check}` returned {what came back}, and it must return {what the ## Check asks}.

Reply `done` here when it's fixed, or close the issue.
```

**VERIFY_CLOSED**, the check holds: the verified comment, which ends the state.

```markdown
<!-- shipmill:verified -->
Checked after @{login} closed this: `{the check}` returned {what came back}.
```

**VERIFY_CLOSED**, the check fails: reopen it (`gh issue reopen <n>`), then post a hand-off through `--body-file`; the open issue holds everything it blocks again.

```markdown
<!-- shipmill:handoff -->
@{assignee}, I reopened this: `{the check}` returned {what came back}, and it must return {what the ## Check asks}. What waits on this issue waits again.

Reply `done` here when it's fixed, or close the issue.
```

**NO_ASSIGNEE**, and a check the session can't run: a question through the [needs-decision protocol](needs-decision.md), in its format, with its marker and the `needs-decision` label. For NO_ASSIGNEE, ask who does the item. For a check, say what the report claims and why the check can't run here, and give two options, the one the evidence supports first:

```markdown
<!-- shipmill:needs-decision -->
@{maintainer} Decision needed: @{login} reported this done, and I can't run its check here (`{the check}`: {why, such as "the host isn't reachable from this machine"}).

1. Close it on the report: what waits on it starts
2. It isn't done: I hand it back to @{assignee}

Reply here with a number or your own answer; shipmill takes this up on the tick after your reply.
```

## A parent's close

A parent held only by its children reads UNBLOCKED once the last closes. Close it with:

```markdown
Every sub-issue is closed:

- {owner/repo#N}: {completed | not planned}
- {owner/repo#M}: {completed | not planned}

Closing this as done.
```

A child closed as not planned doesn't stop the close; say so in its line. A feature issue first gets its spec verified ([spec-gate.md](spec-gate.md), Verify the whole spec).

## Follow-ups

- A PR was replaced: "Update: #{old} was replaced by #{new}, {rebased with review fixes}. This issue closes when #{new} merges."
- The plan changed after review, e.g. main already had part of the fix: "Update after rebasing: {what main already does}; #{PR} now only {what remains}." Also update the PR's title and body.
- A wrong auto-close: `gh issue reopen <n> --comment "Reopening: commit {sha} closed this because its message quotes {text}. The fix is in #{PR}, which hasn't merged yet."`
- A manual close after a merge: `gh issue close <n> --comment "Fixed by #{PR}{, merged with every CI job green}. {What's still unconfirmed, and how to reopen with evidence.}"`
