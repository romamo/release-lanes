# S-014: Config upgrades

status: approved

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
- `propose` (the default): an issue per pending upgrade; an agent session asks the
  maintainer through the needs-decision protocol (D-21) and opens the pull request only
  when the answer accepts it
- `act`: no question: the agent session opens the pull request at once, and
  github-pr-triage merges it on green CI like any other

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
  <id> <version> -->`, the version being the release that last changed the upgrade; one issue per id, opened once and kept up to date, as release proposals are
  (`src/shipmill/propose.py`)
- says what the upgrade changes and why, shows the exact config lines, and gives the ways
  to accept: answer the agent's question on the issue, or run `shipmill upgrade --apply
  <id>` and open the pull request yourself
- says how to decline: answer so, or close the issue

The release workflow posts no question itself: a comment by the workflow's
`github-actions[bot]` is neither the gate's App nor an OWNER, MEMBER, or COLLABORATOR, so
`watch_state.py` and `triage_state.py` would never read it as a needs-decision question.

Closing the issue, as completed or not planned, without the upgrade in the config is a
decline: no issue for that id is opened again, and `shipmill upgrade` stops listing it. A
config that holds the key closes its open issue as completed on the next run. `doctor` warns
about `issues: write` only when `upgrade` is `propose` or `act` and an upgrade is pending
(D-9).

### The question

An agent session that sees an open `shipmill-upgrade` issue with no needs-decision answer
and no pull request (`skills/github-ship-watch/scripts/watch_state.py` state
`UPGRADE_PENDING`, an action row) asks the maintainer with the needs-decision protocol
(`skills/github-issue-triage/references/needs-decision.md`), under `propose` only:

```markdown
<!-- shipmill:needs-decision -->
@<login> Decision needed: turn on <id> (<one line on what it changes>)?

1. Turn it on (recommended): <what users get>
2. Turn it on, and take every future upgrade without asking: also sets `[autonomy] upgrade = "act"`
3. Not now: ask again after the next shipmill release that changes this upgrade
4. Never: close this issue; shipmill won't propose it again

Reply here with a number or your own answer; shipmill takes this up on the tick after your reply.
```

As the protocol says, a gate session posts it as the App with the `needs-decision` label,
and an interactive one also asks the same question with AskUserQuestion; a session the
user started by hand asks only in the session and posts the answer on the issue as
`Decision by @<login>, given in chat; relayed by <agent>` (spec 012), so the decision lives
on GitHub either way. While the label is on, `watch_state.py` lists the issue in its
NEEDS_DECISION row, not as `UPGRADE_PENDING`, and starts no session for it.

The session that takes up the answer removes the label and:

- 1: opens the pull request (below)
- 2: opens it with `upgrade = "act"` added to `[autonomy]` too
- 3: leaves the issue open and adds a `shipmill-upgrade-later` label; `upgrade --propose`
  removes that label, and the question is asked again, only when a newer shipmill release
  changes the upgrade's text or edit
- 4, or a reply that declines in its own words: closes the issue as not planned, a decline

### Upgrade pull requests

Accepted (or under `act`, at once), the session runs `shipmill upgrade --apply <id>` on a
branch `shipmill/upgrade-<id>`, adds a CHANGELOG entry when the repo's rules ask for one,
and opens the pull request as the App (spec 012) with `Closes #<issue>` and a line naming
the decision it carries out (`Accepted by @<login> in #<issue>`, or `[autonomy] upgrade =
"act"`). `skills/github-ship-watch/SKILL.md` gives the steps.

github-pr-triage (`skills/github-pr-triage/SKILL.md`) merges an upgrade pull request on
green CI when its issue holds an accepting answer from an OWNER, MEMBER, or COLLABORATOR,
or when `[autonomy] upgrade` is `act`, and never while a hold is open; any other upgrade
pull request waits for a person to merge it. A pull request closed unmerged is a decline
too: the session closes its issue as not planned and opens no new one.

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
  `shipmill-upgrade` issue with neither the `needs-decision` label, the
  `shipmill-upgrade-later` label, nor an open pull request closing it; with the
  `needs-decision` label it lists the issue in NEEDS_DECISION instead, and after a trusted
  reply it reports `UPGRADE_PENDING` again
- S-014-11: the github-ship-watch skill asks the question above under `propose`, with the
  four options in that order, through the needs-decision protocol, and asks nothing under
  `act`; each answer leads to the outcome the spec gives it (pull request, pull request
  with `upgrade = "act"`, `shipmill-upgrade-later`, issue closed as not planned)
- S-014-12: the upgrade pull request is opened as the App on `shipmill/upgrade-<id>` with
  `Closes #<issue>` and the line naming the decision; a closed-unmerged one closes its
  issue as not planned
- S-014-13: github-pr-triage merges an upgrade pull request on green CI only when its issue
  holds an accepting answer from an OWNER, MEMBER, or COLLABORATOR or `upgrade` is `act`,
  never while a hold is open, and otherwise reports it as waiting on the maintainer
- S-014-14: `upgrade --propose` removes `shipmill-upgrade-later` from an issue only when the
  installed shipmill's version of that upgrade is newer than the one the issue was last
  asked about, which the issue's marker records (`<!-- shipmill-upgrade: <id> <version>
  -->`)
- S-014-15: `shipmill status` lists each pending upgrade with its issue link or the
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
- D-21: every question a gate session asks goes through the needs-decision protocol

## Issues

- shipmill/shipmill#279: S-014-1, S-014-2, S-014-3, S-014-4
- shipmill/shipmill#280: S-014-5, S-014-6, S-014-7, S-014-8, S-014-9, S-014-15
- shipmill/shipmill#281: S-014-10, S-014-11, S-014-12, S-014-13, S-014-14

## Verification
