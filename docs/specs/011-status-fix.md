# S-011: every non-OK status row carries a fix

status: approved

## Problem

`shipmill status` (specs 008 and 009) names what is wrong but often not what to do about
it. The update hint from #233 is the model: a `SHIPMILL_OUTDATED` row prints the exact
command, scoped to the folder it must run in (D-23). Most other rows stop at the fact
(`to land #409`, `parked #26`, `WORKTREE_STALE ... 1 commit(s) not landed`, `BOT_FAILED
failure: <url>`), so a person has to work out the fix, and an agent reading `--json` has
nothing to run or propose. Nothing stops the next new check from shipping without a hint
either. Seen on romamo/treaty at 0.33.0; filed as #242.

## Behaviour

Two layers carry a fix: github-ship-watch's rows
(`skills/github-ship-watch/scripts/watch_state.py`), which `shipmill status --rows` and
`--json` print and the gate and `fleet.py` read, and the summary `shipmill status` prints
(`src/shipmill/status.py`). A fix is one line of text: an exact command where one exists,
otherwise the decision to make, naming what it is about. A fix never runs: `status`
writes nothing (spec 008), and the watch's repairs stay its "watch" scope.

### A fix works from any folder

Every command in a fix names its target, so it works wherever the reader runs it (D-23):
a `gh` command names the repo (`-R owner/name`, or the repo as its argument), a `git`
command names its checkout (`git -C <path>`), and a command that only works in one folder
says so (`in <folder>: ...`, as `SHIPMILL_OUTDATED` already does). A fix is built from
trusted values only: the repo, issue and pull request numbers, run ids, tags, paths,
config keys, and states. It never quotes an issue's or a pull request's title, body, or
comments (D-16).

### watch_state.py: the `fix` field

`Row` gains `fix`, a string or None. `--json` prints it on every line as `"fix"`, a string
or `null`, beside `state`, `subject`, `detail`, and `agent`, whose values are unchanged:
the field is additive and no row's detail changes. The table (`--rows`) prints a row with
a fix as today, followed by an indented line `fix: <fix>`, except when the detail already
ends with that fix (the rows that carry it in their detail today print it once).

`FIXED`, a new set in the script, names the states whose every row carries a fix: every
state in `ACTION`, plus `HOLD`, `WORKTREE_STALE`, and `UNTRUSTED`. `Row` refuses to be
built with a `FIXED` state and no fix (a programming error the tests catch). `PRS_OPEN`
carries a fix only when no gate lands pull requests (below); every other state's fix is
null.

| State | Fix |
|---|---|
| `BOT_FAILED`, `OPERATE_FAILED` | `gh run rerun <id> --failed -R <repo>`, the id from the row's run URL (ship-watch's rule to rerun only a flaky failure, once, stays the skill's) |
| `BOT_STALLED` | `gh workflow run <workflow> -R <repo> -f lane=<lane> -f dry-run=false` with the lane the plan's decision names; for a non-shipmill bot, `gh workflow run <workflow> -R <repo>` |
| `WORK_BRANCH_STALE` | `/shipmill:github-ship-watch <repo>`, whose repair checks that no run owns the branch before it deletes it; no bare delete command |
| `NOT_PUBLISHED` | `gh run rerun <id> --failed -R <repo>` when the newest finished run on the tag (`gh run list -R <repo> --branch <tag>`) failed; otherwise `find the publish run for <tag>: gh run list -R <repo> --branch <tag>` |
| `UNANNOUNCED` | `/shipmill:github-ship-watch <repo>`, which posts the notices with `shipped.py` |
| `ISSUES` | `/shipmill:github-issue-triage <repo>` |
| `UNHEALTHY` | the decision: operate rolls `<env>` back after `[operate] rollback_after` failures; check its health check if it persists |
| `PROMOTION_DUE` | the approve or run command the row's detail already names |
| `INCIDENT_OPEN` | land the hotfix pull request linked to it (`#N`), or `/shipmill:github-issue-resolve <repo>#<issue>` when none is linked |
| `POSTMORTEM_DUE` | `/shipmill:github-ship-watch <repo>`, which drafts the postmortem with its `Incident: <repo>#<issue>` line |
| `NEEDS_DECISION` | answer the needs-decision question on each item named |
| `BRANCH_DELETE_OFF`, `SHIPMILL_OUTDATED`, `GATE_NO_APP`, `SKILL_SHADOWED` | the fix the detail already carries, unchanged |
| `HOLD` | the decision: close `#N` when the factory may go on (`gh issue close <N> -R <repo>`) |
| `WORKTREE_STALE` | `git -C <path> log origin/<branch>..HEAD` to see the unlanded work, then push it and open a pull request, or `git worktree remove <path>` |
| `UNTRUSTED` | review them in an interactive session: `/shipmill:github-issue-triage <repo>` for issues, `/shipmill:github-pr-triage <repo>` for pull requests |
| `PRS_OPEN`, with no `[agents]` or `[agents] prs` not true | `set prs = true under [agents] in .github/shipmill.toml, or land them by hand: /shipmill:github-pr-triage <repo>` |

