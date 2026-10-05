# S-003: Notify when a gated session waits on you

status: approved

## Problem

A session `shipmill gate` starts with `claude --bg` can stop on a question
(AskUserQuestion). Until someone answers, the gate starts nothing else for that repo:
`busy()` runs first in `decide()` (`src/shipmill/gate.py`), so every tick returns WAITING
and `retry_hours` never applies. The only signs are `state: blocked` in `claude agents`
and a `session <id> waits on you: claude attach <id>` line in the gate's log on every tick.
Nobody watches either, so a session can wait indefinitely while triage, landing, and
shipped notices stop for the whole repo (releases keep going). shipmill/shipmill#119, filed
by the maintainer, asks for a desktop notification that repeats while the session waits,
and an optional limit after which the gate stops the session so the repo moves again.

## Behaviour

### What Claude Code shows of a waiting session

Verified on Claude Code 2.1.289. `claude agents --json` lists a waiting background
session with `id`, `name`, `cwd`, `pid`, `startedAt` (when the session started, not when
it blocked), `status: idle`, and `state: blocked`. It does not show the pending question,
when the session became blocked, or which issue or pull request it is working on.
`claude logs <id>` prints raw terminal output with escape codes, not a question the gate
could read reliably, and its text is the session's (and through it, issue authors'), so
the gate never reads or forwards it. `claude stop <id>` ends a background session and
keeps its conversation; `claude attach <id>` opens it again, question included.

So the gate measures a wait itself, from the first tick that saw the session blocked, and
what it reports about a stopped session is what it knows: the session's id and name, how
long it waited, and `claude attach <id>`, which shows the question.

### Config

Three new keys in the `[agents]` section of `.github/shipmill.toml` (D-4), parsed by
`AgentsConfig` in `src/shipmill/agents.py` with the existing `Table` helpers of
`src/shipmill/config.py`:

```toml
[agents]
notify = true        # a desktop notification when a session waits on you
remind_hours = 4     # repeat it while the session still waits
max_wait_hours = 0   # stop a session that waited this long; 0: never
```

| Key | Type | Default | Allowed |
|---|---|---|---|
| `notify` | boolean | `true` | `true`, `false` |
| `remind_hours` | integer | `4` | 1..168 |
| `max_wait_hours` | integer | `0` | 0..168 |

A value of the wrong type or outside its range, and any key outside `prompt`, `prs`,
`retry_hours`, and these three, is refused: the gate exits 2 naming the key, as today.
`remind_hours` larger than `max_wait_hours` is allowed (the stop comes first).

### The wait record

A second file next to the launch record, `$(git rev-parse --git-common-dir)/shipmill/waiting.json`
(the directory `state_dir()` returns), one entry per blocked session of this repo:

```json
{"6b976131": {"since": "2026-10-05T19:00:00+00:00", "notified": "2026-10-05T19:00:00+00:00"}}
```

`since` is the first tick that saw the session blocked; `notified` is the last notification
that was sent for it, `null` while none was. `gate.json` keeps its format. An entry is
dropped once its session is no longer blocked (answered, finished, stopped, or gone), so a
session that blocks again starts a new wait. A file that isn't this shape exits 2 naming
its path and saying to delete it, as `load_launch` does for `gate.json`. Losing the file
restarts every wait at the next tick: at most one extra notification and a later stop.

### Each tick

The waiting step runs in `gate()` after the sessions are listed and before the hold check
(so after the prune of spec 002, which also runs before it). Sessions are this repo's gate
sessions, as `parse_sessions` returns them. On a tick where none is blocked, the step only
drops the record's entries and reads neither the config nor anything else; the tick then
runs as today. On a tick where some are blocked, it reads `[agents]` from the checkout as
it stands (a busy tick still never moves the checkout), loads the record, and for each
blocked session, with `waited = now - since`:

1. **Stop**, when `max_wait_hours > 0` and `waited >= max_wait_hours`: `claude stop <id>`
   through the existing `Claude.stop`, drop its entry, print the stop line, and send the
   stopped notification when `notify` is true. A failing `claude stop` exits 2 like any
   other gate error, starts no session, and keeps the entry, so the next tick tries again
