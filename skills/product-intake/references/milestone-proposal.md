# The milestone proposal

One issue per proposed milestone, labelled `milestone-proposal`, written to a file in the repo's `tmp/` and posted with `--body-file`. Follow the user's writing rules: no trailing periods on list items or table cells, em dashes only rarely.

Title: `Roadmap: milestone {title}, due {YYYY-MM-DD}`

```markdown
<!-- shipmill:milestone title="{title}" due="{YYYY-MM-DD}" -->

The next milestone, from the accepted opportunities that fit the free capacity: {free} of `wip` {wip} open issues, due {cadence} weeks after {the latest open milestone | today}.

## Opportunities

- #{n} {title}
- #{n} {title}

## Why these

| Rank | Opportunity | Requests | +1 | Load | Spec |
|---|---|---|---|---|---|
| 1 | #{n} | {requests} | {thumbs} | {load} | S-{NNN} or not merged yet |

{Any departure from the script's order, with its reason. The accepted opportunities left out, and why: over capacity, larger than wip, or waiting on another.}

## To approve

Close this issue as completed with a comment starting "approve". Edit the Opportunities list first to change what goes in. To decline, close it as not planned with a comment saying what to change; the opportunities stay accepted.
```

`roadmap_state.py` reads only the marker line and the Opportunities section: keep the marker's `title` and `due` exact (the due date as `YYYY-MM-DD`), and list each opportunity there as `#N`.

```bash
gh issue create -R {owner/repo} --title "Roadmap: milestone {title}, due {due}" --label milestone-proposal --body-file tmp/milestone-{title}.md
```

Then comment intake's triage line on it, so `triage_state.py` reads it as triaged rather than NEW:

```markdown
Triage: **milestone proposal**, for the maintainer to approve: close it as completed with a comment starting "approve".
```