The other consumers of the rows:

- `src/shipmill/gate.py` keeps its fingerprint (state, subject, detail) and its session
  prompt (state and subject only, #114) as they are: `fix` enters neither, so adding it
  changes no gate decision
- `skills/github-ship-watch/scripts/fleet.py` accepts a row with the `fix` key (its
  `ROW_KEYS` check is exact today, so it would refuse every line); its own report is
  unchanged
- `skills/github-ship-watch/SKILL.md` says each row's `fix` is what to give the user, and
  its repair table stays the agent's procedure

### watch_state.py: a transient GitHub error

A `gh` read in `watch_state.py` (the script reads and never writes GitHub) that exits
non-zero with a server or network error on its stderr (`HTTP 5xx`, a timeout, a reset or
refused connection, an unexpected EOF) is retried once after a short pause. When the retry
fails too, the script exits 2 as today, and its error line ends `transient GitHub API
error: rerun`. Any other failure (auth, not found, a bad query) fails at once, as today.
`status`'s own `gh` reads (`gh pr list`, `gh repo view` in `src/shipmill/status.py`)
follow the same rule.

### status: the summary's fixes

Every reason the verdict has, STUCK or WAITS ON YOU, carries a fix: in `status.py` a
reason is a value with its text and a required fix, so a reason without one doesn't type
check, and a test builds every reason the report can give and fails on an empty fix.
WORKING and IDLE reasons need none.

The summary shows a fix as an indented line `fix: <fix>` under the line it clears, aligned
with the values. A group whose items share one fix (`needs decision`, say) shows it once,
after its last item; an item with its own fix shows it after that item. A row placed under
`other` shows its row's fix the same way.

Lines whose fix `status.py` builds itself:

- **landing**: when `PRS_OPEN` lists pull requests and no gate lands them, the landing line
  (and the `to land` reason) is followed by `fix: set prs = true under [agents] in
  .github/shipmill.toml, or land them by hand: /shipmill:github-pr-triage <repo>`. With
  `[agents] prs = true`, or no pull request waiting, there is no fix
- **gate**, by its problem: not loaded, `launchctl bootstrap gui/<uid> <plist>`; stale,
  `launchctl kickstart gui/<uid>/<label>`; a failed run or tick, `tail -n 50 <log>` to read
  the error; an `UNCHANGED` cooldown, the decision that the same findings came back after
  the session the line names, so read it (`claude --resume <id>`) or wait for the retry
  time; `WAITING`, `claude attach <id>`; no job on this Mac, `<cli> launchd <repo>` (as
  today)
- **github app**: a failed App check, `<cli> app-install <repo>` to check the App's
  install; no App, shipmill-setup's App step (`<cli> app-create`, then `app_id` in
  `[agents]`), as D-19 says
