# S-010: Publish shipmill to PyPI on each stable and hotfix release

status: approved

## Problem

shipmill installs only from git: `uvx --from git+https://github.com/shipmill/shipmill@v0
shipmill`. Each stable release cuts a GitHub release, moves `v0`, and deploys Pages, but
nothing publishes the package to PyPI (#216). So the command people must type is long, and
docs fall back to a bare `shipmill …` that isn't on `PATH` (#215); every `uvx` run of the
gate and the skills clones the repo instead of downloading a wheel; and the name `shipmill`
is unclaimed on PyPI (`https://pypi.org/pypi/shipmill/json` returns 404), so someone else
could take it.

## Behaviour

### Prerequisites (the maintainer, once, outside the repo)

- On pypi.org, a pending trusted publisher for `shipmill/shipmill`, workflow `publish.yml`,
  environment `pypi`. This also reserves the name
- In the repo's settings, a GitHub environment named `pypi`

No token secret is stored anywhere: the upload authenticates through OIDC.

### The workflow

A new `.github/workflows/publish.yml`, started only by `workflow_dispatch` with a required
`tag` input, the way `move-major-tag.yml` is: shipmill's land step dispatches it on the
default branch with the release's tag (`src/shipmill/land.py`), and `shipmill doctor`
already checks that a dispatched workflow takes a `tag` input.

One job, in the `pypi` environment, with `permissions: id-token: write` and `contents:
read` and nothing else. Its steps:

1. **Check the tag.** A tag that isn't `vX.Y.Z` fails the run with an `::error::` naming it,
   before anything is checked out
2. **Check out the tag**, not the branch the run was dispatched on
3. **Check the version.** The `version` in `pyproject.toml` at that tag must equal the tag
   without its `v`; otherwise the run fails naming both
4. **Build** with `uv build`, giving one wheel and one sdist in `dist/`
5. **Smoke-test the wheel** in a fresh environment with nothing else installed: `shipmill
   --help` exits 0, and the installed package holds the skills' state scripts the gate runs
   (`shipmill/skills/github-ship-watch/scripts/watch_state.py` and
   `shipmill/skills/github-issue-triage/scripts/triage_state.py`), proving the
   `force-include` in `pyproject.toml` reached the wheel
6. **Publish** with `uv publish` through trusted publishing, with `--check-url
   https://pypi.org/simple/` so a rerun for a version already on PyPI skips the files
   already there and succeeds

### Lane wiring

In `.github/shipmill.toml`, `publish.yml` joins `dispatch` in `[lanes.stable]` (after
`move-major-tag.yml`) and in `[lanes.hotfix]`. The dev and rc lanes don't publish.

### Failure

The GitHub release is created before the dispatch, so a failed publish leaves it in place;
nothing rolls it back. Once the first version is on PyPI, `github-ship-watch` reports a
later release that is missing there as NOT_PUBLISHED, as it does for any package on PyPI,
and a rerun of the workflow for that tag publishes it.

### Docs

`docs/install.md` says shipmill is on PyPI, and that it needs Python 3.14: `uvx` and `uv
tool install` fetch it when needed, while `pip install` on an older interpreter fails.
`CHANGELOG.md` gets an Added entry.

## Acceptance criteria

- S-010-1: `.github/workflows/publish.yml` runs only on `workflow_dispatch` with a required `tag` input, and its job runs in the `pypi` environment with `id-token: write` and `contents: read` as its only permissions, and reads no secret
- S-010-2: publish.yml fails, with an error naming the tag, for a tag that isn't `vX.Y.Z`, and fails naming both versions when `pyproject.toml` at the tag holds a different version
- S-010-3: publish.yml checks out the tag it was given, builds with `uv build`, and publishes with `uv publish` and `--check-url`, after a smoke-test step that runs `shipmill --help` from the built wheel and checks the wheel holds `watch_state.py` and `triage_state.py`
- S-010-4: `.github/shipmill.toml` dispatches `publish.yml` on the stable lane (after `move-major-tag.yml`) and on the hotfix lane, and on no other lane, and `shipmill doctor` passes on the repo
- S-010-5: `docs/install.md` says shipmill is published to PyPI on each stable and hotfix release and that it needs Python 3.14

## Out of scope

- Moving `$CR`, the README, `docs/install.md`, and the gate's launchd plists from
  `git+…@v0` to a PyPI range such as `uvx --from 'shipmill<1' shipmill`: a follow-up issue,
  filed once the first release is on PyPI, which also gives #215 its short form
- Publishing dev or rc builds, and TestPyPI
- Signing or attestations beyond what `uv publish` does by default
- The pypi.org and environment setup, which only the maintainer can do

## Decisions relied on

- D-3
- D-4

## Issues

## Verification
