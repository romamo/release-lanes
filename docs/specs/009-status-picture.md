# S-009: shipmill status says whether the factory works or is stuck

status: draft

## Problem

`shipmill status` (spec 008) prints github-ship-watch's rows as they come: one state per
line, in the script's vocabulary (`ISSUES NEEDS_PR #205 #204`, `LOOP ... last exit 0`). A
maintainer reading it can't answer the questions they ran it for: is the factory working
or stuck, and what waits on me? On this repo on 2026-10-07 the report showed an issue
waiting on a decision, a gate session blocked on the user, and a gate that had last run
some minutes before, but it said none of this in words, gave no link to the waiting
items, didn't count the open issues and pull requests, and didn't say whether the local
`main` matched GitHub's, how the gate runs (interactive or headless, as the App or not),
or when it last ran.

## Behaviour

### The report

`shipmill status` (in `src/shipmill/cli.py`, the report built in `src/shipmill/status.py`)
prints a picture instead of the rows. It starts with a verdict line and its reasons, then
one block per area, each line a label and a value:

```
shipmill/shipmill: WAITS ON YOU
  - issue #206 waits on your decision
  - the gate: WAITING: session 3890ede7 waits on you: claude attach 3890ede7

repo
  main         local b2d940f, github 7f3739d: 1 behind (git pull)
  version      latest release v0.26.0 (12 h ago); local main at v0.25.0 +6 commits, github main at v0.26.0
  releases     release.yml ok; v0.26.0 v0.25.0 v0.24.0 no registry
issues       4 open
  wait on you  #206  https://github.com/shipmill/shipmill/issues?q=is%3Aopen%20label%3Aneeds-decision
  to triage    none
  to build     none
  in progress  #205 #204 #26
  parked       none
pull requests  4 open
  wait on you  none
  to triage    #210 #209 #208 #207
  drafts       none
gate
  job          launchd every 15 min, idle, last run 11 min ago, exit 0
  last         WAITING: session 3890ede7 waits on you: claude attach 3890ede7
  mode         interactive (not set, the default)
  app          shipmill-romamo (5209412), connected
  sessions     2: gate waiting/blocked (3890ede7); interactive busy (74686)
  runs         none active
shipmill     cli 0.26.0, plugin user 0.26.0, local 0.26.0; latest v0.26.0, up to date
```

An area with nothing to show prints `none`; a line that can't be read on this host (no
launchd off a Mac, `claude` not on PATH) says so instead of being left out.

### The verdict

The verdict is the worst that applies, and every reason that applies is listed under it:

- **STUCK**: the pipeline can't move without a repair. A release run failed or stalled
  (`BOT_FAILED`, `BOT_STALLED`, `WORK_BRANCH_STALE`), a release is missing from its
  registry (`NOT_PUBLISHED`), operate failed, an environment is unhealthy, or an incident
  is open. With `[agents]` in the config, on a Mac: the gate's launchd job is installed
  but not loaded; it isn't running and the later of its last run and the host's last wake
  (`sysctl -n kern.waketime`, since launchd skips the ticks a sleeping Mac misses) is more
  than twice its interval ago; its last exit code is a number other than 0 (`(never
  exited)` before the first run is not); the log's last line is a failed tick's; or its
  last decision is `UNCHANGED` (the same work found again, retried only at the time the
  line names). Or `[agents] app_id` is set and the App check fails
- **WAITS ON YOU**: nothing is stuck, but a person owes an answer or a step. An issue or a
  pull request waits on a decision (`NEEDS_DECISION`), the gate's last decision is
  `WAITING`, a hold is open (`HOLD`: a person stopped the factory on purpose, D-15, so
  it's theirs to lift, not a fault), a promotion waits on approval (`PROMOTION_DUE`), a
  postmortem is due, an item is `UNTRUSTED`, the plugin or the CLI is older than
  shipmill's latest release, the repo keeps merged branches (`BRANCH_DELETE_OFF`), the
  gate has no App, or `[agents]` is set and this Mac has no launchd job for the repo
  (another host, or a `/loop`, may run the gate; the report reads only this one)
- **WORKING**: nothing above, and there is work for an agent (a row github-ship-watch marks
  `agent`), a gate session runs (an `AGENT_SESSION` row whose detail starts with `gate`;
  the interactive sessions in the checkout, such as the one running `status`, don't
  count), the gate's last decision is `RUNNING` or `LAUNCH` (a headless session isn't in
  `claude agents`, D-17), or a workflow run is active
