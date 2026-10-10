### Fixed

- Triage: `triage_state.py` no longer reads SUSPECT_CLOSE for an issue closed by a commit that names it only mid-line when a same-repo merged pull request closes it, by GitHub's link or a closing keyword in its title or body; a commit with no such pull request, or one that only mentions the issue or never merged, still reads SUSPECT_CLOSE (#363)