- **shipmill**: an outdated CLI's fix names the form actually installed (#238): `uv tool
  upgrade shipmill` when the CLI running is a `uv tool install` (`cli_command()` returns
  the bare `shipmill`), else `uv tool install shipmill`
- **repo**: `N behind` reads `N behind: git -C <checkout> pull --ff-only` in place of `(git
  pull)`, naming the checkout `--repo` points at

`<cli>` is `cli_command()`'s form, as the hints print it today.

### status: the item lines say more

These lines aren't reasons, but they gain what the person needs to act on them, from data
the report already reads or one more field of a read it makes:

- **to land**: each pull request GitHub reports `BEHIND` its base (`mergeStateStatus`, added
  to the `gh pr list` fields `status.py` reads) reads `#N <link> behind <branch>: gh pr
  update-branch N -R <repo>`; one reported `DIRTY` reads `#N <link> conflicts with
  <branch>: needs a rebase`. Any other state, `UNKNOWN` included (GitHub computes it
  lazily), adds nothing
- **in progress**: each issue names the open pull requests that cover it, from
  `triage_state.py --json`'s `note` (the `#M:open` entries): `#389 <link> → #409 <link>`
- **parked**: a `BLOCKED` issue names what it waits on from the same `note` (`waits on
  owner/repo#N (open)`) and that it unblocks when those close or merge; one only
  `labelled blocked` reads `labelled blocked: gh issue edit N -R <repo> --remove-label
  blocked once it can go on`. `POSTPONED` reads `postponed` and `TRIAGED` reads `triaged`
- **drafts**: each draft reads `#N <link>: gh pr ready N -R <repo> once it is ready`

### The gate's QUIET line

When the gate finds no work but `PRS_OPEN` lists pull requests it doesn't land (`[agents]
prs` not true), its decision line in `src/shipmill/gate.py` reads `QUIET: nothing needs an
agent; N pull request(s) wait to land, landing off ([agents] prs = false)`. It still
starts with `QUIET: `, which `status` and the log readers match. With no pull request
waiting it is unchanged.

## Acceptance criteria

- S-011-1: every line `watch_state.py --json` prints has a `fix` key, a string or `null`,
  and its `state`, `subject`, `detail`, and `agent` are the same as before for the same
  inputs
- S-011-2: `watch_state.py`'s `FIXED` holds every state in `ACTION` plus `HOLD`,
  `WORKTREE_STALE`, and `UNTRUSTED`; building a `Row` with a `FIXED` state and no fix
  raises; and a test fails when a state is in `ACTION` but not in `FIXED`
- S-011-3: for each state in `FIXED`, a test builds its row through the script's own row
  code and finds the fix the table under Behaviour names
- S-011-4: a `BOT_FAILED` or `OPERATE_FAILED` row's fix is `gh run rerun <id> --failed -R
  <repo>` with the id of the run its detail links
- S-011-5: a `NOT_PUBLISHED` row's fix is `gh run rerun <id> --failed -R <repo>` when the
  newest finished run on the tag failed, and the `gh run list -R <repo> --branch <tag>`
  decision otherwise
- S-011-6: a `WORKTREE_STALE` row's fix names `git -C <path> log origin/<branch>..HEAD` and
  `git worktree remove <path>` with the row's path
- S-011-7: a `PRS_OPEN` row's fix names `prs = true` under `[agents]` in
  `.github/shipmill.toml` and `/shipmill:github-pr-triage <repo>` when the config has no
  `[agents]` or `prs` isn't true, and is null with `prs = true`
- S-011-8: every `gh` command in a fix names the repo, every `git` command names `-C
  <path>`, and no fix contains an issue's or pull request's title (a test with titles
  holding a marker string finds it in no fix)
- S-011-9: the table (`--rows`) prints `fix: <fix>` on an indented line after a row with a
  fix, and doesn't when the row's detail already ends with that fix
- S-011-10: `fleet.py` reads `watch_state.py --json` lines with the `fix` key, and the
  gate's fingerprint and session prompt are the same with or without a `fix` on the rows
- S-011-11: a `gh` read in `watch_state.py` that fails with an `HTTP 5xx`, timeout, or
  connection error on stderr is run once more; a second failure exits 2 with an error line
  ending `transient GitHub API error: rerun`; an auth or not-found failure is not retried.
  `status.py`'s `gh pr list` and `gh repo view` reads do the same
- S-011-12: every STUCK and WAITS ON YOU reason `status.py` gives has a non-empty fix, a
  test that builds each kind of reason fails when one has none, and the summary shows each
  fix on an indented `fix:` line after the line it clears, once per group sharing it
- S-011-13: with pull requests in `PRS_OPEN` and no gate landing them, the landing line is
  followed by a `fix:` line naming `prs = true` under `[agents]` in `.github/shipmill.toml`
  and `/shipmill:github-pr-triage <repo>`; with `prs = true` no fix follows it
- S-011-14: each gate problem's fix is the one Behaviour names: `launchctl bootstrap` when
  not loaded, `launchctl kickstart` when stale, `tail -n 50 <log>` after a failed run or
  tick, `claude --resume <id>` in an `UNCHANGED` cooldown, `claude attach <id>` when
  `WAITING`, and `<cli> launchd <repo>` with no job on this Mac; a failed App check's fix
  is `<cli> app-install <repo>` and a missing App's names `app-create`
- S-011-15: an outdated CLI's fix is `uv tool upgrade shipmill` when `cli_command()` returns
  `shipmill`, and `uv tool install shipmill` when it returns the uvx form
- S-011-16: the repo line reads `N behind: git -C <checkout> pull --ff-only` with the
  checkout `--repo` names
- S-011-17: a pull request on the `to land` line that GitHub reports `BEHIND` reads `behind
  <branch>: gh pr update-branch N -R <repo>`, one reported `DIRTY` reads `conflicts with
  <branch>: needs a rebase`, and any other merge state adds nothing
- S-011-18: each `in progress` issue names its open pull requests from `triage_state.py`'s
  note as `→ #M <link>`, and each `parked` `BLOCKED` issue names what it waits on, or the
  `gh issue edit N -R <repo> --remove-label blocked` command when it is only labelled
  blocked
