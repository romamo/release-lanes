# S-005: Headless gate sessions with the needs-decision protocol

status: approved

## Problem

In mode 1 (`docs/design/agent-modes.md`), a session `shipmill gate` starts with
`claude --bg` asks the user through AskUserQuestion, or stops on a permission prompt, and
waits. Until someone answers, the gate starts nothing else for that repo: every tick reads
WAITING. Spec 003 made the wait visible (a desktop notification) and boundable
(`max_wait_hours`, now `max_wait_minutes`, D-15), but on a machine nobody watches, the
repo still stalls until a person attaches.

This repo's own gate shows how often. Its launchd job (a 15 minute `StartInterval`) logged
50 ticks between its install on 2026-10-05 and 2026-10-06 10:09. It launched two sessions;
the second blocked, and the gate reported WAITING on that one session for 39 ticks in a
row, about ten hours and 78% of all ticks, and it was still waiting at the last one.
`claude agents` shows a blocked session the same whether it waits on a question or on a
permission prompt, so the log can't tell which; mode 2 has to remove both.

shipmill/shipmill#134, filed by the maintainer, asks for mode 2: a headless session that
can't ask, so a decision for the user becomes a comment and a `needs-decision` label on
the issue or pull request (the protocol of `docs/design/ephemeral-mode.md`, Human
decisions), the item waits on GitHub, and the rest of the repo keeps moving. Spec 004 (the
App identity, D-14) settles how the waiting rule tells the bot's question from the
maintainer's reply.

## Behaviour

### What Claude Code provides

Checked on Claude Code 2.1.291 with `claude --help`: `--bg` takes the session flags
`--permission-mode` (with the choice `dontAsk`), `--allowedTools <tools...>`, and
`--disallowedTools <tools...>`, each a comma or space separated list such as
`"Bash(git *) Edit"`. Both tool lists are variadic, so an argument that follows one is read
as another tool name until the next flag. `--permission-prompts none`, which denies every
prompt, applies only with `--print`, so it can't serve a `--bg` session.

In `dontAsk` mode Claude Code denies a tool call that no allow rule covers instead of
prompting, and the session goes on with the denial as the call's result. The help text
lists the mode without describing it. A probe session couldn't run where this spec was
written, so the build's first issue checks on a real `claude --bg` that a denied call and
a disallowed AskUserQuestion leave the session `working`, never `blocked`, before any
other headless code merges.

### Config

One new key in the `[agents]` section of `.github/shipmill.toml` (D-4), parsed by
`AgentsConfig` in `src/shipmill/agents.py`:

```toml
[agents]
mode = "headless"   # interactive (default): sessions ask you; headless: they ask on GitHub
```

| Key | Type | Default | Allowed |
|---|---|---|---|
| `mode` | string | `"interactive"` | `"interactive"`, `"headless"` |

The gate reads it each tick from the checkout as it stands after `--refresh`, as it reads
the prompt. `shipmill launchd` doesn't change: the job reads the mode from the config, so
switching modes is a reviewed config change and no job needs reinstalling. With
`mode = "interactive"` everything in this spec stays off except the label rule in
`triage_state.py` and `watch_state.py`, which honours a `needs-decision` label whoever set
it.

### The headless launch

On LAUNCH with `mode = "headless"`, `ClaudeCli.launch` in `src/shipmill/gate.py` runs:

```
claude --bg --permission-mode dontAsk --allowedTools <ALLOWED> --disallowedTools AskUserQuestion
       [--settings <spec 004's env>] -n <name> [--claude-arg flags] <prompt>
```

The permission flags come before `-n`, so the prompt is never read as a tool name.
`<ALLOWED>` is one argument, a fixed constant `HEADLESS_TOOLS` in `src/shipmill/gate.py`:

```
Read Edit Write Glob Grep Skill Agent SendMessage ListAgents TodoWrite
Bash(gh *) Bash(git *) Bash(uv *) Bash(uvx *)
```

