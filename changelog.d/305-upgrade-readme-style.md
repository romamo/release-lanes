### Fixed

- `shipmill upgrade --apply changelog-fragments` writes `changelog.d/README.md` with an
  example in the config's `[changelog] style`, as `shipmill init` does, instead of always
  the keep-a-changelog one; it refuses a style it doesn't know, and still leaves an existing
  `README.md` alone (#305)
