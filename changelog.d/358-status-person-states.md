### Fixed

- `shipmill status` knows spec 017's person's-item states: HANDOFF_DUE, VERIFY_DUE, and NO_ASSIGNEE are listed to triage, WITH_PERSON is parked with the logins it waits on (`(@login ...)`), and VERIFY_CLOSED is a closed item to check on its own `verify close` line, no longer counted among the open issues; CHAIN_NO_GATE and CHAIN_UNREADABLE wait on you on a `chain` line with their fix instead of under "other" (#358)
