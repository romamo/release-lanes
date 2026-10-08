# S-014: Config upgrades

status: draft

## Problem

A shipmill release reaches every client through `@v0`, but a feature that needs an opt-in in
the repo's config does nothing there until a person edits `.github/shipmill.toml` by hand
and learns that the feature exists. Spec 013's changelog fragments are the first case: each
repo needs `[changelog] fragments = "changelog.d"` and a `changelog.d/README.md`. `[agents]
plugin_update` (D-22) is another: off by default, so the skills in a gate's checkout go
stale unless someone notices.

shipmill must not turn such a feature on by itself: it changes how everyone in the repo
works, and D-10 and D-22 already keep that decision with the maintainer (#277). What is
missing is a way to put the decision in front of the maintainer and make accepting it one
action: merging a pull request.

## Behaviour

### The catalogue

`src/shipmill/upgrades.py` holds the upgrades shipmill can propose. Each has:

- an id, such as `changelog-fragments`
- the shipmill version that added it
- a check that says whether the repo has decided it: the config holds the upgrade's key,
  whatever its value. Writing the key by hand, `plugin_update = false` included, is a
  decision and ends the proposal
- whether it applies to the repo (`plugin-update` only to a config with an `[agents]`
  section)
- the edit that turns it on: the config lines it adds, under their table, keeping the rest
  of the file byte for byte (comments included), and any file it creates
- a short text for people: what it changes, why, and how to turn it off

The first two entries:

- `changelog-fragments` (S-013): adds `fragments = "changelog.d"` to `[changelog]` and
  creates `changelog.d/README.md` as `shipmill init` writes it
- `plugin-update` (D-22): adds `plugin_update = true` to `[agents]`

### `[autonomy] upgrade`

A new key in the existing `[autonomy]` table (`src/shipmill/autonomy.py`), read like the
other stages:

- `observe`: only `shipmill doctor` and `shipmill status` list the pending upgrades
- `propose` (the default): an issue per pending upgrade, and a pull request that turns it on;
  a person merges it
- `act`: the same pull request, merged by github-pr-triage on green CI like any other

An open hold turns act into propose, as for every stage (D-8). A maintainer who wants every
future upgrade without a click sets `upgrade = "act"` once; the first upgrade PR's body says
so.

### `shipmill upgrade`

A new command in `src/shipmill/cli.py`:

- `shipmill upgrade` lists the repo's pending upgrades: those that apply, are undecided,
  and have no declined proposal. `--json` for scripts. Exit 0
- `shipmill upgrade --apply <id>` makes the upgrade's edit in the checkout, and nothing
  else: no commit, no push. An unknown id, an upgrade that doesn't apply or is already
  decided, or a config table the edit can't find exits 2 naming it
- `shipmill upgrade --propose` opens or updates one issue per pending upgrade and is what
  the release workflow runs; with `observe` it opens nothing

### Proposal issues

The release workflow (`.github/workflows/release.yml`), which every client already runs,
runs `shipmill upgrade --propose` in the job that already has `issues: write`. Each issue:

- carries the label `shipmill-upgrade` and a first line marker `<!-- shipmill-upgrade:
  <id> -->`; one issue per id, opened once and kept up to date, as release proposals are
  (`src/shipmill/propose.py`)
- says what the upgrade changes and why, shows the exact config lines, and gives the two
  ways to accept: merge the pull request an agent opens, or run `shipmill upgrade --apply
  <id>` and open one
- says how to decline: close the issue

Closing the issue, as completed or not planned, without the upgrade in the config is a
decline: no issue for that id is opened again, and `shipmill upgrade` stops listing it. A
config that holds the key closes its open issue as completed on the next run. `doctor` warns
about `issues: write` only when `upgrade` is `propose` or `act` and an upgrade is pending
(D-9).

### Upgrade pull requests

An agent session that sees an open `shipmill-upgrade` issue with no pull request
(`skills/github-ship-watch/scripts/watch_state.py` state `UPGRADE_PENDING`, an action row)
runs `shipmill upgrade --apply <id>` on a branch `shipmill/upgrade-<id>`, adds a CHANGELOG
entry when the repo's rules ask for one, and opens the pull request as the App (spec 012)
with `Closes #<issue>`. `skills/github-ship-watch/SKILL.md` gives the steps.

A pull request closed unmerged is a decline too: the session closes its issue as not
planned and opens no new one.

github-pr-triage (`skills/github-pr-triage/SKILL.md`) merges an upgrade pull request only
when `[autonomy] upgrade` is `act` and no hold is open; under `propose` it reviews and
reports it as waiting on the maintainer and never merges it.

### `status` and `doctor`

`shipmill status` and `shipmill doctor` (`src/shipmill/status.py`, `src/shipmill/doctor.py`)
list each pending upgrade with its id and the fix: the open issue's link, or `shipmill
upgrade --apply <id>` when there is none. Pending upgrades never make `doctor` fail: an
upgrade is an offer, not a problem.

## Acceptance criteria

- S-014-1: `shipmill upgrade` lists `changelog-fragments` for a config without `[changelog]
  fragments`, lists nothing for one that sets it to any value, and lists `plugin-update`
  only for a config with an `[agents]` section and no `plugin_update` key
- S-014-2: `shipmill upgrade --apply changelog-fragments` adds `fragments = "changelog.d"`
  under `[changelog]`, creates `changelog.d/README.md`, leaves every other byte of the
  config as it was (comments included), and makes no commit
- S-014-3: `shipmill upgrade --apply` exits 2 naming the id for an unknown id, an upgrade
  that doesn't apply, and one already decided
- S-014-4: `[autonomy] upgrade` accepts `observe`, `propose`, and `act`, defaults to
  `propose`, and refuses any other value naming the allowed ones; an open hold makes `act`
  read as `propose`
- S-014-5: `shipmill upgrade --propose` opens one `shipmill-upgrade` issue per pending
  upgrade with the id marker on its first line, updates it in place on the next run, and
  opens none under `observe`
- S-014-6: an upgrade whose issue was closed without the key in the config is never
  proposed again and is not listed by `shipmill upgrade`
- S-014-7: a run on a config that holds the upgrade's key closes that upgrade's open issue
  as completed
- S-014-8: the release workflow runs `shipmill upgrade --propose` in its own job with
  `issues: write`, after the release jobs, so a failure there fails that job naming the
  upgrade and never stops a release
- S-014-9: `doctor` warns about a missing `issues: write` for upgrades only when `upgrade` is
  `propose` or `act` and an upgrade is pending (D-9), and pending upgrades alone never make
  it exit 1
- S-014-10: `watch_state.py` reports `UPGRADE_PENDING` as an action row for an open
  `shipmill-upgrade` issue with no open pull request closing it, and reports nothing for
  one that has a pull request
- S-014-11: the github-ship-watch and github-pr-triage skills say how an upgrade pull
  request is opened (as the App, branch `shipmill/upgrade-<id>`, `Closes #<issue>`), that
  a closed-unmerged one is a decline, and that it merges only under `act` with no hold
- S-014-12: `shipmill status` lists each pending upgrade with its issue link or the
  `--apply` command as its fix

## Out of scope

- Opening the upgrade pull request from the release workflow: a pull request opened with
  the workflow's `GITHUB_TOKEN` starts no CI, so the pull request comes from an agent
  session or a person
- Consent across many repos at once, such as a key in ship-watch's fleet file: a repo's
  policy lives in its own config (D-4), so each repo says `upgrade = "act"` itself
- Upgrades that change anything but shipmill's own config and files it owns (workflows,
  CI, a repo's code)
- Removing an upgrade a repo accepted: the upgrade's text says which key to delete

## Decisions relied on

- D-4: the setting is a key in `.github/shipmill.toml`'s `[autonomy]` table
- D-8: an open hold turns act into propose
- D-9: doctor warns about `issues: write` only where it is needed
- D-10: the maintainer accepts or declines; shipmill proposes
- D-14: agent sessions write as the App

## Issues

## Verification
