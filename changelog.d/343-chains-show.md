### Added

- Triage: `chains.py show <owner/repo#N>` prints the chain an issue belongs to, every issue it waits on and every issue that waits on it (natively or as its parent), recursively and each once across repos, with its executor (`agent`, or `person: @login` for a `human` item) and its state (CLOSED, BLOCKED with the open holds, READY); a READY item in a repo whose `.github/shipmill.toml` has no `[agents]` table reads NO_GATE and the command exits 1, and github-ship-watch reports each such item in a chain of the repo's open issues as a `CHAIN_NO_GATE` row, an action for a person (#343)
