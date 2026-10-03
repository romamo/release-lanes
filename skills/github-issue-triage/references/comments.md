# Issue comments

Write each body to a file and post it with `gh issue comment <n> --body-file <file>`. Keep the files in the repo's `tmp/` folder. Start every triage comment with `Triage:` followed by the bolded verdict, because `triage_state.py` finds triage comments by that prefix. Follow the user's writing rules: no trailing periods on list items, and em dashes only rarely.

## Implement

```markdown
Triage: **implement**{, for the next release candidate | after rcN}. The plan:

- {change 1}
- {change 2}

{What is deliberately left out, and where it went (a postponed issue, 1.1).} I'll link the PR here.
```

Once the PR is up:

```markdown
The fix is in #{PR}: {what it does, in one or two sentences}. {Any trade-off or behaviour change, e.g. "a real permission error takes up to 2 s to surface on Windows".} {The answer to the issue's open question, if it had one.} This issue closes when #{PR} merges.
```

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

Keep "On hold" and the upstream link on one line, because `triage_state.py` reads that line to report BLOCKED and later UNBLOCKED:

```markdown
On hold: {why, e.g. #PR goes below the spec's default of 5 rotated files}, decided in {owner/repo#N}. {What stays ready meanwhile, e.g. "#59 stays open, rebased onto main".}
```

Put the same line on the held PR.

## Follow-ups

- A PR was replaced: "Update: #{old} was replaced by #{new}, {rebased with review fixes}. This issue closes when #{new} merges."
- The plan changed after review, e.g. main already had part of the fix: "Update after rebasing: {what main already does}; #{PR} now only {what remains}." Also update the PR's title and body.
- A wrong auto-close: `gh issue reopen <n> --comment "Reopening: commit {sha} closed this because its message quotes {text}. The fix is in #{PR}, which hasn't merged yet."`
- A manual close after a merge: `gh issue close <n> --comment "Fixed by #{PR}{, merged with every CI job green}. {What's still unconfirmed, and how to reopen with evidence.}"`
