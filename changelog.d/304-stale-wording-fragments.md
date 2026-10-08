### Fixed

- github-pr-triage no longer tells a reviewer to reword an Unreleased entry after a rebase in
  a repo with changelog fragments, which `changelog_guard.py check` fails: stale wording is
  fixed in the fragment that holds the entry, and an entry still under Unreleased stays or
  moves into a fragment in the same PR (#304)
