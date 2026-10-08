### Added

- Changelog fragments are on by default: `shipmill init` sets `[changelog] fragments =
  "changelog.d"` and writes `changelog.d/README.md`, and `shipmill init --no-fragments`
  writes neither; in a repo with fragments, `changelog_guard.py check` fails a pull request
  that adds a line under Unreleased, naming the fragment to write, or a fragment that
  doesn't read; the skills add a fragment instead of editing `CHANGELOG.md`, and shipmill's
  own pull requests do too (spec 013) (#274)