- **IDLE**: none of these

Any other row github-ship-watch prints that the report doesn't place is listed under an
`other` block as the script prints it, so a new state is never dropped.

### Each area

- **repo, main**: the local default branch's commit against GitHub's, after the fetch
  `watch_state.py` makes: `in sync`, `N behind (git pull)`, `N ahead`, `diverged (N ahead,
  M behind)`, or `no local <branch>`. The default branch is `gh repo view`'s
- **repo, version**: GitHub's latest release (`gh release view`) and its age, and the
  version each default branch is at, local and GitHub's: its newest version tag and the
  commits since (`git describe --tags --long`); `no release yet` without a release
- **repo, releases**: the release workflow's state and each checked tag's registry state,
  from the `BOT_*`, `PUBLISHED`, `NOT_PUBLISHED`, `NO_REGISTRY`, and `UNANNOUNCED` rows
- **issues**: the count of open issues and, by `triage_state.py --json`'s states: `wait on
  you` (`NEEDS_DECISION`) with a link to GitHub's list of open issues labelled
  `needs-decision`; `to triage` (`NEW`, `REVISIT`, `SPEC_REFUSED`, `UNFILLED`,
  `DONE_NOT_CLOSED`); `to build` (`NEEDS_PR`, `UNBLOCKED`); `in progress` (`IN_PROGRESS`);
  `parked` (`BLOCKED`, `POSTPONED`, `TRIAGED`); and `untrusted` and `suspect close` when
  there are any
- **pull requests**: the count of open pull requests (`gh pr list`) and: `wait on you`,
  the `NEEDS_DECISION` row's numbers that are pull requests, with a link to GitHub's list
  of open pull requests labelled `needs-decision`; `to triage`, the `PRS_OPEN` row's;
  `drafts`, the drafts
- **gate, job**: the launchd job for the repo (`src/shipmill/launchd.py`'s label): its
  interval, whether `launchctl print` finds it loaded and running, its last exit code, and
  the time since its last run, taken from its log file's modification time (a tick writes
  to the log every run)
- **gate, last**: the log's last decision line (`QUIET`, `LAUNCH`, `RUNNING`, `WAITING`,
  `UNCHANGED`, `HELD`), or the failure that follows it: a failed tick leaves
  `shipmill: <error>` (`cli.run`), `error: ...` (uvx), or a traceback as its last
  unindented line
- **gate, mode**: `[agents] mode`, or `interactive (not set, the default)` without the
  key; `no [agents]: no gate runs for this repo` without the table
- **gate, app**: with `[agents] app_id`, the gate's own App check (`app_check` in
  `src/shipmill/app.py`: key, JWT, installation on the repo, bot account) with the key
  the launchd job passes the gate (`--app-key` in its arguments), else the App's default
  key; it prints the App's slug and id and `connected`, or `not connected:` and the check's
  error. When that key isn't on this host, it prints `can't check here: no key at <path>`,
  which is no reason for a verdict. Without an app_id, `none: sessions write as your gh
  login`
- **gate, sessions** and **runs**: the `AGENT_SESSION` and `RUNS_ACTIVE` rows, counted and
  shortened
- **shipmill**: one line: the installed CLI's version (the package metadata), the plugin
  installs and shipmill's latest release from the `SHIPMILL_VERSION` row, and `up to date`
  or `update available:` with the commands the `SHIPMILL_OUTDATED` rows name (and `uv tool
  upgrade shipmill` when the CLI is older)

### Reads

Beyond `watch_state.py`, the report reads, all read-only: `triage_state.py <repo> --json`
(the bundled script), `gh pr list`, `gh repo view`, `gh release view`, the checkout's git
refs and tags, the launchd plist, `launchctl print`, `sysctl -n kern.waketime`, the gate's
log, and, with an App, the App check's GitHub reads. It still writes nothing (spec 008).

The App check runs first: when it passes, both `watch_state.py` and `triage_state.py` read
with `--bot-login <slug>[bot]`, as the gate does, so a needs-decision question the App's
bot asked counts as a question and the maintainer's reply as its answer. The report still
leaves out `--trusted-only`, so a person sees every item (spec 008).

### Flags and exit codes

- `--rows` prints `watch_state.py`'s table unchanged, as `shipmill status` did under spec
  008; this replaces S-008-4's default
