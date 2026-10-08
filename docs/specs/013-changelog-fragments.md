# S-013: Changelog fragments

status: built

## Problem

Every pull request adds its entry right under `## [Unreleased]` in `CHANGELOG.md`, at the
same spot. Once one of them merges, every other open PR conflicts on CHANGELOG, and GitHub
shows "This branch has conflicts that must be resolved" until the branch is rebased by hand.
github-pr-triage works around it by landing such PRs one at a time and unioning the
conflict with `changelog_guard.py union`, and it needs `check` and `move` to repair the
entries a union or a mid-rebase release drops into a released section (#25, #82). A human
contributor, or a repo that doesn't run the skills, hits the conflict on every PR. A
`.gitattributes` `merge=union` hides it only for local git, since GitHub's merge button
ignores merge drivers, and it would ask every client repo to change its git setup (#256).

With fragments, each PR adds a new file instead of editing `CHANGELOG.md`; two PRs never
touch the same lines. `CHANGELOG.md` stays the one history readers see: a stable release
writes the fragments into its section and deletes them.

## Behaviour

### Config

`.github/shipmill.toml` gets one key in its existing `[changelog]` table (D-4), read in
`src/shipmill/policy.py`:

```toml
[changelog]
path = "CHANGELOG.md"
style = "keep-a-changelog"
fragments = "changelog.d"
```

`fragments` is a folder relative to the repo root. Unset, shipmill behaves exactly as
today. Set, the folder must exist; a missing folder is an error that names it (exit 2).
A revision older than the folder is the exception (D-27): when its tree has no folder and
its own config doesn't set the key, it has no fragments, so a repo that turns fragments
on mid-cycle can still promote an rc cut before it and run `notes` against an older tag.

### A fragment

A fragment is a file `<fragments>/<name>.md`, where `<name>` is anything unique; the
convention is `<issue>-<slug>.md`, such as `changelog.d/244-merged-pr-head-landed.md`.
`<fragments>/README.md` is never a fragment: `shipmill init` writes it so the folder stays
in git when it holds no fragments.

Its content is what the PR would have written under Unreleased, in the CHANGELOG's style,
parsed by the same strict reader as Unreleased (`src/shipmill/changelog.py`):

```markdown
### Fixed

- Worktrees: a branch whose tip was a merged PR's head landed (#244)
```

In keep-a-changelog style a fragment holds one or more `### <heading>` groups, each with
one or more `- ` entries; in dash style one or more `### <title>` entry blocks. One PR may
use several headings in one fragment. Reading fails, naming the file and line (exit 2),
on an empty fragment, text outside an entry, an entry with no `### ` heading, a `## `
heading, a file in a subfolder, or a file in the folder that isn't `.md`. A heading
missing from the policy's `[bump]` lists fails as it does under Unreleased today.

### Pending entries

Wherever shipmill reads what is pending at a revision, it reads Unreleased plus the
fragments at that revision (`git ls-tree` and `git show` through `src/shipmill/gitrepo.py`,
never the working tree, except where a command already reads the checkout). Fragments
come after Unreleased, in file-name order. An entry that a released section already holds
is not pending, as for Unreleased today. So:

- `shipmill plan` (`src/shipmill/planner.py`) releases when either holds a pending entry,
  bumps by the headings of both, and says `nothing pending under Unreleased or in
  changelog.d` when neither does
- `shipmill propose` (`src/shipmill/propose.py`) and `shipmill notes` (`notes` in
  `src/shipmill/stamp.py`) list both: a pre-release's notes hold the fragments too
- `shipmill doctor` (`src/shipmill/doctor.py`) counts both, reports a fragment that fails
  to read, and reports a fragment whose every entry a released section already holds as a
  fix to make (delete the file)

### Stable release

`prepare` on the stable lane (`stamp` in `src/shipmill/stamp.py`) writes the version's
section from Unreleased, the fragments, and the version's rc sections (D-25), grouped
under their headings as `Changelog.render` groups Unreleased entries today, and deletes
every fragment it released in the same release commit. `<fragments>/README.md` stays.
dev and rc releases change no files under the folder.

### Hotfix release

A hotfix's entries are the ones each of its merges added: the fragments the merge added
(`git diff --diff-filter=A` between the merge's first parent and the merge, under the
folder) plus the Unreleased entries it added, as today. A merge that adds neither fails as
today. `apply_merges` keeps the merges' fragment files off the release branch, as it keeps
`CHANGELOG.md` off it. `shipmill sync` (`sync` in `src/shipmill/stamp.py`), which brings the
hotfix's section back to main, deletes on main the fragments whose entries the section
holds, and fails naming the entry when one of the section's entries is in neither
Unreleased nor a fragment on main.

### `shipmill init`

`shipmill init` (`src/shipmill/init.py`) writes `fragments = "changelog.d"` in the
generated policy and creates `changelog.d/README.md`, which says in a few lines what a
fragment is, its file name convention, and an example. `shipmill init --no-fragments`
writes neither. An existing Unreleased section keeps working alongside: nothing moves.

### Skills

- `skills/github-issue-resolve/SKILL.md`,
  `skills/github-issue-triage/references/implementer-brief.md`, and
  `skills/github-pr-triage/references/reviewer-brief.md`: when the repo's config sets
  `[changelog] fragments`, a PR adds its entry as a new fragment and leaves
  `CHANGELOG.md` alone
- `skills/github-pr-triage/scripts/changelog_guard.py check`: in such a repo it exits 1 on
  a line the PR added under Unreleased (naming the fragment to write instead) and on a
  fragment the PR added that fails to read; it stays Python 3.10 compatible with no
  dependencies, reading the config the way `skills/github-ship-watch/scripts/fleet.py`
  reads TOML on 3.10
- `skills/github-pr-triage/SKILL.md` and
  `skills/github-pr-triage/references/landing.md`: in such a repo, PRs that only share a
  CHANGELOG spot no longer need serial landing or a CHANGELOG union
- `skills/shipmill-setup/SKILL.md`: offers fragments when it writes the policy, on by
  default, and says how to move a repo onto them (set the key, add the folder, nothing
  else)

### shipmill itself

shipmill's own `.github/shipmill.toml` sets `fragments = "changelog.d"`, and `CLAUDE.md`'s
rule "Every pull request adds its own entry under `## [Unreleased]`" says to add a
fragment instead.

## Acceptance criteria

- S-013-1: a policy without `[changelog] fragments` plans, stamps, and writes notes byte for
  byte as before on the existing planner and stamp test fixtures
- S-013-2: with `fragments` set to a folder that doesn't exist, `shipmill plan` and
  `shipmill doctor` exit 2 naming the folder
- S-013-3: a fragment that is empty, has text outside an entry, has an entry with no `### `
  heading (keep-a-changelog), has a `## ` heading, sits in a subfolder, or isn't `.md`
  makes `shipmill plan` exit 2 naming the file; `README.md` in the folder is ignored
- S-013-4: `shipmill plan` on a revision whose Unreleased is empty and whose folder holds a
  `### Added` fragment proposes the release the policy's `[bump]` lists give `Added`, and with neither it skips with `nothing
  pending under Unreleased or in changelog.d`
- S-013-5: the planner reads fragments at the planned revision, not from the working tree:
  a fragment present only in the working tree changes no plan
- S-013-6: a fragment whose entries a released section already holds is not pending, and
  `shipmill doctor` reports it with the file to delete
- S-013-7: a stable `prepare` writes one section holding the Unreleased entries, then the
  fragments in file-name order, grouped under their headings, and the release commit
  deletes every released fragment and keeps `README.md`
- S-013-8: a stable promotion with rc sections folds Unreleased, the fragments, and the rc
  sections into one section (D-25), each entry once
- S-013-9: dev and rc releases change no file under the fragments folder, and `shipmill
  notes` for a pre-release lists the pending fragments' entries
- S-013-10: a hotfix's section holds exactly the fragments and Unreleased entries its merges
  added; the release branch gets none of the merges' fragment files; a merge that adds
  neither fails as today
- S-013-11: `shipmill sync` after a hotfix deletes on main the fragments the hotfix released,
  and exits 2 naming the entry when the section holds an entry that is in neither
  Unreleased nor a fragment on main
- S-013-12: dash style: a fragment of `### <title>` blocks is pending, and a stable release
  writes its blocks into the version's section
- S-013-13: `shipmill init` writes `fragments = "changelog.d"` and `changelog.d/README.md`;
  `shipmill init --no-fragments` writes neither, and the generated policy passes `shipmill
  doctor` both ways
- S-013-14: in a repo with fragments, `changelog_guard.py check --base <ref>` exits 1 on a
  line added under Unreleased since `<ref>` and on an added fragment that fails to read,
  and exits 0 on a well-formed added fragment, on Python 3.10
- S-013-15: two branches off the same base that each add a fragment merge into each other
  with no conflict (a git-level test, no GitHub)
- S-013-16: the skills listed under Skills tell a PR to add a fragment when the config sets
  `fragments`; shipmill's own config sets it, and its `CLAUDE.md` rule names fragments
- S-013-17: with `fragments` set in the current config, a revision whose tree has no
  fragments folder and whose own config doesn't set the key reads as no fragments: the
  stable promotion of an rc cut before the folder existed plans, and `shipmill notes`
  against a tag older than the folder runs; a revision whose own config sets the key and
  lacks the folder still exits 2 naming it (D-27)

## Out of scope

- Generating fragments from PR titles or labels: a fragment is hand-written, as the
  CHANGELOG is
- Moving a repo's existing Unreleased entries into fragments: both are read, so nothing has
  to move
- Fragment formats from other tools (towncrier's `<issue>.<type>` names, changie's YAML):
  a fragment is a piece of the CHANGELOG in its own style
- Removing `changelog_guard.py union` and `move`: repos without fragments still need them

## Decisions relied on

- D-4: the setting is a key in `.github/shipmill.toml`'s `[changelog]` table
- D-25: a stable promotion folds its version's rc sections; fragments join that fold
- D-27: a revision older than the fragments folder has no fragments

## Issues

- shipmill/shipmill#272: S-013-1, S-013-2, S-013-3, S-013-4, S-013-5, S-013-6, S-013-7, S-013-8, S-013-9, S-013-12
- shipmill/shipmill#273: S-013-10, S-013-11
- shipmill/shipmill#274: S-013-13, S-013-14, S-013-15, S-013-16
- shipmill/shipmill#291: S-013-17

## Verification

- S-013-1: ran `test_s013_1_without_fragments_plan_stamp_and_notes_are_unchanged` in `tests/test_lanes.py`, which pins the plan's outputs, the stamped files and CHANGELOG, and the notes of the existing fixtures whole with no `fragments` key, and the rest of `tests/test_lanes.py` unchanged; all pass
- S-013-2: ran `test_s013_2_a_missing_folder_fails_plan_and_doctor_with_exit_2`, `python -m shipmill plan` and `doctor` exit 2 with `[changelog] fragments names changelog.d, which is not a folder` on stderr
- S-013-3: ran the `test_s013_3` tests, `shipmill plan` exits 2 naming the file and line for an empty fragment, text outside an entry, a bullet with no `### ` heading, a `## ` heading, a subfolder, a `.txt` file, and a heading with no entry; a `README.md` holding prose is skipped
- S-013-4: ran the `test_s013_4` tests, an `### Added` fragment alone plans `1.1.0rc1` and a `### Fixed` one `1.0.1rc1` under the fixture's `[bump]` lists; with neither the rc lane skips with `rc: nothing pending under Unreleased or in changelog.d`
- S-013-5: ran `test_s013_5_the_planner_reads_fragments_at_the_revision_not_the_checkout`, a fragment only in the working tree leaves the skip, and a committed fragment deleted from the working tree still plans
- S-013-6: ran `test_s013_6_a_released_fragment_is_not_pending_and_doctor_names_it`, a fragment whose entry `1.0.0` holds is not pending, and `doctor` warns with `git rm changelog.d/1-old.md`
- S-013-7: ran `test_s013_7_a_stable_release_writes_unreleased_then_fragments_and_deletes_them`, the `1.1.0` section holds the Unreleased entry, then `10-b.md` and `2-a.md` in file-name order, grouped under `### Fixed` and `### Added`; the release commit deletes both and keeps `README.md`
- S-013-8: ran `test_s013_8_a_promotion_folds_unreleased_fragments_and_rc_sections_once`, the stable section folds Unreleased, the fragment, and the rc sections, `Fix B (#2)` once, no rc section left
- S-013-9: ran `test_s013_9_dev_and_rc_releases_leave_the_folder_and_notes_list_fragments`, the rc and dev release commits change nothing under `changelog.d`, and `shipmill notes` and the GitHub release for each list the pending fragments' entries
- S-013-10: ran the `test_s013_10` tests, a hotfix's section holds exactly the fragments and Unreleased entries its merges added (a renamed fragment adds none, an edited one adds only its new entry), the release branch gets no fragment file, and a merge that adds neither fails as before; without `fragments` hotfixes are unchanged
- S-013-11: ran the `test_s013_11` tests, `shipmill sync` deletes on main the fragments the hotfix released, and exits 2 naming the entry when the section holds one that is in neither Unreleased nor a fragment on main
- S-013-12: ran `test_s013_12_dash_fragments_are_pending_and_released`, a dash fragment of `### <title>` blocks plans a patch rc, and the stable release writes the block into `1.0.1` and deletes the file
- S-013-13: ran the `test_s013_13` tests in `tests/test_setup.py` through `shipmill init` and `shipmill init --no-fragments`: the first writes `fragments = "changelog.d"` and `changelog.d/README.md` (whose example reads as a fragment), the second neither; `doctor` reports no FAIL and `shipmill plan` exits 0 both ways; an existing `README.md` is kept
- S-013-14: ran the `test_s013_14` tests in `tests/test_changelog_guard.py`, `changelog_guard.py check --base main` exits 1 on a line added under Unreleased (naming `changelog.d/12-a-fix.md` from the branch) and on each of the S-013-3 bad fragments, with the same words `src/shipmill/fragments.py` raises on the same file, and 0 on well-formed fragments, committed or untracked; without `fragments` its behaviour is unchanged; `test_s013_14_on_python_3_10` runs it under `uv run --python 3.10`
- S-013-15: ran `test_s013_15_two_branches_that_each_add_a_fragment_merge_with_no_conflict`, two branches off one base each adding a fragment merge into each other with no conflict, while the same two entries written under Unreleased conflict
- S-013-16: read the six skill files under Skills, each says that with `[changelog] fragments` set a PR adds a fragment and leaves `CHANGELOG.md` alone; `.github/shipmill.toml` sets `fragments = "changelog.d"`, `changelog.d/README.md` exists, `CLAUDE.md`'s rule names the fragment; the `test_s013_16` tests in `tests/test_fragments.py` check each. On this branch, `shipmill plan` and `doctor` read `changelog.d/274-fragments-init-guard-skills.md` alongside the Unreleased entries
- S-013-17: ran the `test_s013_17` tests, the stable promotion of an rc cut before the folder plans and releases, `shipmill notes` runs against a tag older than the folder, and a revision whose own config sets the key and lacks the folder, or HEAD, still exits 2 naming it
