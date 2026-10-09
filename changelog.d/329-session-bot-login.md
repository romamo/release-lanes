### Fixed

- With `[agents] app_id` set, a gate session's prompt names the App's `<slug>[bot]` login and
  tells the session to pass `--bot-login '<slug>[bot]'` to `triage_state.py` and
  `watch_state.py`, and github-issue-triage's steps 1 and 7 say so too, so a session's own
  reads no longer show an App question the maintainer answered as NEEDS_DECISION, or, with
  `--trusted-only`, an issue the App opened as UNTRUSTED; a repo without an App gets the same
  prompt as before (#329)
