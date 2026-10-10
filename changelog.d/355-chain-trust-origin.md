### Fixed

- `chains.py show --trusted-only`, the watch's chain walk, judges an issue's author by the repo the walk started from (D-16), not by the issue's own repo, where an outsider owns their own: an issue in another repo is followed only when its author is the starting repo's owner or collaborator (one `gh api repos/<repo>/collaborators/<login>` per login, cached) or the `--bot-login`, so an outsider's issue linked to a public one no longer makes every watch tick report CHAIN_UNREADABLE (#355)
