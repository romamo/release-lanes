# The needs-decision protocol

A headless session can't ask the user: nobody answers AskUserQuestion or a permission prompt. A session is headless when AskUserQuestion is unavailable, or when its prompt carries the gate's headless paragraph. Every place github-issue-triage, github-issue-resolve, and github-pr-triage would ask with AskUserQuestion (triage's design-gate questions and a PR's "decisions for you", resolve's product decisions and spec departures, pr-triage's denied action and a departure from a `D-n`) follows this protocol instead, and the item waits on GitHub while the rest of the repo keeps moving (spec S-005, D-17).

## Asking

1. Post one comment on the issue or pull request the decision belongs to, through `--body-file` (never an inline `--body`):

   ```markdown
   <!-- shipmill:needs-decision -->
   @<login> Decision needed: <the question, one sentence>

   1. <option> (recommended): <why>
   2. <option>: <what it costs>

   Reply here with a number or your own answer; shipmill takes this up on the tick after your reply.
   ```

   The marker is the comment's first line, with or without an App: it is how `triage_state.py` and `watch_state.py` tell the question from the reply when the session posts as the maintainer. `<login>` is the person the gate's prompt names (the host's `gh` login). Put the recommendation first, and say in each option what it means for users and what it costs, in words a user of the tool knows. Name `D-n` entries and `S-NNN-k` criteria when the question is a departure from one
2. Add the `needs-decision` label to the item: `gh issue edit <n> --add-label needs-decision` (or `gh pr edit`)
3. Leave the item: don't build, push, merge, or comment on it further in this session. Go on with the other items, and report this one under the decisions you left

## A denied tool call is a decision

A headless session runs with an allowlist of tools and can't prompt for more, so Claude Code denies a call outside it. Don't retry it in another form or route around it. Treat the denial as a decision for the user: post the comment above on the item you were working on, naming the tool and the command you tried, with the options (widen the gate's allowlist with `--claude-arg --allowedTools --claude-arg "<tool>"`, or do that step by hand), add the label, and leave the item. The allowlist grows from these comments, in review, instead of from guesses.

## While it waits

The scripts read a labelled item as waiting, not as work: `triage_state.py` reports it as NEEDS_DECISION (no action, so it doesn't make the script exit 1), and `watch_state.py` leaves it out of ISSUES and PRS_OPEN and lists it as `#N` in its NEEDS_DECISION row, which starts no session. It waits until a reply comes:

| | The question | A reply |
|---|---|---|
| With `--bot-login` (the gate's App, spec 004) | The newest comment by that login | A newer comment by an OWNER, MEMBER, or COLLABORATOR other than that login |
| Without it | The newest comment whose first line is the marker | A newer comment by an OWNER, MEMBER, or COLLABORATOR without the marker |

A comment from any other author association never counts as a reply, so an outsider can't wake an item. A labelled item with no question waits until a person takes the label off: someone parked it on purpose.

## Taking up an answered item

Once a trusted person replies, the item reads as the state it would without the label (NEW, NEEDS_PR, UNBLOCKED, and so on), so it is work again. The session that takes it up:

1. Reads the reply. A reply that answers none of the options, or asks something back, is a new question: ask again with this protocol
2. Removes the `needs-decision` label before it acts on the answer: `gh issue edit <n> --remove-label needs-decision` (or `gh pr edit`)
3. Records the answer with `decisions.py add` when it sets a rule, as the [design gate](design-gate.md) says, and goes on
