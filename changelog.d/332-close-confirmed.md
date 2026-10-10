### Added

- Triage: a right close can end SUSPECT_CLOSE. `triage_state.py` reads each recently closed issue's last 5 comments (paging back to the close only when all 5 came after it) and drops the SUSPECT_CLOSE row when a comment after the last close has `<!-- shipmill:close-confirmed -->` as its first line and comes from an OWNER, MEMBER, or COLLABORATOR or the `--bot-login` App; an outsider's marker never counts, and a reopen and a new close re-arm it. The skill's step 7 and `references/comments.md` give the comment (#332)
