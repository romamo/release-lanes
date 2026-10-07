# S-005: Headless gate sessions with the needs-decision protocol

status: built

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

Checked on Claude Code 2.1.291 with `claude --help`: a session takes
`--allowedTools <tools...>` and `--disallowedTools <tools...>`, each a comma or space
separated list such as `"Bash(git *) Edit"`. Both tool lists are variadic, so an argument
that follows one is read as another tool name until the next flag.
`--permission-prompts none`, which denies every prompt, applies only with `--print`.

This spec first launched headless sessions with `claude --bg --permission-mode dontAsk`.
The probe for shipmill/shipmill#160 (13 runs on 2.1.291) showed that can't work:

- `dontAsk` denies a Bash call outside the allowlist without a prompt, and the session goes
  on, as expected
- `--disallowedTools AskUserQuestion` removes the tool: it isn't in the tool list and
  ToolSearch can't find it
- `state` in `claude agents --json` doesn't track pending prompts. A run whose last reply
  delivers results reached `done` (6 of 6); a run whose last reply asks the user for
  something read `blocked` and stayed there (4 of 4) with no prompt pending, including
  one that ended with the headless paragraph's "Decision needed (@<login>): ..."

So a `--bg` session that posts its question on GitHub would still read `blocked`, and spec
003's waiting step would hold the repo as WAITING until `max_wait_minutes` passed.
`--bg` and `--print` can't be combined: `claude --bg -p` refuses with "--bg and --print
conflict: --print never starts the interactive session that `claude agents` attaches to".

A headless session therefore runs as `claude -p` (D-17). A print session runs its prompt
and exits; there is no state in which it waits on anyone. It isn't listed by
`claude agents`, so the gate tracks the process itself (Tracking a headless session,
below). `--session-id <uuid>` sets the session's id up front, and `claude --resume <id>`
reopens its conversation after it ends.

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
the prompt. The launchd job reads the mode from the config, so switching modes is a
reviewed config change (the one plist key headless needs is in Tracking a headless
session, below). With
`mode = "interactive"` everything in this spec stays off except the label rule in
`triage_state.py` and `watch_state.py`, which honours a `needs-decision` label whoever set
it.

### The headless launch

On LAUNCH with `mode = "headless"`, `ClaudeCli.launch` in `src/shipmill/gate.py` runs:

```
claude -p --permission-prompts none --allowedTools <ALLOWED> --disallowedTools AskUserQuestion
       --session-id <uuid> [--settings <spec 004's env>] -n <name> [--claude-arg flags] <prompt>
```

`<uuid>` is a new random UUID per launch. The tool lists come before `--session-id`, so
the prompt is never read as a tool name.
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
checks run `npm test`) adds `--claude-arg=--allowedTools --claude-arg "Bash(npm *)"` (with
`=`: argparse refuses a separate `--claude-arg` value that starts with a dash).

A headless gate refuses, with exit 2 before it stops or starts any session, a
`--claude-arg` that is `--permission-mode`, `--permission-prompts`,
`--dangerously-skip-permissions`, `--allow-dangerously-skip-permissions`, `--bg`,
`--background`, or `--session-id` (alone or in `--flag=value` form): each would bring the
prompts back, drop the allowlist, or break how the gate tracks the session.

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

### Tracking a headless session

The gate starts `claude -p` detached from the tick: in a new process session
(`start_new_session`), with stdin from `/dev/null` and stdout and stderr appended to
`<state dir>/sessions/<uuid>.log`, the state directory being the one that holds
`gate.json`. It doesn't wait for it. Right after the start it reads the process's start
time (`ps -p <pid> -o lstart=`) and records, in `gate.json`, the launch's `mode`
(`"headless"`), the session id (`<uuid>`), the `pid`, and that start time, beside the
fingerprint and `at` it records today.

`shipmill launchd` writes `AbandonProcessGroup` as true in the job's plist, in either
mode, so launchd doesn't end a session when the tick that started it exits. A job
installed before this needs `shipmill launchd` run once more; the gate doesn't detect an
old plist.

