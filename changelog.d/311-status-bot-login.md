### Fixed

- `shipmill status --rows` and `--json`, and the summary after a failed App check, read with the App's `--bot-login` when the config sets `[agents] app_id` and its key is on this host, so an App question the maintainer answered no longer reads NEEDS_DECISION (#311)
