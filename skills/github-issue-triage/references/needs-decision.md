# The needs-decision protocol

A session the shipmill gate started can't count on an answer in the session: it runs in the background, and nobody may attach. So every question a gate session asks goes to GitHub first, interactive or headless (D-21), and the item waits there while the rest of the repo keeps moving (spec S-005, D-17).

A session is a **gate session** when its prompt carries one of the gate's paragraphs:

- **`Gate session:`** an interactive session (`claude --bg`). Post the question with this protocol, then ask the same question in the session with AskUserQuestion too
- **`Headless:`** a headless session (`claude -p`), or any session where AskUserQuestion is unavailable. Nobody answers AskUserQuestion or a permission prompt: post the question with this protocol and leave the item

A session the user started by hand, whose prompt carries neither paragraph, asks with AskUserQuestion as usual and posts no marker or label.

Every place github-issue-triage, github-issue-resolve, and github-pr-triage would ask the user (triage's design-gate questions and a PR's "decisions for you", resolve's product decisions and spec departures, pr-triage's denied action and a departure from a `D-n`) follows this protocol in a gate session, before anything else.

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
2. Add the `needs-decision` label to the item: `gh issue edit <n> --add-label needs-decision` (or `gh pr edit`). From now on the scripts read the item as waiting, not as work, so the gate doesn't start a session for it again
3. **Interactive (`Gate session:`) only:** ask the same question, with the same options in the same order, with AskUserQuestion. An answer in the session: follow [Answered in the session](#answered-in-the-session). No answer: the item already waits on GitHub, and the gate's `max_wait_minutes` stops the waiting session (D-15); whoever answers on GitHub later is read as below
4. **Headless:** Leave the item: don't build, push, merge, or comment on it further in this session. Go on with the other items, and report this one under the decisions you left

A triage verdict that waits on a decision says so in its first line ("Waits on your decision below") and leaves the **Decision needed** block out: the block is this comment, posted right after the verdict, since a comment can't start with both `Triage:` and the marker.

## Answered in the session

An interactive gate session that gets the answer through AskUserQuestion puts the record on GitHub before it acts, so the item doesn't wait on a question already answered and the thread shows who decided:

1. Post the answer on the item as a comment without the marker, through `--body-file`. Posted as the App (`shipmill gh` with `[agents] app_id` set), its first line names who decided and who relayed it, so the thread tells a person's decision from agent text:

   ```markdown
   Decision by @<login>, relayed by <agent>

   Answered in the session: <the option or the user's own words>
   ```

   `<login>` is the person who answered, `<agent>` the tool that relays it (`Claude Code`)
2. Remove the `needs-decision` label: `gh issue edit <n> --remove-label needs-decision` (or `gh pr edit`)
3. Record the answer with `decisions.py add` when it sets a rule, as the [design gate](design-gate.md) says, and go on

## A denied tool call is a decision

A headless session runs with an allowlist of tools and can't prompt for more, so Claude Code denies a call outside it.

**One allowlisted command per Bash call.** Claude Code checks each part of a compound command on its own, and one part that isn't on the list denies the whole call, which reads like "Bash was denied". So a headless session runs one command per Bash call: `git -C <dir> ...`, not `cd <dir> && ...`; no variable assignments, no `;` or `&&` chains, and no pipes into tools that aren't on the list (use `gh --jq` and the Read and Grep tools instead). When a denied call chained commands and every command the step needs is on the list, rerun them as separate calls: that isn't routing around the list, since each call passes it on its own.

A command that isn't on the list is different. Don't retry it in another form or route around it. Treat the denial as a decision for the user: post the comment above on the item you were working on, naming the tool and the command you tried, with the options (widen the gate's allowlist with `--claude-arg=--allowedTools --claude-arg "<tool>"` on `shipmill gate` or `shipmill launchd`, or do that step by hand), add the label, and leave the item. The allowlist grows from these comments, in review, instead of from guesses.

## While it waits

The scripts read a labelled item as waiting, not as work: `triage_state.py` reports it as NEEDS_DECISION (no action, so it doesn't make the script exit 1), and `watch_state.py` leaves it out of ISSUES and PRS_OPEN and lists it as `#N` in its NEEDS_DECISION row, which starts no session. It waits until a reply comes:

| | The question | A reply |
|---|---|---|
| With `--bot-login` (the gate's App, spec 004) | The newest comment by that login whose first line is the marker | A newer comment by an OWNER, MEMBER, or COLLABORATOR other than that login, without the marker |
| Without it | The newest comment whose first line is the marker, by an OWNER, MEMBER, or COLLABORATOR | A newer comment by an OWNER, MEMBER, or COLLABORATOR without the marker |

A marker comment from any other author never counts, so an outsider can't re-park an answered item, and with `--bot-login` the bot's comment without the marker (a triage comment) is no question. With `--bot-login`, when an OWNER, MEMBER, or COLLABORATOR posts a marker comment after the bot's, the person asked after the bot did, and the item reads as labelled with no question. A comment from any other author association never counts as a reply, so an outsider can't wake an item. A labelled item with no question waits until a person takes the label off: someone parked it on purpose.

## Taking up an answered item

Once a trusted person replies, the item reads as the state it would without the label (NEW, NEEDS_PR, UNBLOCKED, and so on), so it is work again. The session that takes it up:

1. Reads the reply. A reply that answers none of the options, or asks something back, is a new question: ask again with this protocol
2. Removes the `needs-decision` label before it acts on the answer: `gh issue edit <n> --remove-label needs-decision` (or `gh pr edit`)
3. Records the answer with `decisions.py add` when it sets a rule, as the [design gate](design-gate.md) says, and goes on