These are the tools the three skills' instructions call: the file tools, the skills and
subagents they dispatch, peer messages, and `gh`, `git`, the skill scripts (`uv run
--no-project python ...`), and the check commands (`uv run ...`, `uvx ...`). The allowlist
keeps a prompt from blocking the session; it is not a sandbox. `git`, `gh`, and `uv run`
can each run arbitrary code, so the trust filter below, not the allowlist, keeps an
outsider's text from steering the session. A host that needs more tools (a repo whose
checks run `npm test`) adds `--claude-arg --allowedTools --claude-arg "Bash(npm *)"`.

A headless gate refuses, with exit 2 before it stops or starts any session, a
`--claude-arg` that is `--permission-mode`, `--dangerously-skip-permissions`, or
`--allow-dangerously-skip-permissions` (alone or in `--flag=value` form): each would bring
the prompts back or drop the allowlist.

The headless prompt is the mode 1 prompt with one paragraph added at its end:

```
Headless: nobody can answer AskUserQuestion or a permission prompt. A decision for the
user, or a tool call that was denied, becomes the needs-decision protocol
(github-issue-triage's references/needs-decision.md): mention @<login>, label the item
needs-decision, and leave it.
```

`<login>` is the host's `gh` login (`gh api user -q .login`), read by the gate with the
host identity as its other reads are (S-004-12): the person who runs the gate is the one
who decides. A failing read exits 2 and launches nothing.

### When a session needs a tool outside the list

It can't prompt: Claude Code denies the call. The skill then treats the denial as a
decision for the user: it posts a `needs-decision` comment on the item it was working on,
naming the tool and the command it tried, with the options (widen the allowlist with
`--claude-arg`, or do that step by hand), and leaves the item. The maintainer's answer, or
a widened allowlist, makes it work again on a later tick. The allowlist grows from these
comments, in review, instead of from guesses.

### The needs-decision protocol, in the skills

A new reference, `skills/github-issue-triage/references/needs-decision.md`, defines it once;
`skills/github-issue-triage/SKILL.md`, `skills/github-issue-resolve/SKILL.md`, and
`skills/github-pr-triage/SKILL.md` each gain a headless rule that links it. A session is
headless when AskUserQuestion is unavailable or its prompt carries the headless paragraph.
Every place those skills ask with AskUserQuestion (triage's design-gate questions and a
PR's "decisions for you", resolve's product decisions and spec departures, pr-triage's
denied action and a departure from a `D-n`) instead:

1. Posts one comment on the issue or pull request, through `--body-file`:

   ```
   <!-- shipmill:needs-decision -->
   @<login> Decision needed: <the question, one sentence>

   1. <option> (recommended): <why>
   2. <option>: <what it costs>

   Reply here with a number or your own answer; shipmill takes this up on the tick after
   your reply.
   ```

   The marker is the comment's first line, with or without an App

2. Adds the `needs-decision` label to the item
3. Leaves that item, goes on with the others, and reports it under the decisions it left

A session that takes up an answered item (one that reads as work again, below) reads the
reply, removes the `needs-decision` label before it acts, and records the answer with
`decisions.py add` when it sets a rule, as the design gate already says.

### Waiting on GitHub

`skills/github-issue-triage/scripts/triage_state.py` gains a state and two flags:

- `NEEDS_DECISION`: an open issue labelled `needs-decision` whose question has no reply.
  It isn't an action state, so it doesn't count toward the exit code
- `--bot-login <login>`: the login shipmill's sessions write as
- `--trusted-only`: the trust filter (Trust, below)

The question and the reply are found by the comments' author, `author_association`, and
marker:

| | The question | A reply |
|---|---|---|
| With `--bot-login` (an App, spec 004) | The newest comment by that login | A newer comment by an OWNER, MEMBER, or COLLABORATOR other than that login |
| Without it | The newest comment whose first line is the marker | A newer comment by an OWNER, MEMBER, or COLLABORATOR without the marker |

