# S-015: Self-hosted runners and a schedule from the policy

status: approved

## Problem

On a private repository every job shipmill runs is billed against the owner's Actions
minutes, and the release workflows give a caller no way to spend fewer (#319, split from
#318):

- Every job in `.github/workflows/prepare.yml` and `.github/workflows/land.yml` has
  `runs-on: ubuntu-latest`, and neither workflow has an input to change it. A repo with a
  self-hosted runner can't put the settle wait, `prepare`, or `land` on it without copying
  both workflows, which loses the fixes shipped through `@v0`
- `shipmill init` always writes `cron: "7 * * * *"` into `release.yml`. Each hourly tick
  starts at least two jobs, each rounded up to a whole billed minute: about 1,400 minutes a
  month before anything is released, even for a policy whose lanes only release at one
  weekly window
- A runner shared with deploys takes one job at a time, so a 30-minute settle wait queues a
  deploy behind it; nothing tells the user before they pick that runner

## Behaviour

### A `runs-on` input on the release workflows

`.github/workflows/prepare.yml` and `.github/workflows/land.yml` each take a new
`workflow_call` input:

```yaml
runs-on:
  description: "The runner for every job: a label, or a JSON list of labels"
  type: string
  default: "ubuntu-latest"
```

Every job in both workflows (`settle`, `prepare`, `propose`; `land`, `close-proposal`,
`cleanup`, `upgrade`) sets

```yaml
runs-on: ${{ startsWith(inputs.runs-on, '[') && fromJSON(inputs.runs-on) || inputs.runs-on }}
```

so `self-hosted` picks a runner by one label and `'["self-hosted", "linux"]'` by all of
them. A caller that passes nothing keeps `ubuntu-latest` for every job, so existing
`release.yml` files run as before. The settle job's concurrency group and its steps don't
change (D-1).

### `shipmill init --runs-on`

`src/shipmill/init.py` and `src/shipmill/cli.py` (`CLI flags`): `init` takes
`--runs-on <value>`, a runner label or a JSON list of labels. With it, `caller_text` writes
`runs-on:` under `with:` in both the `prepare` and the `land` job of
`.github/workflows/release.yml`; a label is written bare (`runs-on: self-hosted`) and a
list as a single-quoted YAML string in compact JSON (`runs-on: '["self-hosted","linux"]'`),
since an unquoted list would reach the workflow as a YAML sequence and fail the `string`
input. Without the flag nothing is written and the default applies.

`init` exits 2 naming the value for an empty value, a label holding whitespace, a quote, or
a comma, a value starting with `[` that isn't JSON, and a JSON value that isn't a non-empty
list of non-empty strings. The repo's own CI workflow, which the caller's `ci` job calls,
picks its runner itself; `init` doesn't edit it.

### The schedule `init` writes

A new module, `src/shipmill/ticks.py`, works out from a `Policy` which scheduled ticks the
Release workflow needs, reading the triggers `_due` in `src/shipmill/planner.py` evaluates
(`quiet_minutes`, `schedule`, `milestone`) and the stable lane's `promote_from` from
`src/shipmill/policy.py`. The hotfix lane has no trigger and needs nothing. For the other
lanes, in this order:

1. **Hourly**, when any lane sets `milestone = true` (a milestone empties when an issue
   closes, which starts no Release run), or the stable lane sets both `promote_from` and
   `quiet_minutes` (an rc finishes its soak while main is quiet, so no push comes to plan
   it). The cron is today's: `cron: "7 * * * *"` with today's comment. It also covers every
   window
2. **One cron per window time**, when some lane has a `schedule` and none needs the hourly
   tick. For each window, `ticks.py` takes every start in the 366 days from the day `init`
   runs, as `Window.latest_start` in `src/shipmill/schedule.py` computes it (so a time zone
   with daylight saving time yields both of its UTC offsets), and groups the starts by UTC
   minute and hour into `M H * * D,D` lines, the days as cron weekdays (0 is Sunday) in
   ascending order, deduplicated across windows and sorted by hour, then minute.
   `Mon-Fri 07:00 Europe/Kyiv` gives `0 4 * * 1,2,3,4,5` and `0 5 * * 1,2,3,4,5`; the tick
   for the offset that isn't in force fires an hour before or after the window and plans
   like any run. The block's comment says the lines come from the policy's windows and how
   to rewrite them (`init --caller --force`)
3. **No `schedule:` key** at all, when no lane has a schedule and none needs the hourly tick
   (lanes on `quiet_minutes` alone, without a promoting stable lane, or lanes started by
   hand only). Such lanes are push-driven: each push plans once the settle wait ends

`push` and `workflow_dispatch` stay in every `release.yml`: a push run plans every lane, so
a window that opened while its lane was held, frozen, or had nothing pending still releases
on the next push. Without the hourly tick, such a lane releases on the next push or the
next window tick rather than within the hour; `docs/release-lanes.md` says so next to
`schedule`. The workflow_dispatch `lane=policy` run is unchanged (D-1). The operate layer
doesn't read the Release workflow's schedule: `init --operate` writes its own
`*/10 * * * *` caller, unchanged.

`init` derives the cron from the policy it writes. Its starting policy has
`milestone = true` on stable, so a plain `init` writes the hourly tick, and its
`release.yml` is byte for byte what it is today.

### `shipmill init --caller`

shipmill-setup edits the policy after `init` writes it, so the schedule must follow the
edited file. `init --caller` writes only `.github/workflows/release.yml`, from the policy
already in `.github/shipmill.toml`, with `--ci` and `--runs-on` as for a full `init`. It
refuses without `--force` when `release.yml` exists, exits 2 when there is no policy (or it
doesn't load, with the loader's error), and refuses `--operate` and `--no-fragments` with it.
Rewriting the file with `--force` replaces any hand edits, as `init --force` does today.

### `doctor` checks the schedule

`src/shipmill/doctor.py` reads the `cron:` lines of `release.yml` and adds a `schedule`
check: a WARN when the policy needs a tick the file lacks, naming each missing cron line and
the fix (`init --caller --force`), and a PASS otherwise. A cron of the form `M * * * *`
counts as the hourly tick and covers every need. Extra ticks are never warned about: an
hourly tick on a policy that needs less costs minutes, not releases, so existing setups see
no new warning (D-9).

### Docs

`docs/release-lanes.md` gains a "Self-hosted runners" section: the `runs-on` input and
`init --runs-on`, and what the runner needs: `bash`, `git`, and `curl` on the path; network
access to `github.com`, `api.github.com`, and the package index `uvx` installs shipmill's
dependencies from (`pypi.org`, `files.pythonhosted.org`); `uv` itself comes from the
`astral-sh/setup-uv` step. It says that a runner takes one job at a time, so a lane's
settle wait holds it for up to `quiet_minutes`. The schedule paragraph says how `init`
derives the cron and what the narrowed schedule changes for a held lane. `docs/install.md` names
the new flags where it names `init --no-fragments`.

### shipmill-setup

`skills/shipmill-setup/SKILL.md`, step 3: for a private repo (step 1 reads the visibility,
#318), the runner is one of the decisions asked with the lanes: GitHub-hosted (the default)
or a self-hosted runner named by its labels. When the user picks a self-hosted runner and a
lane uses `quiet_minutes`, the skill asks whether that runner also runs deploys, and if so
warns: the settle wait holds the runner for up to `quiet_minutes` after each push, and a
deploy started meanwhile queues behind it; give shipmill a runner of its own (another label
or instance), or drop `quiet_minutes` for a schedule. Step 4 then runs `init --caller
--force` with `--runs-on` after editing the policy, so `release.yml` carries the runner and
the schedule the edited policy needs.

`skills/shipmill-setup/scripts/setup_state.py` reports a `RUNNER_SHARED` row when
`release.yml` passes a `runs-on` other than `ubuntu-latest`, a lane sets `quiet_minutes`
above 0, and a workflow named by an environment's `workflow` or a lane's `dispatch` has a
job whose `runs-on` labels are the same set as shipmill's, or a subset or superset of it
(a runner with the larger set takes both jobs). The row names the workflow and the fix
above; it is a warning, not part of setup's done states.

### Alternatives

- **`runs-on` parsing.** Rejected: always JSON (`default: '"ubuntu-latest"'`), which makes
  every caller quote a plain label twice; and two inputs, `runs-on` and `runs-on-labels`,
  which needs a rule for when both are set. A runner label never starts with `[`, so one
  input that is a label unless it starts with `[` covers both forms. The object form
  (`{group: ..., labels: ...}`) for runner groups is left out until someone needs it
- **One input for every job**, not one per job (`settle-runs-on` and so on): a self-hosted
  runner that can run the wait can run the rest, and per-job inputs would triple the
  surface for a split nobody asked for
- **The cron.** Rejected: deriving a cron for every trigger, since a milestone empties and a
  soak ends at times no cron can name; a single UTC cron per window ignoring daylight saving
  time, which fires before the window for half the year and so releases nothing until the
  next window; and a `[release] cron` key in the config, which would repeat what the policy
  already says (D-4) and still have to be copied into the workflow. Kept: the hourly tick
  whenever any trigger needs polling, narrowed only when every scheduled lane is
  schedule-only, at the cost of a held lane waiting for the next push or window
- **Dropping `push` for a schedule-only policy** saves about two job-minutes per push but
  makes a window found held wait a whole cycle; rejected
- **Replacing the settle sleep with a check on the next tick (#319's third ask)** is out of
  scope, below

## Acceptance criteria

- S-015-1: `prepare.yml` and `land.yml` each declare a `runs-on` `workflow_call` input of
  type string defaulting to `ubuntu-latest`, every job in both sets `runs-on` to
  `${{ startsWith(inputs.runs-on, '[') && fromJSON(inputs.runs-on) || inputs.runs-on }}`,
  the settle job's concurrency group is unchanged, and actionlint accepts both workflows
- S-015-2: `shipmill init` on a repo with no `--runs-on` writes a `release.yml` byte for byte
  equal to the one written before this spec, with no `runs-on` line and `cron: "7 * * * *"`
- S-015-3: `shipmill init --runs-on self-hosted` writes `runs-on: self-hosted` under `with:`
  in both the `prepare` and the `land` job, and `--runs-on '["self-hosted", "linux"]'`
  writes `runs-on: '["self-hosted","linux"]'` there; actionlint accepts each written
  `release.yml`
- S-015-4: `shipmill init --runs-on` exits 2 naming the value for an empty value, a label
  with whitespace, a quote, or a comma, a value starting with `[` that isn't JSON, and a JSON
  value that isn't a non-empty list of non-empty strings, and writes no file
- S-015-5: for a policy with `milestone = true` on any non-hotfix lane, or a stable lane
  with both `promote_from` and `quiet_minutes`, the written `release.yml` has exactly one
  cron, `7 * * * *`
- S-015-6: for a policy whose lanes trigger on `schedule` (and `quiet_minutes` on a lane that
  doesn't promote) only, the written crons are one per UTC minute and hour of the windows'
  starts over the 366 days from the given date: `Mon-Fri 07:00 UTC` gives
  `0 7 * * 1,2,3,4,5` alone, `Mon-Fri 07:00 Europe/Kyiv` gives `0 4 * * 1,2,3,4,5` and
  `0 5 * * 1,2,3,4,5`, and `Mon 01:00 Asia/Tokyo` gives `0 16 * * 0`; two windows with the
  same UTC time give one line
- S-015-7: for a policy with no `schedule`, no `milestone`, and no promoting stable lane on
  `quiet_minutes`, the written `release.yml` has no `schedule:` key and still has `push` on
  the policy's branch and `workflow_dispatch` with its inputs
- S-015-8: `shipmill init --caller` writes only `release.yml`, from the existing
  `.github/shipmill.toml` and with `--runs-on`, refuses without `--force` when `release.yml`
  exists, exits 2 when there is no policy, and exits 2 when combined with `--operate` or
  `--no-fragments`
- S-015-9: `shipmill doctor` WARNs on a `schedule` check naming each cron line the policy
  needs that `release.yml` lacks and `init --caller --force` as the fix, and gives no WARN
  when the file has a `M * * * *` cron or every needed line, extra lines included
- S-015-10: `docs/release-lanes.md` documents the `runs-on` input and `init --runs-on`, names
  `bash`, `git`, `curl`, `github.com`, `api.github.com`, and `astral-sh/setup-uv` as what a
  self-hosted runner needs, says a runner takes one job at a time during the settle wait,
  and says a held lane under a narrowed schedule releases on the next push or window tick
- S-015-11: shipmill-setup's step 3 asks for the runner on a private repo and, for a
  self-hosted runner with a `quiet_minutes` lane, asks whether it also runs deploys and
  gives the warning and both fixes above; step 4 runs `init --caller --force` after the
  policy is edited
- S-015-12: `setup_state.py` reports `RUNNER_SHARED` naming the workflow when `release.yml`
  passes a non-default `runs-on`, a lane has `quiet_minutes` above 0, and an environment's or
  dispatch workflow has a job whose `runs-on` labels equal, contain, or are contained in
  shipmill's; it reports no such row with `quiet_minutes` unset or 0, with disjoint labels,
  or with no `runs-on` in `release.yml`, and runs under Python 3.10

## Out of scope

- Replacing the settle sleep with a check on the next scheduled tick (#319's third ask). It
  needs no change to D-1, since a scheduled run already evaluates `quiet_minutes` as
  `_due` does, but it doesn't remove the cost, it moves it: a 30-minute quiet wait checked
  hourly releases up to 90 minutes after the last push, and a tick fine enough to keep the
  latency (every 10 minutes) costs about 8,600 job-minutes a month, more than the sleep for
  any repo with fewer than about 290 pushes a month. A self-hosted runner from this spec
  makes the sleep free. A later spec can offer it as an opt-in for a policy that already
  needs the hourly tick
- A `runs-on` input on `.github/workflows/operate.yml`, whose 10-minute schedule costs more
  than the Release workflow's: the same pattern applies, in its own change
- Runner groups (the object form of `runs-on`)
- Skipping the settle job's runner on a schedule or dispatch run, where every step is
  already skipped (#318 handles the settle wait under `mode = "off"`)
- Editing the repo's own CI workflow to take a runner input

## Decisions relied on

- D-1: the settle job's concurrency group is unchanged, and a hand-started `lane=policy` run
  still evaluates triggers as a scheduled one does
- D-3: the docs, the generated `release.yml` comments, and the skill call the automation
  shipmill
- D-4: no new config key; the runner and the schedule are wiring in `release.yml`, and the
  schedule is derived from `.github/shipmill.toml`
- D-9: doctor warns about the schedule only when the policy needs a tick the file lacks

## Issues

- shipmill/shipmill#323: S-015-1, S-015-2, S-015-3, S-015-4
- shipmill/shipmill#324: S-015-5, S-015-6, S-015-7, S-015-8, S-015-9
- shipmill/shipmill#325: S-015-10, S-015-11, S-015-12

## Verification
