### Added

- Self-hosted runner docs: `docs/release-lanes.md` gains "Self-hosted runners" (the `runs-on` input, `init --runs-on`, what the runner needs, and the settle wait holding it) and "The schedule in `release.yml`"; shipmill-setup asks for the runner on a private repo, warns when a self-hosted runner also runs deploys, and rewrites `release.yml` with `init --caller --force` after editing the policy; `setup_state.py` warns with `RUNNER_SHARED` when a deploy or dispatch workflow runs on shipmill's runner labels while a lane sets `quiet_minutes` (#325)