Each tick, when the last launch is headless, the gate decides whether it still runs from
the record alone: it runs when a process with that `pid` exists and its start time equals
the recorded one (a different start time means the pid was reused). A running headless
session reads RUNNING, reason
`session <uuid> is still working: tail -f <log>`. An ended one is gone: there is nothing
to stop before the next launch, and `claude --resume <uuid>` reopens its conversation. A
headless session never reads WAITING, so spec 003's waiting step and `max_wait_minutes`
don't apply to it.

The gate still reads `claude agents --json` every tick, so a `--bg` session left over
from interactive mode is still handled as spec 003 says. A
`gate.json` without `mode` is an interactive launch. The gate never deletes a session's
log.

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
| With `--bot-login` (an App, spec 004) | The newest comment by that login whose first line is the marker | A newer comment by an OWNER, MEMBER, or COLLABORATOR other than that login, without the marker |
| Without it | The newest comment whose first line is the marker, by an OWNER, MEMBER, or COLLABORATOR | A newer comment by an OWNER, MEMBER, or COLLABORATOR without the marker |

A marker comment from any other author never counts (#184): an outsider can't re-park an
answered item, and with `--bot-login` a bot comment without the marker (a triage comment)
is not a question. With `--bot-login`, when the newest marker comment by that login or by an
OWNER, MEMBER, or COLLABORATOR is a person's, the person asked after the bot did, and the
issue reads as a labelled issue with no question. A labelled issue with no question comment
reads NEEDS_DECISION until the label comes off: a person parked it. An issue with a reply reads the state it would read without the label
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
  notifies the maintainer on GitHub. The gate sends no desktop notification for items; it
  still records the waiting items in `needs-decision.json` and lists them in `decisions`
  (Output) with `notified: false`
- **Without it:** the comment is the maintainer's own, and GitHub doesn't notify anyone of
  their own mention. The gate notifies instead, with spec 003's notifier and keys: on a
  tick that reads the state, for each item in the NEEDS_DECISION row, with
  `notify = true`, at once and then every `remind_hours` while it waits. Title
  `shipmill <owner/repo>`, body `#<n> waits on your decision: https://github.com/<owner/repo>/issues/<n>`
  (GitHub redirects an issue link to a pull request). The record is a third file in the
  state directory, `needs-decision.json`, `{"<n>": {"since": ..., "notified": ...}}`, with
  `waiting.json`'s rules: an entry is dropped once its item no longer waits, a failed send
  is printed and tried next tick, a malformed file exits 2 naming its path, and
  `--dry-run` sends nothing, writes nothing, and prints `would notify #<n> (waiting <N>h)`.
  Only a send or a failed send prints a line: an item that waits and isn't due for a
  reminder prints none. Ticks that don't read the state (HELD, WAITING, RUNNING) don't
  notify for items

A headless launch without `app_id` prints, under its decision line,
`  no app_id: needs-decision comments post as <login>, so GitHub won't notify you`.

A headless session can't block: it ends, and its question waits on GitHub.

### Output

Text output adds one line per notified item, `  notified #<n> (waiting <N>h)` or
`  notify failed for #<n>: <error>`. `--json` adds `"mode"` (`"interactive"` or
`"headless"`, or null on a tick that read no config: HELD, WAITING, RUNNING) and `"decisions": [{"item", "since", "waited_hours", "notified", "error"}]`,
an empty list when nothing waits or the tick didn't read the state.

### Setup

`skills/shipmill-setup/scripts/setup_state.py`:

- With `[agents] mode = "headless"` (since D-21, any `[agents]` section), `needs-decision` joins the labels it wants
  (`LABELS_MISSING` names it when absent, `--fix` creates it with color `d876e3` and the
  description `A shipmill session asked a question here; waits for a reply`). Without
  `[agents]`, it isn't wanted (D-9's reasoning: warn only where the feature is used);
  since D-21 an interactive gate asks this way too, so an interactive setup reads
  `LABELS_MISSING` once, until `--fix`
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
- S-005-2: with `mode = "interactive"`, the gate's `claude --bg` command line and prompt are the ones it builds without this spec, and it calls `watch_state.py` without `--trusted-only` or `--bot-login` (amended by D-21, #206: the prompt ends with the `Gate session:` paragraph naming `@<login>`, so an interactive launch reads the login too)
- S-005-3: on LAUNCH with `mode = "headless"`, the gate runs `claude -p` (never `--bg`) with `--permission-prompts none`, `--allowedTools` with exactly `HEADLESS_TOOLS` as one argument, `--disallowedTools AskUserQuestion`, and `--session-id` with a new UUID, the tool lists before `--session-id`, and the prompt as the last argument
- S-005-4: `docs/design/agent-modes.md`, What Claude Code provides, names the Claude Code version on which a real `claude -p` started with S-005-3's flags, detached as Tracking a headless session says, met a denied Bash call, had no AskUserQuestion, outlived the process that started it, and exited on its own; the build issue that delivers this merges before any other headless code
- S-005-5: with `mode = "headless"`, a `--claude-arg` of `--permission-mode`, `--permission-prompts`, `--dangerously-skip-permissions`, `--allow-dangerously-skip-permissions`, `--bg`, `--background`, or `--session-id` (alone or as `--flag=value`) exits 2 naming the flag, and stops and launches no session
- S-005-6: the headless prompt ends with the headless paragraph naming `@<login>` from `gh api user -q .login` and `references/needs-decision.md`; a failing login read exits 2 and launches nothing
- S-005-7: `triage_state.py` reports an open issue labelled `needs-decision` whose question has no reply as `NEEDS_DECISION`, which doesn't make it exit 1, and an issue with a reply as the state it reads without the label
- S-005-8: a question's first line is `<!-- shipmill:needs-decision -->`; without `--bot-login`, the question is the newest such comment by an OWNER, MEMBER, or COLLABORATOR; with `--bot-login X`, the question is the newest such comment by X, unless a newer one by an OWNER, MEMBER, or COLLABORATOR exists, and then the issue has no question; a reply is a newer comment without the marker by an OWNER, MEMBER, or COLLABORATOR (other than X); a marker comment by any other author is never a question and a comment by any other author association is never a reply; a labelled issue with no question reads `NEEDS_DECISION`
- S-005-9: `watch_state.py --json` leaves waiting issues out of the ISSUES row and waiting pull requests out of PRS_OPEN, and prints a `NEEDS_DECISION` row whose detail lists only `#N` numbers, with `agent: false`; a repo whose only open work waits on a decision reads QUIET at the gate, and a waiting item doesn't change the gate's fingerprint
- S-005-10: with `--trusted-only`, `triage_state.py` reports an open issue whose author is neither an OWNER, MEMBER, or COLLABORATOR nor the `--bot-login` as `UNTRUSTED`, not an action state, and `watch_state.py` counts in PRS_OPEN only pull requests whose head is in the repo, listing the others in an `UNTRUSTED` row with `agent: false`
- S-005-11: a headless gate calls `watch_state.py` with `--trusted-only`, and with `--bot-login <slug>[bot]` when `app_id` is set; when resolving the slug fails, it exits 2, reads no state, and launches no session
- S-005-12: headless without `app_id` and with `notify = true`, a tick that reads the state sends one notification titled `shipmill <owner/repo>` with body `#<n> waits on your decision: https://github.com/<owner/repo>/issues/<n>` for each newly waiting item, records it in `needs-decision.json`, and repeats it only after `remind_hours`; with `notify = false` it sends none and still records the wait
- S-005-13: an entry in `needs-decision.json` whose item no longer waits is dropped on the next tick that reads the state; a failed send prints `notify failed for #<n>: <error>` and leaves `notified` unchanged; a malformed file exits 2 naming its path; `--dry-run` sends nothing, writes no file, and prints `would notify #<n>`
- S-005-14: headless with `app_id` set, the gate sends no desktop notification for a waiting item; headless without `app_id`, a launch prints `no app_id: needs-decision comments post as <login>, so GitHub won't notify you`
- S-005-15: `shipmill gate --json` reports `mode` and a `decisions` list with each notified or waiting item's number, `since`, whole hours waited, whether it was notified, and the failed send's error or null; an empty list when nothing waits
- S-005-16: `setup_state.py` with `[agents] mode = "headless"` reports `needs-decision` under `LABELS_MISSING` when the label is absent and `--fix` creates it; with no `[agents]` it doesn't want the label (amended by D-21, #206: with `interactive` it wants it too); headless with no `app_id` reads `AGENTS_NO_APP`, which leaves the exit code as `AGENTS_OK` would
- S-005-17: `skills/github-issue-triage/references/needs-decision.md` defines the comment (the marker as its first line, the mention, the question, options with the recommendation first), the label, leaving the item, a denied tool call as a decision, and removing the label when an answered item is taken up, and the three SKILL.md files each carry a headless rule that links it
- S-005-18: `docs/design/agent-modes.md`, `skills/shipmill-setup/SKILL.md`, and `docs/install.md` document `mode`, `HEADLESS_TOOLS` and how to widen it, the trust filter, the `needs-decision` label, and notifications with and without `app_id`
- S-005-19: a headless launch starts the session in a new process session with stdin from `/dev/null` and its output appended to `<state dir>/sessions/<uuid>.log`, returns without waiting for it, and records in `gate.json` the `mode`, the session id, the `pid`, and the process's start time beside the fingerprint and `at`; `--dry-run` starts nothing and writes nothing
- S-005-20: when the last launch is headless, a tick reads RUNNING with `session <uuid> is still working: tail -f <log>` while a process with the recorded `pid` and start time exists, and goes on to decide as if no session ran once it doesn't, including when the pid exists with another start time; a headless session never reads WAITING and is never stopped by `max_wait_minutes`; a `gate.json` without `mode` reads as an interactive launch
- S-005-21: `shipmill launchd` writes `AbandonProcessGroup` as true in the plist it installs and in `--print`

