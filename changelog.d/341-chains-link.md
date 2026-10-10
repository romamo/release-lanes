### Added

- Triage: `chains.py link <owner/repo#N> --blocked-by <owner/repo#M>` and `--child <owner/repo#C>` write a dependency both as GitHub's native relation (blocked by, or sub-issue) and as a `Depends on` line in the issue's body, adding only the half that is missing; when the forge refuses the relation it writes the line alone and says `text only:` (#341)
