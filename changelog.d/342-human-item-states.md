### Added

- Triage: an issue labelled `human` is a person's item, triaged from the start and never dispatched; with every hold closed it reads HANDOFF_DUE until a trusted `<!-- shipmill:handoff -->` comment, WITH_PERSON until an assignee or trusted author replies `done`, then VERIFY_DUE, and NO_ASSIGNEE with no assignee; one closed as completed by a person with a `## Check` section reads VERIFY_CLOSED (never SUSPECT_CLOSE) until a trusted `<!-- shipmill:verified -->` comment, and `--wip` never counts a person's item (#342)
