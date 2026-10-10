### Changed

- A headless gate now asks the config-upgrade question: with `--trusted-only`, `triage_state.py` trusts an issue the release workflow opened as the Bot `github-actions[bot]` when it carries both the `shipmill-upgrade` label and the `<!-- shipmill-upgrade: <id> <version> -->` marker as its first line, so `watch_state.py` reports it as UPGRADE_PENDING instead of UNTRUSTED; any other `github-actions[bot]` issue stays UNTRUSTED (D-30) (#314)
