### Fixed

- A headless gate session no longer stops on a chained Bash call whose commands are all on
  the allowlist (`cd <dir> && git push`): the skills and the implementer and reviewer briefs
  run one allowlisted command per Bash call (`git -C <dir>`, no `;` or `&&` chains or pipes
  into unlisted tools) and rerun a denied chain as separate calls; a command that isn't on
  the list is still a needs-decision (#312)
