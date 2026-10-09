### Added

- The reusable `prepare.yml` and `land.yml` take a `runs-on` input, a runner label or a JSON
  list of labels, that every one of their jobs runs on; it defaults to `ubuntu-latest`, so a
  caller that passes nothing runs as before. `shipmill init --runs-on <runner>` writes it
  into `release.yml`'s `prepare` and `land` jobs, a label bare and a list as compact JSON in
  single quotes, and exits 2 naming the value for one that isn't a runner label or a
  non-empty JSON list of labels, writing no file; without the flag `release.yml` is the one
  `init` wrote before (spec 015) (#323)