A labelled issue with no question comment reads NEEDS_DECISION until the label comes off: a
person parked it. An issue with a reply reads the state it would read without the label
(NEW, NEEDS_PR, UNBLOCKED, and so on), so it is work again. A comment from any other
author association never counts as a reply, so an outsider can't wake an item.

`skills/github-ship-watch/scripts/watch_state.py` passes `--bot-login` and
`--trusted-only` through to `triage_state.py`, applies the same rule to open pull requests
(their comments and labels), and:

- leaves NEEDS_DECISION issues out of the ISSUES row, and waiting pull requests out of
  PRS_OPEN, so neither starts a session nor enters the gate's fingerprint
- adds a `NEEDS_DECISION` row whose detail lists the waiting items as `#N` only, never a
  title, with `agent: false`. It is in `ACTION` (a person owes it, like PROMOTION_DUE), not
  in `AGENT`

### Trust

An unattended session reads issue text with the whole workspace in reach, and with
`Bash(git *)` and `uv run` allowed it can run code. Mode 2 therefore works only on items
trusted people opened (D-16, added with this spec), inside this spec (the "Trust filter" of
agent-modes.md's Next, for headless mode only):

- An issue is trusted when its author's `author_association` is OWNER, MEMBER, or
  COLLABORATOR, or its author is the `--bot-login` (the App files build issues and flake
  issues). With `--trusted-only`, `triage_state.py` reports any other open issue as
  `UNTRUSTED`, which isn't an action state, whatever it would read otherwise
- A pull request is trusted when its head is in the repo itself (`isCrossRepository` is
  false): only people and Apps with push access can put a branch there. With
  `--trusted-only`, `watch_state.py` counts only those in PRS_OPEN and reports the rest
  in an `UNTRUSTED` row (detail `#N` only, `agent: false`, not in `ACTION`)

The headless gate always passes `--trusted-only`. Untrusted items wait for an interactive
session; github-ship-watch's report lists them. Mode 1 doesn't filter.

### The gate's state read

In headless mode, the gate calls `watch_state.py` with `--trusted-only`, and with
`--bot-login <slug>[bot]` when `app_id` is set. To know the slug, a headless tick that
reads the state first runs spec 004's launch steps 1 to 3 (the key, the JWT, `GET /app`);
a failure exits 2, reads no state, and launches nothing (D-14). Without `app_id` it passes
no `--bot-login`, and the marker tells the question from the reply.

### Notifications

- **With `app_id`:** the question comes from `<slug>[bot]`, so its `@<login>` mention
  notifies the maintainer on GitHub. The gate sends no desktop notification for items
- **Without it:** the comment is the maintainer's own, and GitHub doesn't notify anyone of
  their own mention. The gate notifies instead, with spec 003's notifier and keys: on a
  tick that reads the state, for each item in the NEEDS_DECISION row, with
  `notify = true`, at once and then every `remind_hours` while it waits. Title
  `shipmill <owner/repo>`, body `#<n> waits on your decision: https://github.com/<owner/repo>/issues/<n>`
  (GitHub redirects an issue link to a pull request). The record is a third file in the
  state directory, `needs-decision.json`, `{"<n>": {"since": ..., "notified": ...}}`, with
  `waiting.json`'s rules: an entry is dropped once its item no longer waits, a failed send
  is printed and tried next tick, a malformed file exits 2 naming its path, and
  `--dry-run` sends nothing, writes nothing, and prints `would notify #<n>`. Ticks that
  don't read the state (HELD, WAITING, RUNNING) don't notify for items

A headless launch without `app_id` prints, under its decision line,
`  no app_id: needs-decision comments post as <login>, so GitHub won't notify you`.

A session that still blocks in headless mode (a Claude Code change, say) is handled by
spec 003's waiting step, unchanged.

### Output

Text output adds one line per notified item, `  notified #<n> (waiting <N>h)` or
`  notify failed for #<n>: <error>`. `--json` adds `"mode"` (`"interactive"` or
`"headless"`) and `"decisions": [{"item", "since", "waited_hours", "notified", "error"}]`,
an empty list when nothing waits or the tick didn't read the state.

### Setup

`skills/shipmill-setup/scripts/setup_state.py`:

- With `[agents] mode = "headless"`, `needs-decision` joins the labels it wants
  (`LABELS_MISSING` names it when absent, `--fix` creates it with color `d876e3` and the
  description `A shipmill session asked a question here; waits for a reply`). With
  `interactive` or no `[agents]`, it isn't wanted, so existing setups see no new row
  (D-9's reasoning: warn only where the feature is used)
- With `mode = "headless"` and no `app_id`, the agents row reads `AGENTS_NO_APP`, counted as
  done so it doesn't change the exit code, with the detail
  `headless without app_id: needs-decision comments post as you, so GitHub won't notify you; desktop notifications only`

### Docs

- `docs/design/agent-modes.md`: mode 2's row marked built, the `mode` key in the config
  block, the allowlist and what a denied call does, the trust filter for headless, and
  Next updated
- `skills/shipmill-setup/SKILL.md`, The gate, and `docs/install.md`: `mode`, the allowlist
  and how to widen it, the trust filter, the `needs-decision` label, and notifications with
  and without an App

## Acceptance criteria

- S-005-1: `AgentsConfig` reads `mode` as `"interactive"` when `[agents]` omits it and as the given value when it is `"interactive"` or `"headless"`, and refuses any other value or a non-string with exit 2 naming the key
- S-005-2: with `mode = "interactive"`, the gate's `claude --bg` command line and prompt are the ones it builds without this spec, and it calls `watch_state.py` without `--trusted-only` or `--bot-login`
- S-005-3: on LAUNCH with `mode = "headless"`, `claude --bg` gets `--permission-mode dontAsk`, `--allowedTools` with exactly `HEADLESS_TOOLS` as one argument, and `--disallowedTools AskUserQuestion`, all before `-n <name>`, and the prompt is the last argument
- S-005-4: `docs/design/agent-modes.md`, What Claude Code provides, names the Claude Code version on which a real `claude --bg` started with these flags met a denied Bash call and a disallowed AskUserQuestion and stayed `working` until `done`, never `blocked`; the build issue that delivers this merges before any other headless code
- S-005-5: with `mode = "headless"`, a `--claude-arg` of `--permission-mode`, `--dangerously-skip-permissions`, or `--allow-dangerously-skip-permissions` (alone or as `--flag=value`) exits 2 naming the flag, and stops and launches no session
- S-005-6: the headless prompt ends with the headless paragraph naming `@<login>` from `gh api user -q .login` and `references/needs-decision.md`; a failing login read exits 2 and launches nothing
- S-005-7: `triage_state.py` reports an open issue labelled `needs-decision` whose question has no reply as `NEEDS_DECISION`, which doesn't make it exit 1, and an issue with a reply as the state it reads without the label
- S-005-8: without `--bot-login`, the question is the newest comment whose first line is `<!-- shipmill:needs-decision -->` and a reply is a newer comment without it by an OWNER, MEMBER, or COLLABORATOR; with `--bot-login X`, the question is the newest comment by X and a reply is a newer comment by an OWNER, MEMBER, or COLLABORATOR other than X; in both, a comment by any other author association is never a reply, and a labelled issue with no question reads `NEEDS_DECISION`
- S-005-9: `watch_state.py --json` leaves waiting issues out of the ISSUES row and waiting pull requests out of PRS_OPEN, and prints a `NEEDS_DECISION` row whose detail lists only `#N` numbers, with `agent: false`; a repo whose only open work waits on a decision reads QUIET at the gate, and a waiting item doesn't change the gate's fingerprint
- S-005-10: with `--trusted-only`, `triage_state.py` reports an open issue whose author is neither an OWNER, MEMBER, or COLLABORATOR nor the `--bot-login` as `UNTRUSTED`, not an action state, and `watch_state.py` counts in PRS_OPEN only pull requests whose head is in the repo, listing the others in an `UNTRUSTED` row with `agent: false`
- S-005-11: a headless gate calls `watch_state.py` with `--trusted-only`, and with `--bot-login <slug>[bot]` when `app_id` is set; when resolving the slug fails, it exits 2, reads no state, and launches no session
- S-005-12: headless without `app_id` and with `notify = true`, a tick that reads the state sends one notification titled `shipmill <owner/repo>` with body `#<n> waits on your decision: https://github.com/<owner/repo>/issues/<n>` for each newly waiting item, records it in `needs-decision.json`, and repeats it only after `remind_hours`; with `notify = false` it sends none and still records the wait
- S-005-13: an entry in `needs-decision.json` whose item no longer waits is dropped on the next tick that reads the state; a failed send prints `notify failed for #<n>: <error>` and leaves `notified` unchanged; a malformed file exits 2 naming its path; `--dry-run` sends nothing, writes no file, and prints `would notify #<n>`
- S-005-14: headless with `app_id` set, the gate sends no desktop notification for a waiting item; headless without `app_id`, a launch prints `no app_id: needs-decision comments post as <login>, so GitHub won't notify you`
- S-005-15: `shipmill gate --json` reports `mode` and a `decisions` list with each notified or waiting item's number, `since`, whole hours waited, whether it was notified, and the failed send's error or null; an empty list when nothing waits
- S-005-16: `setup_state.py` with `[agents] mode = "headless"` reports `needs-decision` under `LABELS_MISSING` when the label is absent and `--fix` creates it; with `interactive` or no `[agents]` it doesn't want the label; headless with no `app_id` reads `AGENTS_NO_APP`, which leaves the exit code as `AGENTS_OK` would
- S-005-17: `skills/github-issue-triage/references/needs-decision.md` defines the comment (the marker as its first line, the mention, the question, options with the recommendation first), the label, leaving the item, a denied tool call as a decision, and removing the label when an answered item is taken up, and the three SKILL.md files each carry a headless rule that links it
- S-005-18: `docs/design/agent-modes.md`, `skills/shipmill-setup/SKILL.md`, and `docs/install.md` document `mode`, `HEADLESS_TOOLS` and how to widen it, the trust filter, the `needs-decision` label, and notifications with and without `app_id`

## Out of scope

- The trust filter for mode 1: interactive sessions keep working on every author's items, since a person can watch them; agent-modes.md's Next keeps it for that mode
- Comment-level trust: an outsider's comment on a trusted issue still reaches the session, as data under the prompt's untrusted-text line, and can't wake an item
- Approving an untrusted item for headless work (ephemeral mode's `shipmill:go` label): an interactive session handles those
- An allowlist key in `.github/shipmill.toml`: the list is code, widened per host with `--claude-arg`, until the denial comments show what repos need
- Mode 3 (GitHub Actions, `docs/design/ephemeral-mode.md`), budgets, turn limits, and attempt limits
- A `--mode` flag on `shipmill gate` or `shipmill launchd`: the mode is the config's (Config)
- Recording an answer in the decisions log automatically: the session does it with `decisions.py add`, as today

## Decisions relied on

- D-4
- D-9
- D-14
- D-15
- D-16

## Issues

- shipmill/shipmill#160: S-005-4
- shipmill/shipmill#161: S-005-7, S-005-8, S-005-9, S-005-10, S-005-17
- shipmill/shipmill#162: S-005-1, S-005-2, S-005-3, S-005-5, S-005-6, S-005-11
- shipmill/shipmill#163: S-005-12, S-005-13, S-005-14, S-005-15
- shipmill/shipmill#164: S-005-16, S-005-18

## Verification