2. **Notify**, otherwise, when `notify` is true and `notified` is null or at least
   `remind_hours` ago: send the waiting notification. A sent one sets `notified` to now; a
   failed one is printed with the session id and the error, leaves `notified` as it was
   (so the next tick tries again), and changes neither the decision nor the exit code

Then it writes the record and the tick goes on as today with the stopped sessions left
out: the hold check, `busy()`, and `decide()`. A tick that stopped the only blocked session
can therefore launch, under the usual rules: the same findings as the last launch within
`retry_hours` still read UNCHANGED. The waiting step runs on HELD ticks too (D-13).

The gate writes nothing to GitHub for a waiting or stopped session: it doesn't know which
issue or pull request the session was on (the launch prompt lists every finding, and a
session can touch several), and it can't read the question.

### Notifications

A notifier in `src/shipmill/gate.py` (or a new module `src/shipmill/notify.py`) sends a
title and a body through an injectable command runner, chosen once per run:

| Where | Command |
|---|---|
| macOS (`sys.platform == "darwin"`) | `osascript -e 'on run argv' -e 'display notification (item 2 of argv) with title (item 1 of argv)' -e 'end run' <title> <body>` |
| Elsewhere, with `notify-send` on `PATH` | `notify-send <title> <body>` |
| Elsewhere, without it | none: the send fails with `no notifier (osascript or notify-send)` |

The title and body are passed as arguments, never put into the AppleScript source.
`osascript` ships with macOS; `terminal-notifier` would be an extra dependency, so it isn't
used, and since `osascript` offers no action button, the body carries the command:

- Waiting: title `shipmill <owner/repo>`, body `session <id> waits on you (<N>h): claude attach <id>`
- Stopped: title `shipmill <owner/repo>`, body `stopped session <id> after <N>h waiting: claude attach <id> shows its question`

`<N>` is the whole hours waited, rounded down. A send fails when the command is missing,
exits non-zero, or runs longer than 30 seconds.

### Output

Text output adds one line per blocked session under the decision line:

- `  notified <id> (waiting <N>h)`
- `  notify failed for <id>: <error>`
- `  stopped <id> after <N>h waiting: <name>`
- `  <id> waiting <N>h` when it did neither

