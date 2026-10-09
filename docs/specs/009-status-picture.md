# S-009: shipmill status says whether the factory works or is stuck

status: built

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
prints a short summary instead of the rows: a heading with the verdict, then one line per
thing worth knowing, each a label and a value. Items are links, not descriptions; the link
carries the description (#212):

```
shipmill/shipmill (https://github.com/shipmill/shipmill): WAITS ON YOU
  repo           in sync at v0.28.0, release ok
  needs decision #206 https://github.com/shipmill/shipmill/issues/206
  parked         #26 https://github.com/shipmill/shipmill/issues/26
  open issues    2 https://github.com/shipmill/shipmill/issues
  pull requests  none
  gate           OK, launchd every 15 min
  mode           interactive
  landing        on
  github app     active
  shipmill       0.28.0, up to date
```

The heading is `owner/name (<the repo's GitHub URL>): <VERDICT>`. There is no separate
list of reasons: each reason the verdict has is a line of the summary, so a line that
holds something is either a fact a person asked for (repo, open issues, pull requests,
gate, mode, landing, github app, shipmill) or a reason. A line whose label has nothing to report
is dropped, except the always-shown lines named below.

### The verdict

The verdict is the worst that applies; each reason that applies shows as its line of the
summary (Each line):

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
  (another host, or a `/loop`, may run the gate; the report reads only this one). Or open
  pull requests (`PRS_OPEN`) wait to land and no gate lands them: with `[agents] prs =
  false` the reason reads `pull request #409 #408 wait to land: the gate doesn't land pull
  requests ([agents] prs = false); set [agents] prs = true, or land them by hand`, and
  without `[agents]`, `pull request #N wait to land: no gate lands pull requests (no
  [agents] in the config)` (#231: the gate logged QUIET while green pull requests waited)
- **WORKING**: nothing above, and there is work for an agent (a row github-ship-watch marks
  `agent`), a gate session runs (an `AGENT_SESSION` row whose detail starts with `gate`;
  the interactive sessions in the checkout, such as the one running `status`, don't
  count), the gate's last decision is `RUNNING` or `LAUNCH` (a headless session isn't in
  `claude agents`, D-17), a workflow run is active, or open pull requests (`PRS_OPEN`)
  wait for a gate with `[agents] prs = true` to land them (`pull request #N to land`)
- **IDLE**: none of these

### Each line

Always shown, in this order around the item lines:

- **repo** (first): the local default branch against GitHub's, after the fetch
  `watch_state.py` makes: `in sync`, `N behind (git pull)`, `N ahead`, `diverged (N ahead,
  M behind)`, or `no local <branch>`; then `at <version>`, the newest version tag on
  GitHub's default branch and `+N` for the commits since (`git describe --tags --long`),
  or `no release yet`; then `release ok`, or in its place the release problem: the
  `BOT_FAILED`, `BOT_STALLED`, `WORK_BRANCH_STALE`, `NOT_PUBLISHED`, or `UNANNOUNCED` state
  with its tag or a link to the failed run. A repo with no release workflow or policy
  (`BOT_NONE`) reads `no release workflow` in place of `release ok`, which is no reason
  for a verdict. The default branch is `gh repo view`'s
- **open issues**: the count of open issues and the link to GitHub's issue list
  (`https://github.com/<owner>/<name>/issues`), or `none`
- **pull requests**: the count of open pull requests (`gh pr list`) and the link to the
  list (`.../pulls`), or `none`
- **gate**: with `[agents]` in the config, `OK, launchd every N min` when the launchd job
  (`src/shipmill/launchd.py`'s label) is loaded, ran within twice its interval (or since
  the last wake), last exited 0, and the log's last line is no failed tick and no
  `UNCHANGED` decision; otherwise the problem in place of `OK`, with the schedule after
  it: `not loaded`, `stale: last run <age> ago`, `failed: exit <code>`, `failed: <the
  error line>` (a failed tick's `shipmill:`, `error:`, or traceback line), `cooldown: same
  findings, retry <when the line names>`, or `waiting on you: claude attach <id>` for a
  `WAITING` decision. `HELD` reads `held by #N <link>`. Off a Mac it reads `can't check
  here: no launchd`; on a Mac with no job for the repo, `no launchd job on this Mac`.
  Without `[agents]`, `none: no [agents] in the config`
- **mode**: `[agents] mode` as `interactive` or `headless`, or `interactive (not set)`
  without the key; dropped without `[agents]`
- **landing**: whether the gate lands pull requests: `on` with `[agents] prs = true`, `off:
  [agents] prs = false (the gate opens PRs but never lands them)` otherwise; dropped without
  `[agents]`
- **github app**: with `[agents] app_id`, the gate's own App check (`app_check` in
  `src/shipmill/app.py`, with the key the launchd job passes the gate, else the App's
  default key): `active` when it passes, or the problem in its place (`not connected:
  <the check's error>`, or `can't check here: no key at <path>`, which is no reason for a
  verdict). Without an app_id, `not active`
- **shipmill** (last): the installed CLI's version, then `up to date`, or `update
  available:` with the commands the `SHIPMILL_OUTDATED` rows name (and `uv tool upgrade
  shipmill` when the CLI is older). The plugin's version shows only when it differs from
  the CLI's

Shown only when they hold something, between repo and open issues, one item per line with
its direct link (`https://github.com/<owner>/<name>/issues/<n>` or `.../pull/<n>`), the
label on the first line of each group:

- **needs decision**: the issues and pull requests in the `NEEDS_DECISION` row, each with
  its own link, never a label search
- **hold** (`HOLD`), **incident** (`INCIDENT_OPEN`), **postmortem** (`POSTMORTEM_DUE`),
  **promotion** (`PROMOTION_DUE`, with the run's link), **operate** (`OPERATE_FAILED`,
  `UNHEALTHY`), **untrusted** (`UNTRUSTED`), **suspect close**, and **branches**
  (`BRANCH_DELETE_OFF`: `merged branches kept`)
- **to triage**, **to build**, **in progress**, and **parked**, by `triage_state.py
  --json`'s states as before: to triage (`NEW`, `REVISIT`, `SPEC_REFUSED`, `UNFILLED`,
  `DONE_NOT_CLOSED`), to build (`NEEDS_PR`, `UNBLOCKED`), in progress (`IN_PROGRESS`),
  parked (`BLOCKED`, `POSTPONED`, `TRIAGED`); **to land**, after in progress, the open
  pull requests in the `PRS_OPEN` row (whose detail is only `#N` tokens; any other detail
  fails the report), newest first; and **drafts**, the draft pull requests
- **sessions** and **runs**: the gate's sessions (`AGENT_SESSION` rows whose detail starts
  with `gate`) and active runs (`RUNS_ACTIVE`), counted, with the run links