- S-011-19: each `drafts` item reads `gh pr ready N -R <repo> once it is ready`
- S-011-20: the gate's decision with no work and pull requests in `PRS_OPEN` it doesn't land
  reads `QUIET: nothing needs an agent; N pull request(s) wait to land, landing off
  ([agents] prs = false)`, and with none waiting reads `QUIET: nothing needs an agent` as
  before

## Out of scope

- A stable lane with `milestone = true` and no milestone for its next version: a new
  finding, not a fix for a row that exists. Telling it needs the version the planner would
  cut, which `watch_state.py` doesn't read; it goes to its own issue, for `shipmill plan`
  or `doctor` to report
- Unprefixed skills shadowed by a link (#236): main already reports them as
  `SKILL_SHADOWED` with their fix (#241); this spec only copies that fix into `fix`
- Why a pull request is a draft: GitHub records no reason, so the drafts line gives only
  the command that ends it
- A JSON form of `status`'s own reasons (the gate's job, the App check, the CLI's
  version): `--json` stays `watch_state.py`'s lines (S-009-14), now with `fix`; a summary
  in JSON is its own change
- Moving the fixes that five rows carry in their detail (`BRANCH_DELETE_OFF`,
  `SHIPMILL_OUTDATED`, `GATE_NO_APP`, `SKILL_SHADOWED`, `PROMOTION_DUE`) into `fix` alone: the details stay
  as they are, so nothing that reads them changes
- `fleet.py report` printing the fixes, and a retry in `triage_state.py` or other scripts:
  each is a change of its own
- Running a fix: `status` writes nothing (spec 008), and the watch's repairs stay under the
  user's words

## Decisions relied on

- D-3: prose calls the release automation shipmill
- D-4: the fixes name `.github/shipmill.toml` as the one config file
- D-24: a stale worktree is the person's to finish or remove; the fix says how, never does it
- D-14: `status` writes nothing, so it still runs as the user's `gh`
- D-15: a hold is the person's to lift; its fix is the decision and the close command
- D-16: a fix never quotes untrusted issue or pull request text, and untrusted items go to
  an interactive session
- D-19: a gate without an App is unfinished setup; its fix is the App step
- D-23: each fix works when run where it says, as the plugin update's already does

## Issues

- shipmill/shipmill#258: S-011-1, S-011-2, S-011-3, S-011-4, S-011-5, S-011-6, S-011-7, S-011-8, S-011-9
- shipmill/shipmill#259: S-011-12, S-011-13, S-011-14, S-011-15, S-011-16, S-011-17, S-011-18, S-011-19
- shipmill/shipmill#260: S-011-10, S-011-20
- shipmill/shipmill#261: S-011-11

## Verification