`--json` adds `"waiting": [{"session", "name", "since", "waited_hours", "notified",
"stopped", "error"}]`, one object per blocked session (`notified` and `stopped` booleans,
`error` the failed send's message or null); an empty list when none is blocked.

`--dry-run` runs no notifier, stops no session, and writes no record; its lines say
`would notify <id>` and `would stop <id>` instead, and the JSON booleans say what would
have happened.

### Docs

- `skills/shipmill-setup/SKILL.md`, The gate: the three keys in the config block, and in
  Hand over that a waiting session sends a notification every `remind_hours` and that
  `max_wait_hours` stops it
- `docs/design/agent-modes.md`: step 3 (WAITING notifies, and stops at `max_wait_hours`
  even while held), State (`waiting.json`), and What Claude Code provides (a blocked
  session shows no question, no time it blocked, and no issue)
- `docs/install.md`: the three keys in its `[agents]` block
- The references to D-11 in `src/shipmill/gate.py`, `skills/shipmill-setup/SKILL.md`,
  `skills/github-ship-watch/SKILL.md`, and `docs/design/agent-modes.md` name D-13, which
  supersedes it

## Acceptance criteria

- S-003-1: `AgentsConfig` reads `notify` as true, `remind_hours` as 4, and `max_wait_hours` as 0 when the `[agents]` section omits them, and the values given when present
- S-003-2: the config is refused with exit 2 naming the key when `notify` isn't a boolean, `remind_hours` isn't an integer in 1..168, `max_wait_hours` isn't an integer in 0..168, or `[agents]` holds a key outside `prompt`, `prs`, `retry_hours`, `notify`, `remind_hours`, and `max_wait_hours`
- S-003-3: on a tick where no gate session of the repo is blocked, `gate()` reads no config, runs no notifier, stops no session, and leaves no entry in `waiting.json`; its decision is the one it makes today
- S-003-4: the first tick that sees a session blocked, with `notify = true`, records it in `waiting.json` with `since` set to that tick, sends one notification titled `shipmill <owner/repo>` whose body names the session id and `claude attach <id>`, and still returns WAITING with exit 0
- S-003-5: a later tick while the same session stays blocked sends no notification until `remind_hours` have passed since the last one sent, then sends one and updates `notified`
- S-003-6: a notification that fails (missing command, non-zero exit, or longer than 30 seconds) prints `notify failed for <id>: <error>`, leaves `notified` unchanged so the next tick sends again, and changes neither the decision nor the exit code
- S-003-7: with `notify = false`, no notifier command runs on any tick, and the wait is still recorded
- S-003-8: on macOS the notifier runs `osascript` with the title and body as arguments after an `on run argv` script, on another platform with `notify-send` on `PATH` it runs `notify-send <title> <body>`, and with neither it runs no command and the send fails with `no notifier (osascript or notify-send)`
- S-003-9: an entry in `waiting.json` whose session is no longer blocked is dropped on the next tick, so a session that blocks again gets a new `since` and a new first notification; a malformed `waiting.json` exits 2 naming its path
- S-003-10: with `max_wait_hours > 0`, the first tick on which a session has been blocked for at least `max_wait_hours` since its recorded `since` runs `claude stop <id>`, drops its entry, prints `stopped <id> after <N>h waiting: <name>`, sends the stopped notification when `notify` is true, writes nothing to GitHub, and decides the rest of the tick without that session (launching under the usual rules, UNCHANGED included)
- S-003-11: with `max_wait_hours = 0`, the gate never stops a waiting session, however long it waits
- S-003-12: while an open `shipmill-hold` issue makes the tick HELD, the gate still notifies for a blocked session and still stops one that reached `max_wait_hours`, and the HELD reason no longer names a session it stopped
- S-003-13: when `claude stop` fails, `shipmill gate` exits 2, launches no session, and keeps the session's entry in `waiting.json`
- S-003-14: `shipmill gate --dry-run` runs no notifier, stops no session, writes no `waiting.json`, and prints `would notify <id>` or `would stop <id>` where a real tick would act
- S-003-15: `shipmill gate --json` lists each blocked session under `waiting` with its session id, name, `since`, whole hours waited, whether it was notified, whether it was stopped, and the failed send's error or null, and an empty `waiting` list when none is blocked
- S-003-16: `skills/shipmill-setup/SKILL.md`'s gate section, `docs/design/agent-modes.md`, and `docs/install.md` document `notify`, `remind_hours`, and `max_wait_hours`, and agent-modes.md documents `waiting.json` and that a blocked session shows no question, no time it blocked, and no issue

## Out of scope

- Quoting the session's question or commenting on an issue or pull request: Claude Code doesn't expose the question or the item a session works on; a later spec can add it if `claude agents` starts to
- Mode 2 (noninteractive, `needs-decision` on GitHub; `docs/design/agent-modes.md`), which makes waiting unnecessary rather than visible
- Notifying for a session that is still working, or for sessions the gate didn't start
- `terminal-notifier`, an action button, sounds, or other channels (email, Slack, a GitHub issue)
- A systemd unit or installer for Linux: the gate ships launchd only; on Linux the notifier uses `notify-send` where it exists, wherever the user runs the gate from
- Clearing the launch record when a session is stopped, so stopped work relaunches before `retry_hours`: it would relaunch into the same question

## Decisions relied on

- D-4
- D-13

## Issues

- shipmill/shipmill#128: S-003-1, S-003-2
- shipmill/shipmill#129: S-003-3, S-003-4, S-003-5, S-003-6, S-003-7, S-003-8, S-003-9, S-003-14, S-003-15
- shipmill/shipmill#130: S-003-10, S-003-11, S-003-12, S-003-13, S-003-16

## Verification
