### Added

- Watch and setup: github-ship-watch lists a person's item waiting on its assignees in ISSUES_OPEN as `WITH_PERSON #N (@login ...)` (triage_state.py's `--json` line for WITH_PERSON now carries an `assignees` list), and shipmill-setup's `setup_state.py` wants the `human` label wherever it wants `needs-decision` and `--fix` creates both (#344)
