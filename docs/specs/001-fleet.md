# S-001: Fleet watch report

status: built

## Problem

A maintainer with several shipmill repos has to run ship-watch once per repo and read each report on its own, so an incident or a stalled release in one product hides among the others. romamo/shipmill#91 asks for one report across many repos: every repo's states together, what needs the maintainer first, and the fleet's metrics side by side.

## Behaviour

A fleet file lists the repos, in TOML:

```toml
[[repos]]
repo = "romamo/shipmill"

[[repos]]
repo = "owner/other"
incident_label = "sev"   # optional; the label that repo's incidents carry (default "incident")
```

`skills/github-ship-watch/scripts/fleet.py report --fleet <file>` (stdlib, Python 3.10+, run with `uv run --no-project python` like the other ship-watch scripts) runs the ship-watch state check (`skills/github-ship-watch/scripts/watch_state.py`) for each repo and prints one table: a repo column, then each row's state, subject and detail as watch_state prints them. Rows that watch_state counts as action come first, across all repos, then the report-only rows. It exits 1 when any repo has an action row, else 0.

- `--metrics` adds the measures of `skills/github-ship-watch/scripts/metrics.py` for each repo, one column per repo, with "no data" kept as it is
- `--json` prints the same report as one JSON object: the repos, each with its rows (and its metrics when asked)
- A repo whose check fails (not found, no access, a gh error) gets one REPO_ERROR row naming the error's first line; the other repos are still reported, and the run exits 2 after printing everything
- The fleet file is refused with exit 2 and a message naming the file and the problem when it is missing, isn't TOML, has no `[[repos]]`, has an entry without `repo`, has a key other than `repo` and `incident_label`, names a repo not in `owner/name` form, or lists a repo twice. On Python 3.10, where `tomllib` doesn't exist, the file is read in the plain form above and anything else is refused with the same exit 2
- `skills/github-ship-watch/SKILL.md` gains a Fleet section: the fleet file, the report, and a routine prompt that runs it

## Acceptance criteria

- S-001-1: `fleet.py report --fleet F` prints every listed repo's watch rows with the repo named on each, action rows before report-only rows
- S-001-2: `fleet.py report` exits 1 when any repo has an action row and 0 when none has
- S-001-3: a fleet file that is missing, isn't TOML, has no repos, has an entry without `repo`, has an unknown key, or names a repo not in owner/name form is refused with exit 2 and a message naming the file and the problem
- S-001-4: a fleet file listing the same repo twice is refused with exit 2
- S-001-5: a repo whose check fails gives one REPO_ERROR row with the error's first line, the other repos are still reported, and the run exits 2
- S-001-6: `--metrics` reports each repo's metrics side by side, one column per repo, keeping "no data" as no data
- S-001-7: `--json` prints the report as one JSON object holding each repo's rows, and its metrics when `--metrics` is given
- S-001-8: a repo's `incident_label` from the fleet file is the label its watch uses for incidents
- S-001-9: the github-ship-watch skill documents the fleet file, the report, and a routine prompt that runs it

## Out of scope

- Repairs across repos: the fleet report only reads; each repo's own watch pass still repairs (rerun, start a stalled lane, post notices)
- A shared, org-level decisions log that every repo inherits (layer 11's second half): a later spec
- Cross-repo dependency updates (an upstream release opening PRs downstream): a later spec

## Decisions relied on

- D-6

## Issues

- romamo/shipmill#93: S-001-3, S-001-4, S-001-8
- romamo/shipmill#94: S-001-1, S-001-2, S-001-5, S-001-7
- romamo/shipmill#95: S-001-6, S-001-9

## Verification

- S-001-1: `fleet.py report --fleet F` on main plus the build PRs, F listing romamo/shipmill and the nonexistent romamo/no-such-repo-s001: every row named its repo, and the action rows (shipmill's ISSUES, the REPO_ERROR) came before BOT_OK, NO_REGISTRY, and PRS_OPEN; holds
- S-001-2: a fleet of romamo/shipmill alone exited 1 on its ISSUES action row (NEEDS_PR #93; NEW #49); exit 0 with no action row is checked in the tests only, as shipmill had action rows throughout; holds
- S-001-3: `fleet.py report` on a missing file, a broken `[[repos]` header, a file with no repos, an entry without `repo`, an entry with a `path` key, and `repo = "shipmill"`, on Python 3.14 and 3.10: each exited 2 with `error: <file>: <problem>` on stderr and nothing on stdout; holds
- S-001-4: a file listing romamo/shipmill and Romamo/Shipmill exited 2 with `lists Romamo/Shipmill twice` (owner and name compared ignoring case, as GitHub does); holds
- S-001-5: the nonexistent repo gave one row, `REPO_ERROR gh repo clone GraphQL: Could not resolve to a Repository with the name 'romamo/no-such-repo-s001'. (repository)`, while romamo/shipmill's six rows were still printed, and the run exited 2; holds
- S-001-6: `--metrics` on the same file printed shipmill's seven measures in its own column under a "Metrics: the 30 days to ..." line, and the failed repo had no column; "no data" cells checked in the tests only, as shipmill has data for every measure; holds
- S-001-7: `--json` printed one object with both repos and their rows (shipmill's six, the failed repo's REPO_ERROR) and no metrics; `--json --metrics` added shipmill's `metrics.py --json` object and `null` for the failed repo, the same on Python 3.10.0 (`uv run --python 3.10 --isolated --no-project python`); holds
- S-001-8: a fleet entry `repo = "romamo/shipmill"`, `incident_label = "bug"` made the watch read closed `bug` issues as incidents (POSTMORTEM_DUE rows for #83, #82, #80, ...), where the default label finds only #81, which has its postmortem; holds
- S-001-9: `skills/github-ship-watch/SKILL.md` has a Fleet section with the fleet file (its example parses as a fleet file), the report and its flags, and a `/schedule` routine prompt that runs `fleet.py report --fleet <file> --metrics`; holds
