# S-008: shipmill status, the pipeline report from the CLI

status: draft

## Problem

`shipmill doctor` says whether a repo is set up, and `shipmill plan` says whether a lane
releases now, but nothing in the CLI says what the pipeline owes: a failed release run, a
release missing from PyPI, fixed issues not told which version shipped them, an open hold.
That report exists only as github-ship-watch's `watch_state.py`, which the gate already runs
(`src/shipmill/gate.py`), and which a person reaches only through an agent session or by
finding the script inside the installed wheel. A maintainer without an agent at hand, or a
CI job, has no command for "is anything stuck?".

## Behaviour

### The command

```
shipmill status [owner/name] [--json]
```

A new subcommand in `src/shipmill/cli.py`. It runs the wheel's
`skills/github-ship-watch/scripts/watch_state.py` for the repo on the current checkout, the
way the gate does (`watch_command` in `src/shipmill/gate.py`, shared rather than copied),
and prints its rows: the table by default, JSON lines with `--json`, exactly as
`watch_state.py` prints them.

With no repo it takes the checkout's `origin` repo; outside a GitHub checkout and with no
repo it exits 2 saying to name one. A named repo must match the checkout's `origin`, since
the report reads the checkout (tags, the policy, worktrees); otherwise it exits 2 naming
both.

### What it reads and writes

The same as `watch_state.py`: `gh` and `git fetch` against origin, nothing else. `status`
never repairs, reruns, comments, or starts a session; it is ship-watch's "status" scope, not
its "watch" scope. Untrusted issue text is never printed beyond what `watch_state.py`
already prints.

### Exit codes

- 0: no row is an action state
- 1: some row is an action state (`watch_state.py`'s `ACTION` and `TRIAGE_ACTION`), as
  `doctor` exits 1 on a failure, so a CI job or a script can gate on it
- 2: bad input, no `gh`, or `watch_state.py` failed; its stderr is shown, cut to 500
  characters as the gate cuts it

The module docstring's command list and exit codes in `src/shipmill/cli.py` name `status`.

## Acceptance criteria

- S-008-1: `shipmill status` with no repo runs `watch_state.py` for the checkout's `origin`
  repo with `--repo-dir` set to the checkout root
- S-008-2: `shipmill status` outside a GitHub checkout and with no repo exits 2 saying to
  name one
- S-008-3: `shipmill status owner/name` exits 2 naming both repos when it differs from the
  checkout's `origin`
- S-008-4: `shipmill status` prints `watch_state.py`'s table unchanged, and `--json` prints
  its JSON lines unchanged
- S-008-5: `shipmill status` exits 1 when `watch_state.py` exits 1, and 0 when it exits 0
- S-008-6: `shipmill status` exits 2 with the script's stderr when `watch_state.py` exits
  with any other code
- S-008-7: the gate and `status` build the `watch_state.py` command through one function
- S-008-8: the `src/shipmill/cli.py` docstring lists `status` and its exit code 1

## Out of scope

- Repairs: rerunning a job, starting a lane, posting shipped notices stay the skill's
  "watch" scope, under the user's words
- Several repos at once: `fleet.py report` covers a fleet; `status` reads one checkout
- `--trusted-only` and `--bot-login`: the gate's flags for unattended sessions (D-16); a
  person reading the report sees every row
- Folding `doctor`'s checks into the report: setup and pipeline state stay two commands

## Decisions relied on

- D-3: prose calls the release automation shipmill
- D-4: the policy is read from `.github/shipmill.toml`, through `watch_state.py`
- D-16: unattended sessions work only on trusted authors' items; `status` starts none

## Issues

## Verification