## Out of scope

- The trust filter for mode 1: interactive sessions keep working on every author's items, since a person can watch them; agent-modes.md's Next keeps it for that mode
- Comment-level trust: an outsider's comment on a trusted issue still reaches the session, as data under the prompt's untrusted-text line, and can't wake an item
- Approving an untrusted item for headless work (ephemeral mode's `shipmill:go` label): an interactive session handles those
- An allowlist key in `.github/shipmill.toml`: the list is code, widened per host with `--claude-arg`, until the denial comments show what repos need
- Mode 3 (GitHub Actions, `docs/design/ephemeral-mode.md`), budgets, turn limits, and attempt limits
- A `--mode` flag on `shipmill gate` or `shipmill launchd`: the mode is the config's (Config)
- Recording an answer in the decisions log automatically: the session does it with `decisions.py add`, as today
- A time limit on a running headless session: it reads RUNNING until it exits, as an interactive session that keeps working does today
- Removing old session logs: they stay under the state directory until someone deletes them
- Detecting a launchd job installed without `AbandonProcessGroup`

## Decisions relied on

- D-4
- D-9
- D-14
- D-15
- D-16
- D-17

## Issues

- shipmill/shipmill#160: S-005-4
- shipmill/shipmill#161: S-005-7, S-005-8, S-005-9, S-005-10, S-005-17
- shipmill/shipmill#162: S-005-1, S-005-2, S-005-3, S-005-5, S-005-6, S-005-11, S-005-19, S-005-20, S-005-21
- shipmill/shipmill#163: S-005-12, S-005-13, S-005-14, S-005-15
- shipmill/shipmill#164: S-005-16, S-005-18

## Verification

Checked on main plus #191 (#164). The spec merged in #148, with its `claude -p` launch from
#177 and the trusted-question rule from #188; the build issues landed in #178 (#160), #180
(#161), #187 (#162), #190 (#163), and #191 (#164). `specs.py coverage --spec 005` names a
passing test for every criterion. The probe behind S-005-4 ran a real `claude -p` on Claude
Code 2.1.291; every other criterion is checked by its tests, with fakes for `claude`, `gh`,
and the notifier. By hand: `shipmill launchd shipmill/shipmill --print` wrote
`AbandonProcessGroup` true; `shipmill gate` parsed the documented widening,
`--claude-arg=--allowedTools --claude-arg "Bash(npm *)"`, as both flags (a job installed
with one needs #192's fix); and `setup_state.py shipmill/shipmill` on this interactive repo
read the same five rows as before, exit 0.

- S-005-1: `test_s005_1_mode_defaults_to_interactive_and_reads_what_is_given`, `test_s005_1_a_bad_mode_exits_2_naming_the_key`, `test_s005_1_mode_loads_from_the_config_file`, passing
- S-005-2: `test_s005_2_interactive_launches_as_it_did_before`, `test_s005_2_interactive_reads_the_state_without_the_trust_filter`, `test_s005_2_interactive_with_an_app_reads_the_state_without_a_bot_login`, `test_s005_2_interactive_takes_the_flags_headless_refuses`, passing
- S-005-3: `test_s005_3_headless_runs_claude_p_with_the_allowlist`, `test_s005_3_a_widened_allowlist_never_reads_the_prompt_as_a_tool`, `test_s005_3_the_allowlist_is_exactly_the_specs`, `test_s005_3_each_launch_gets_a_new_session_id`, `test_s005_3_with_an_app_the_settings_env_goes_before_the_name`, passing
- S-005-4: the probe on Claude Code 2.1.291 (#160), recorded in agent-modes.md's What Claude Code provides; `test_s005_4_the_design_doc_records_the_print_probe`, passing
- S-005-5: `test_s005_5_the_refused_flags_are_the_specs`, `test_s005_5_a_refused_claude_arg_exits_2_and_stops_and_starts_nothing`, `test_s005_5_a_refused_claude_arg_stops_no_waiting_session_either`, passing
- S-005-6: `test_s005_6_the_headless_prompt_ends_with_the_paragraph_naming_the_login`, `test_s005_6_the_login_is_read_with_gh_api_user`, `test_s005_6_a_login_that_isnt_one_exits_2`, `test_s005_6_no_gh_exits_2`, `test_s005_6_a_failing_login_read_exits_2_and_launches_nothing`, passing
- S-005-7: `test_s005_7_an_unanswered_question_reads_needs_decision_and_is_no_action`, `test_s005_7_a_reply_reads_the_state_it_would_without_the_label`, `test_s005_7_needs_decision_masks_other_states_only_while_it_waits`, passing
- S-005-8: `test_s005_8_without_bot_login_the_question_is_the_newest_marker_comment`, `test_s005_8_a_trusted_comment_is_a_reply`, `test_s005_8_any_other_author_association_never_replies`, `test_s005_8_with_bot_login_the_question_is_the_bots_newest_marker_comment`, `test_s005_8_a_labelled_issue_with_no_question_waits`, `test_s005_8_an_outsiders_marker_comment_never_reparks_an_answered_issue`, `test_s005_8_an_outsiders_marker_comment_alone_is_no_question`, `test_s005_8_with_bot_login_a_persons_newer_question_leaves_no_question`, `test_s005_8_with_bot_login_a_bot_question_then_a_trusted_reply_no_longer_waits`, `test_s005_8_with_bot_login_a_plain_bot_comment_is_no_question`, `test_s005_8_the_rule_reads_the_comments_raw`, `test_s005_8_a_pr_follows_the_trusted_question_rule`, passing
- S-005-9: `test_s005_9_waiting_items_leave_issues_and_prs_open_for_a_needs_decision_row`, `test_s005_9_a_waiting_pr_follows_the_bot_login`, `test_s005_9_a_repo_whose_only_work_waits_reads_quiet_at_the_gate`, `test_s005_9_a_waiting_item_does_not_change_the_gates_fingerprint`, passing
- S-005-10: `test_s005_10_trusted_only_reads_an_outsiders_issue_as_untrusted`, `test_s005_10_trusted_authors_issues_read_as_usual`, `test_s005_10_the_bots_own_issue_is_trusted`, `test_s005_10_a_deleted_author_is_untrusted`, `test_s005_10_trusted_only_lists_fork_prs_as_untrusted`, passing
- S-005-11: `test_s005_11_headless_reads_the_state_with_the_trust_filter`, `test_s005_11_headless_with_an_app_passes_its_bot_login`, `test_s005_11_watch_state_takes_the_flags_the_gate_passes`, `test_s005_11_a_failing_app_check_reads_no_state_and_launches_nothing`, `test_s005_11_headless_with_app_id_and_no_app_check_reads_no_state`, passing
- S-005-12: `test_s005_12_a_newly_waiting_item_is_notified_once_and_recorded`, `test_s005_12_it_repeats_only_after_remind_hours`, `test_s005_12_a_new_item_beside_a_notified_one_is_notified_alone`, `test_s005_12_with_notify_false_it_sends_none_and_still_records`, `test_s005_12_a_launching_tick_notifies_too`, `test_s005_12_a_tick_that_reads_no_state_neither_notifies_nor_touches_the_file`, `test_s005_12_interactive_mode_neither_notifies_nor_writes_the_file`, passing
- S-005-13: `test_s005_13_an_item_that_no_longer_waits_is_dropped`, `test_s005_13_the_file_goes_once_nothing_waits`, `test_s005_13_a_failed_send_is_printed_and_leaves_notified_unchanged`, `test_s005_13_a_malformed_file_exits_2_naming_its_path`, `test_s005_13_a_dry_run_sends_nothing_writes_nothing_and_says_it_would_notify`, `test_s005_13_a_dry_run_leaves_a_file_with_nothing_waiting`, `test_s005_13_a_watch_row_that_lists_anything_but_numbers_exits_2`, passing
- S-005-14: `test_s005_14_with_app_id_no_desktop_notification_for_an_item`, `test_s005_14_a_headless_launch_without_app_id_warns_github_wont_notify`, `test_s005_14_an_interactive_launch_prints_no_such_warning`, passing
- S-005-15: `test_s005_15_json_reports_mode_and_each_waiting_items_decision`, `test_s005_15_json_reports_an_empty_list_when_nothing_waits`, `test_s005_15_interactive_json_reports_its_mode`, `test_s005_15_a_tick_that_reads_no_config_reports_no_mode`, `test_s005_15_the_session_waiting_step_keeps_its_file_and_output`, passing
- S-005-16: `test_s005_16_headless_wants_the_needs_decision_label`, `test_s005_16_interactive_or_no_agents_doesnt_want_the_label`, `test_s005_16_a_headless_mode_outside_agents_doesnt_count`, `test_s005_16_fix_creates_the_label_only_when_wanted`, `test_s005_16_headless_without_app_id_reads_agents_no_app_counted_done`, `test_s005_16_the_agents_mode_never_reads_as_the_release_mode`, passing; the existing setup_state tests for interactive and no-`[agents]` configs pass unchanged
- S-005-17: `test_s005_17_the_reference_defines_the_protocol`, `test_s005_17_each_skill_carries_a_headless_rule_linking_it`, passing
- S-005-18: read the three docs; `test_s005_18_the_docs_document_headless_mode` asserts each topic and every tool of `HEADLESS_TOOLS`, and `test_s005_18_the_needs_decision_reference_widens_with_the_working_flag`, passing
- S-005-19: `test_s005_19_a_headless_launch_is_detached_logged_and_recorded`, `test_s005_19_a_dry_run_starts_and_writes_nothing`, `test_s005_19_the_session_runs_in_a_new_session_with_no_stdin_appending_its_log`, `test_s005_19_the_launch_returns_without_waiting_and_its_start_time_is_read`, `test_s005_19_a_command_that_cant_start_exits_2`, passing
- S-005-20: `test_s005_20_a_running_headless_session_reads_running`, `test_s005_20_it_still_reads_running_after_the_mode_goes_back_to_interactive`, `test_s005_20_an_ended_session_or_a_reused_pid_decides_as_if_none_ran`, `test_s005_20_a_session_whose_start_time_wasnt_read_is_not_running`, `test_s005_20_a_headless_session_never_waits_and_is_never_stopped`, `test_s005_20_a_record_without_mode_is_an_interactive_launch`, `test_s005_20_a_malformed_headless_record_exits_2`, `test_s005_20_a_headless_record_needs_a_uuid_session`, `test_s005_20_a_headless_record_whose_session_isnt_a_string_exits_2`, passing
- S-005-21: `test_s005_21_launchd_abandons_the_process_group`, passing; the `launchd --print` above showed `AbandonProcessGroup` true