- **other**: any row github-ship-watch prints that the summary doesn't place, as the
  script prints it, so a new state is never dropped

### Reads

Beyond `watch_state.py`, the report reads, all read-only: `triage_state.py <repo> --json`
(the bundled script), `gh pr list`, `gh repo view`, the checkout's git refs and tags, the
launchd plist, `launchctl print`, `sysctl -n kern.waketime`, the gate's log, and, with an
App, the App check's GitHub reads. It still writes nothing (spec 008).

The App check runs first: when it passes, both `watch_state.py` and `triage_state.py` read
with `--bot-login <slug>[bot]`, as the gate does, so a needs-decision question the App's
bot asked counts as a question and the maintainer's reply as its answer. The report still
leaves out `--trusted-only`, so a person sees every item (spec 008).

Without a passing check (`--rows` and `--json`, which skip it, or a failed check), the reads
still pass `--bot-login <slug>[bot]` when the config sets `[agents] app_id` and the key the
check would use is on this host (#311): the App's JWT and `GET /app` alone give the slug,
one GitHub call. A key missing on this host, or a refused read, leaves the login out, as
before; for `--rows` and `--json` a refused read prints a warning to stderr, and the picture
shows the check's own failure. Without the key the login can't be read: the App's id alone
names no slug.

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

- S-009-1: dropped in #212, see S-009-16
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
- S-009-7: dropped in #212, see S-009-18, S-009-19
- S-009-8: dropped in #212, see S-009-18, S-009-19
- S-009-9: dropped in #212, see S-009-17
- S-009-10: dropped in #212, see S-009-17
- S-009-11: dropped in #212, see S-009-20, S-009-21
- S-009-12: dropped in #212, see S-009-22
- S-009-13: a row the report doesn't place is printed under `other` as the script prints it
- S-009-14: `--rows` prints `watch_state.py`'s table unchanged, `--json` its JSON lines
  unchanged, and the exit code is spec 008's whatever the verdict
- S-009-15: with a passing App check, the report's `watch_state.py` and `triage_state.py`
  reads pass `--bot-login <slug>[bot]` and never `--trusted-only`; `--rows` and `--json`
  read as spec 008 does (amended in #311, see S-009-27)
- S-009-16: the report's first line is `owner/name (https://github.com/owner/name):
  <VERDICT>`, with no list of reasons under it; each reason the verdict has is a line of
  the summary
- S-009-17: the repo line reads `<sync state> at <version>, release ok`: in sync, behind,
  ahead, diverged, or missing; the newest version tag on GitHub's default branch with `+N`
  commits since, or `no release yet`; and the release problem in place of `release ok`
  when a release row is `BOT_FAILED`, `BOT_STALLED`, `WORK_BRANCH_STALE`, `NOT_PUBLISHED`,
  or `UNANNOUNCED`
- S-009-18: each issue or pull request waiting on a decision is on a `needs decision` line
  with its own direct link (`.../issues/N` or `.../pull/N`), never a label search URL; the
  same holds for every item line (to triage, to build, in progress, parked, drafts, hold,
  incident, postmortem, untrusted, suspect close)
- S-009-19: `open issues` and `pull requests` show the count and the link to the repo's
  issue or pull request list, or `none` without a link
- S-009-20: the gate line reads `OK, launchd every N min` when the job is healthy, and the
  problem in place of `OK` when it is not loaded, stale, last exited non-zero, failed its
  last tick, or sits in an `UNCHANGED` cooldown, or when its last decision is `WAITING`;
  `none: no [agents] in the config` without `[agents]`
- S-009-21: the mode line shows `interactive`, `headless`, or `interactive (not set)`, and
  the github app line shows `active`, `not active` without an app_id, or the App check's
  problem
- S-009-22: the shipmill line shows the CLI's version and `up to date`, or `update
  available:` with the commands; the plugin's version shows only when it differs
- S-009-23: a label with nothing to report (needs decision, to triage, to build, in
  progress, parked, drafts, sessions, runs, other, and the reason lines) is left out;
  repo, open issues, pull requests, gate, and shipmill are always shown, mode and github
  app whenever the config has `[agents]`

- S-009-24: each open pull request in the `PRS_OPEN` row is on a `to land` line after `in
  progress`, newest first, with its own `.../pull/N` link, never under `other`; the line is
  dropped without one, and a `PRS_OPEN` detail that isn't `#N` tokens fails the report
- S-009-25: with `[agents]`, a `landing` line after `mode` reads `on` with `prs = true` and
  `off: [agents] prs = false (the gate opens PRs but never lands them)` otherwise; it is
  dropped without `[agents]`
- S-009-26: open pull requests in `PRS_OPEN` are a WAITS ON YOU reason naming them, the
  setting, and the fix when `[agents] prs = false` or the config has no `[agents]`, and a
  WORKING reason (`pull request #N to land`), never WAITS, with `prs = true`
- S-009-27: with `[agents] app_id` set and its key on this host, `--rows`, `--json`, and a
  report whose App check failed read with `--bot-login <slug>[bot]`, the slug from one `GET
  /app`, so the App's question followed by an owner's reply reads DECIDED, not
  NEEDS_DECISION; without an app_id or the key they read as before with no GitHub call, and
  a refused read leaves the login out with a warning on stderr for `--rows` and `--json`

## Out of scope

- Repairing anything the report names: that stays github-ship-watch's "watch" scope
- Changing `watch_state.py`'s rows or exit code: the gate and `fleet.py` read them
- Several repos at once (`fleet.py report`)
- A gate on another host than this one: the job, log, and sessions are this machine's
- Reading the headless gate's process record (`gate.json`): the gate's last decision says
  whether its session runs

## Decisions relied on

- D-3: prose calls the release automation shipmill
- D-4: `[agents]` is read from `.github/shipmill.toml`
- D-14: the App line runs the gate's App check, which writes nothing
- D-15: a hold stops the gate on purpose; the report counts it as the person's to lift
- D-16: `--trusted-only` stays the unattended gate's; the report shows every item
- D-17: a headless session never reads as waiting on the user, and isn't in `claude agents`
- D-19: a gate without an App is unfinished setup: a reason the report waits on you, and
  `GATE_NO_APP` is placed on the app line
- D-20: `[agents] mode` is written explicitly; the mode line says when it isn't

## Issues

- shipmill/shipmill#211: S-009-1, S-009-2, S-009-3, S-009-4, S-009-5, S-009-6, S-009-7, S-009-8, S-009-9, S-009-10, S-009-11, S-009-12, S-009-13, S-009-14, S-009-15
- shipmill/shipmill#212: S-009-16, S-009-17, S-009-18, S-009-19, S-009-20, S-009-21, S-009-22, S-009-23
- shipmill/shipmill#231: S-009-24, S-009-25, S-009-26
- shipmill/shipmill#311: S-009-27

## Verification

Checked on main plus #212's PR with `tests/test_status_picture.py`, whose facts are built
in the test (rows, issues, pull requests, the job, the App), and the CLI tests run against
fakes of `watch_state.py`, `triage_state.py`, and `gh`. `shipmill status` was also run for
real on this repo on 2026-10-07: WORKING, with the summary's lines as Each line names them.

- S-009-1: dropped in #212
- S-009-2: `test_s009_2_a_broken_pipeline_row_is_stuck`, passing
- S-009-3: `test_s009_3_a_gate_that_isnt_ticking_is_stuck`, `test_s009_3_a_gate_after_sleep_or_before_its_first_run_isnt_stuck`, `test_s009_3_no_job_on_this_host_waits_on_you_rather_than_stuck`, `test_s009_3_the_log_failure_after_a_decision_is_its_last_line`, and `test_s009_3_a_traceback_shows_its_error_and_a_named_exit_code_is_still_a_number`, passing
- S-009-4: `test_s009_4_a_failed_app_check_is_stuck_and_shown` and `test_s009_4_an_app_key_missing_on_this_host_is_not_a_failure`, passing
- S-009-5: `test_s009_5_what_a_person_owes_waits_on_you`, passing
- S-009-6: `test_s009_6_agent_work_or_a_gate_session_or_a_run_is_working`, passing
- S-009-7: dropped in #212
- S-009-8: dropped in #212
- S-009-9: dropped in #212
- S-009-10: dropped in #212
- S-009-11: dropped in #212
- S-009-12: dropped in #212
- S-009-13: `test_s009_13_a_row_the_report_doesnt_place_is_printed_under_other` and `test_s009_13_a_row_the_sessions_line_shows_is_not_under_other_too`, passing
- S-009-14: `test_s009_14_rows_json_and_the_exit_code_stay_spec_008s`, passing for exit 0 and 1
- S-009-15: `test_s009_15_with_the_app_the_reads_pass_its_bot_login`, passing
- S-009-16: `test_s009_16_the_heading_is_the_repo_its_link_and_the_verdict_with_no_reasons_list`, `test_s009_16_the_whole_summary_reads_as_the_spec_shows_it`, `test_s009_16_each_reason_is_a_line_of_the_summary`, and `test_s009_16_the_gate_reasons_are_lines_too`, passing
- S-009-17: `test_s009_17_the_repo_line_compares_local_with_github`, `test_s009_17_the_repo_line_names_the_version_and_the_release_problem`, and `test_s009_17_a_repo_without_a_release_workflow_says_so_and_is_no_reason`, passing
- S-009-18: `test_s009_18_each_item_has_its_own_link_never_a_search` and `test_s009_18_an_untrusted_pull_request_links_to_the_pull`, passing
- S-009-19: `test_s009_19_open_issues_and_pull_requests_are_counted_with_the_list_link`, passing
- S-009-20: `test_s009_20_the_gate_line_reads_ok_or_the_problem`, passing
- S-009-21: `test_s009_21_the_mode_and_github_app_lines`, passing
- S-009-22: `test_s009_22_the_shipmill_line_shows_the_version_and_the_updates`, passing
- S-009-23: `test_s009_23_empty_lines_are_dropped_and_the_fixed_ones_always_shown` and `test_s009_23_gate_sessions_and_runs_are_counted_with_their_links`, passing
- S-009-24: `test_s009_24_open_pull_requests_are_listed_to_land_newest_first` and `test_s009_24_an_unreadable_pull_request_row_fails`, passing
- S-009-25: `test_s009_25_the_landing_line_says_whether_the_gate_lands_pull_requests`, passing
- S-009-26: `test_s009_26_pull_requests_no_gate_lands_wait_on_you` and `test_s009_26_pull_requests_the_gate_lands_are_work_not_waiting`, passing
- S-009-27: `test_s009_27_rows_and_json_read_the_apps_question_as_answered`, `test_s009_27_without_an_app_id_or_its_key_rows_and_json_read_as_before`, `test_s009_27_a_refused_login_read_warns_and_reads_as_before`, and `test_s009_27_a_failed_app_check_still_reads_with_the_bot_login`, passing