- `--json` prints its JSON lines unchanged, as before
- The exit code is spec 008's, decided by `watch_state.py`'s run (S-008-5, S-008-6),
  whatever the verdict: a STUCK from this host's facts alone (the job, the log, the App)
  can exit 0, so a script gates on the exit code for the pipeline and reads the verdict
  for the host. The extra reads fail as any failed command does (exit 2 with its error),
  except the App check, whose failure is the report's finding

## Acceptance criteria

- S-009-1: `shipmill status` prints the verdict line `owner/name: <VERDICT>` first, and
  every reason that applies under it
- S-009-2: the verdict is STUCK when a row is `BOT_FAILED`, `BOT_STALLED`,
  `WORK_BRANCH_STALE`, `NOT_PUBLISHED`, `OPERATE_FAILED`, `UNHEALTHY`, or `INCIDENT_OPEN`
- S-009-3: the verdict is STUCK when the config has `[agents]` and, on a Mac, the launchd
  job isn't loaded, the later of its last run and the last wake is more than twice the
  interval ago while it isn't running, its last exit code is a number other than 0, or the
  log's last line is a failed tick's or an `UNCHANGED` decision; not before the first run,
  after a wake, or off a Mac; and no job on this Mac waits on you instead
- S-009-4: the verdict is STUCK when `[agents] app_id` is set and the App check fails, and
  the app line shows the check's error; a key missing on this host is no reason, and the
  line says so
- S-009-5: the verdict is WAITS ON YOU, not STUCK, when the only reasons are a decision
  waiting, a `WAITING` gate, `HOLD`, `PROMOTION_DUE`, `POSTMORTEM_DUE`, `UNTRUSTED`, an
  outdated plugin or CLI, `BRANCH_DELETE_OFF`, a gate without an App, or no gate job on
  this Mac
- S-009-6: the verdict is WORKING when nothing above applies and a row is marked `agent`, a
  gate session runs, the gate's last decision is `RUNNING` or `LAUNCH`, or a run is
  active; an interactive session alone isn't; IDLE otherwise
- S-009-7: the issues block counts the open issues and lists them as wait on you, to
  triage, to build, in progress, and parked by `triage_state.py`'s states, with the link to
  GitHub's open `needs-decision` issues on the wait on you line
- S-009-8: the pull requests block counts the open pull requests and lists wait on you
  (with its link), to triage, and drafts, telling a waiting pull request from a waiting
  issue
- S-009-9: the main line compares the local default branch with GitHub's: in sync, behind,
  ahead, diverged, or missing
- S-009-10: the version line names GitHub's latest release and its age, and the version
  tag and commits since for the local and GitHub default branches, or `no release yet`
- S-009-11: the gate block shows the job's interval, loaded state, last exit code, time
  since the log was last written, the last decision line, the mode (`not set` without the
  key), and the App line
- S-009-12: the shipmill line shows the CLI's and the plugin's versions against the latest
  release, and `update available:` with the commands when either is older
- S-009-13: a row the report doesn't place is printed under `other` as the script prints it
- S-009-14: `--rows` prints `watch_state.py`'s table unchanged, `--json` its JSON lines
  unchanged, and the exit code is spec 008's whatever the verdict
- S-009-15: with a passing App check, the report's `watch_state.py` and `triage_state.py`
  reads pass `--bot-login <slug>[bot]` and never `--trusted-only`; `--rows` and `--json`
  read as spec 008 does

## Out of scope

- Repairing anything the report names: that stays github-ship-watch's "watch" scope
- Changing `watch_state.py`'s rows or exit code: the gate and `fleet.py` read them
- Several repos at once (`fleet.py report`)
- A gate on another host than this one: the job, log, and sessions are this machine's
- Reading the headless gate's process record (`gate.json`): the gate's last decision says
  whether its session runs
- Naming D-19 and D-20 (a gate needs an App and an explicit mode, still in review): the
  report already shows both facts, and a later change can cite them once they land

## Decisions relied on

- D-3: prose calls the release automation shipmill
- D-4: `[agents]` is read from `.github/shipmill.toml`
- D-14: the App line runs the gate's App check, which writes nothing
- D-15: a hold stops the gate on purpose; the report counts it as the person's to lift
- D-16: `--trusted-only` stays the unattended gate's; the report shows every item
- D-17: a headless session never reads as waiting on the user, and isn't in `claude agents`

## Issues

## Verification
