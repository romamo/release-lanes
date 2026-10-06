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
shipmill [--repo PATH] status [owner/name] [--json]
```

A new subcommand in `src/shipmill/cli.py`. As with `gate`, the global `--repo` is the
checkout (default: the current directory) and the positional `owner/name` is the GitHub
repo. It runs the wheel's `skills/github-ship-watch/scripts/watch_state.py` for that repo
with `--repo-dir` set to the checkout's top level (`git rev-parse --show-toplevel`), so
running it from a subdirectory works. It prints the script's rows exactly as the script
prints them: the table by default, JSON lines with `--json`.

The command is built by `watch_command` in `src/shipmill/gate.py`, which the gate already
uses. That function gains a `json` parameter: the gate passes true, and `status` passes its
own `--json`. Today it always adds `--json`.

The report reads the checkout (tags, the policy, worktrees), so status needs one whether or
not a repo is named. Outside a git checkout it exits 2 saying to run it in a checkout of the
repo or to pass `--repo PATH`; this is checked first, before git is asked for anything else.
In a checkout with no repo named, status takes the `origin` through `_origin_repo` in
`src/shipmill/cli.py` (as `app-install` does); when `origin` isn't a GitHub repo it exits 2
saying to name the repo. A named repo must match the checkout's `origin`, checked by the
gate's `check_checkout` in `src/shipmill/gate.py`; otherwise it exits 2 naming both. No
third origin parser is written.

### What it reads and writes

Whatever `watch_state.py` reads, and nothing more:

- `gh` for the repo, its runs, issues, and pull requests, and `git fetch` against origin
- PyPI's JSON API for the published versions
- `uvx --from <tool> shipmill plan` and `shipmill worktrees`, with the script's default
  `--tool` (the `v0` tag), as the gate runs it
- `claude plugin list`, `launchctl`, and the bundled `triage_state.py` and `shipped.py`,
  where present

`status` never repairs, reruns, comments, or starts a session; it is ship-watch's "status"
scope, not its "watch" scope. Untrusted issue text is never printed beyond what
`watch_state.py` already prints.

### Exit codes

`status` passes through `watch_state.py`'s exit code, with one guard:

- 0: the script exited 0
- 1: the script exited 1 and printed at least one row. The script exits 1 when a row's state
  is in its `ACTION` set. That set includes `SHIPMILL_OUTDATED` and `BRANCH_DELETE_OFF`, so
  an outdated plugin on the host is enough. A CI job or a script can gate on this, as on
  `doctor`'s exit 1
- 2: bad input, no `gh`, the script exited with any other code, or the script exited 1 with
  no row printed. The script prints its rows only once they are all read, so an uncaught
  exception (a network error reaching PyPI, say) exits 1 with nothing on stdout. That is a
  failure, not a report. Its whole stderr is shown, since a traceback ends with the
  exception

status captures the script's stdout before printing it, since the guard needs to know
whether a row was printed; the rows reach stdout unchanged. The script's stderr is passed
to status's stderr on every exit, so its warnings are seen on 0 and 1 too.

### The gate shares the guard

The gate has the same gap: `watch` in `src/shipmill/gate.py` accepts exit 1 and parses an
empty stdout as no findings, so a crashed script reads as QUIET. One function in
`src/shipmill/gate.py` decides whether a `watch_state.py` run failed (any exit but 0 or 1,
or exit 1 with no row), and both `watch` and `status` call it. A failed run stops the gate's
tick with an error, as any other code but 0 or 1 does today; its message keeps the last 500
characters of stderr rather than the first, so the exception is in it.

### Docs

The README's setup names `shipmill status` as the command for "is anything stuck?", and
github-ship-watch's `SKILL.md` says that its "status" scope is what `shipmill status`
prints for a person without an agent.

The module docstring's command list and exit codes in `src/shipmill/cli.py` name `status`.

## Acceptance criteria

- S-008-1: `shipmill status` with no repo runs `watch_state.py` for the checkout's `origin`
  repo with `--repo-dir` set to the checkout's top level, also when run from a subdirectory
- S-008-2: `shipmill status` in a checkout whose `origin` isn't a GitHub repo, with no repo
  named, exits 2 saying to name the repo
- S-008-3: `shipmill status owner/name` exits 2 naming both repos when it differs from the
  checkout's `origin`
- S-008-4: `shipmill status` prints `watch_state.py`'s table unchanged, and `--json` prints
  its JSON lines unchanged
- S-008-5: `shipmill status` exits 1 when `watch_state.py` exits 1 having printed a row, and
  0 when it exits 0
- S-008-6: `shipmill status` exits 2 with the script's stderr when `watch_state.py` exits
  with any code but 0 or 1, or exits 1 with no row printed
- S-008-7: the gate and `status` build the `watch_state.py` command through
  `watch_command`, whose `json` parameter decides `--json`; the gate's command is unchanged
- S-008-8: the `src/shipmill/cli.py` docstring lists `status` and its exit code 1
- S-008-9: `shipmill status` outside a git checkout exits 2 saying to run it in a checkout or
  pass `--repo`, with or without a named repo
- S-008-10: `shipmill status` passes the script's stderr through on exits 0 and 1, and shows
  its whole stderr on a failure
- S-008-11: the gate's tick fails, rather than going QUIET, when `watch_state.py` exits 1
  with no row; its message ends with the end of the script's stderr
- S-008-12: the README and github-ship-watch's `SKILL.md` name `shipmill status`

## Out of scope

- Repairs: rerunning a job, starting a lane, posting shipped notices stay the skill's
  "watch" scope, under the user's words
- Several repos at once: `fleet.py report` covers a fleet; `status` reads one checkout
- `--trusted-only` and `--bot-login`: the gate's flags for unattended sessions (D-16); a
  person reading the report sees every row
- Folding `doctor`'s checks into the report: setup and pipeline state stay two commands
- Pinning `--tool` to the installed shipmill: a development build has no tag to pin to, and
  the gate runs the default too; one change can move both later
- Making `watch_state.py` itself exit 2 on unexpected errors: the shared guard covers
  `status` and the gate; `fleet.py` is its own change

## Decisions relied on

- D-3: prose calls the release automation shipmill
- D-4: the policy is read from `.github/shipmill.toml`, through `watch_state.py`
- D-12: `status` creates no worktree of its own; the plan's worktree stays the script's to
  remove
- D-14: `status` writes nothing, so it runs as the user's `gh` and needs no App
- D-16: unattended sessions work only on trusted authors' items; `status` starts none

## Issues

## Verification
