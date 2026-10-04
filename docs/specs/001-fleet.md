# S-001: Fleet watch report

status: approved

## Problem

A maintainer with several shipyard repos has to run ship-watch once per repo and read each report on its own, so an incident or a stalled release in one product hides among the others. romamo/shipyard#91 asks for one report across many repos: every repo's states together, what needs the maintainer first, and the fleet's metrics side by side.

## Behaviour

A fleet file lists the repos, in TOML:

```toml
[[repos]]
repo = "romamo/shipyard"

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

The build issues, filled in once they are filed: one `- owner/repo#N: S-NNN-1, S-NNN-2` per
line, naming the criteria that issue delivers. Each criterion belongs to exactly one.

## Verification

Filled in when the spec reaches `built`: one line per criterion id, saying how it was
checked against the default branch (beyond its tests) and the result.
