# S-013: Changelog fragments, one file per PR

status: approved

## Problem

Every pull request adds its entry right under `## [Unreleased]` in `CHANGELOG.md`, at the
same spot, so once one merges every other open PR conflicts on `CHANGELOG.md` (#256).
github-pr-triage works around it by landing serially and running `changelog_guard.py union`
(`skills/github-pr-triage/references/landing.md`, Conflicts), but each merge turns every
other open PR into a rebase and a force-push, a contributor without the skills hits the
conflict on every PR, and a union next to a release heading can drop entries into a
released section (#25, #82), which `changelog_guard.py check` and `move` exist to repair. A
`.gitattributes` `merge=union` driver doesn't help: GitHub's merge button ignores merge
drivers, and shipmill shouldn't require client repos to change their git config.

Changelog fragments fix this at the source: each PR adds its own file, so two PRs never
touch the same lines, and shipmill folds the files into `CHANGELOG.md` when it cuts a
stable or hotfix release. `CHANGELOG.md` stays the one history readers see.

## Behaviour

### Opting in

A new key in the `[changelog]` table of `.github/shipmill.toml` (D-4), read by
`src/shipmill/policy.py`:

```toml
[changelog]
path = "CHANGELOG.md"
style = "keep-a-changelog"
fragments = "changelog.d"
```

`fragments` is a directory relative to the repo root. An empty value, an absolute path, or
a path with a `..` part fails the config load (exit 2). Without the key nothing changes:
shipmill reads no fragment directory, even one that exists, and every command's output and
every file it writes are what they are today.

### Fragment files

Each PR adds one file per entry group to the fragment directory. The directory may also hold
a `README.md`, which shipmill ignores and never deletes. Any other file is a fragment and
must be valid; an invalid one fails fast (below).

Keep a Changelog style: the name is `<number>-<slug>.<heading>.md`

- `<number>`: the issue the PR fixes, or the PR's own number when there is no issue; digits
  only. It orders fragments and keeps names unique; nothing looks it up
- `<slug>`: lowercase letters, digits, and single hyphens, such as `merged-pr-head-landed`
- `<heading>`: the category, lowercase: `added`, `changed`, `deprecated`, `removed`,
  `fixed`, `security`, or any other heading the policy's `[bump]` lists, lowercased (init's
  `Breaking` is `breaking`). A `[bump]` heading with a space can't be named by a fragment
- The body is one or more `- ` (or `* `) bullets with indented continuation lines, exactly
  as they would appear in the CHANGELOG. Each bullet is one entry under the heading the name
  gives. A `### ` line, text outside a bullet, or a body with no bullet (empty or only
  whitespace) is invalid

```
changelog.d/
  README.md
  244-merged-pr-head-landed.fixed.md
  251-plugin-update.added.md
```

Dash style (`## Unreleased`, one `### Title` block per entry): the name is
`<number>-<slug>.md`, with no heading part, and the body is exactly one `### Title` block,
the same block the CHANGELOG would hold. A heading part, a body that doesn't start with
`### `, or a second `### ` line is invalid.

### Pending is Unreleased plus fragments

`src/shipmill/changelog.py` gains a reader for the fragment directory, from a checkout or
at a git revision (the same revision the CHANGELOG is read at). With fragments on, what is
pending is the Unreleased entries, then the fragments' entries in file order (by
`<number>` numerically, then by slug), each entry once. A fragment entry that a released
section already holds isn't pending, the rule `Changelog.pending()` applies to Unreleased
today, so a fragment left behind by a failed sync never releases twice.

Every place that reads pending entries reads both:

- `src/shipmill/planner.py`: a lane has something pending when either holds entries, the
  bump part follows the fragments' headings like any entry's (`Planner._part`), and the skip
  reason becomes "nothing pending under Unreleased or in changelog.d/"
- `src/shipmill/propose.py`: the proposal issue lists the fragments' entries with the
  Unreleased ones
- `shipmill notes` (`stamp.notes`): a pre-release's notes render them; a stable release's
  notes stay its CHANGELOG section, which already holds them

An invalid fragment fails every one of these, and `prepare`, with exit 2 and a message
naming the file and what is wrong, before anything is written.

### Folding at a release

- **Stable** (`stamp.stamp`, `Changelog.release`): the new X.Y.Z section holds the pending
  Unreleased entries, the fragments at the release's base, and, per D-25, the entries of the
  version's rc sections, each once, merged under their headings. With fragments on, the
  headings come in Keep a Changelog order (Added, Changed, Deprecated, Removed, Fixed,
  Security), then any other heading in the order it first appears, the order
  `changelog_guard.py move` already uses; under each heading, Unreleased entries come first,
  then fragments in file order, then rc-section entries. The release commit deletes every
  fragment at its base, including one that wasn't pending because its entry was released
  already, and keeps `README.md`. Without fragments the section is rendered as today
- **rc and dev**: an rc or dev release writes no CHANGELOG section today, and with fragments
  it still writes none and changes no fragment. The fragments stay on main until the stable
  release that promotes the rc folds them, so nothing folds twice: shipmill never writes an
  rc section, and D-25's rc sections are only those an earlier release bot left
- **Stable off main** (`land._sync_main`, `stamp.sync`, `shipmill sync`): when the
  promoted rc's base isn't main's head, the sync commit on main deletes the fragment files
  the release commit deleted, and keeps every fragment added to main after the release's
  base
- **Hotfix** (`stamp.hotfix_entries`, `stamp.apply_merges`): the hotfix's entries are the
  Unreleased entries and the fragment files each chosen merge added (`git diff
  --diff-filter=A <merge>^1 <merge> -- <fragments>`). `apply_merges` excludes the fragment
  directory as it excludes the CHANGELOG, so no fragment lands on `release/X.Y`. A merge that
  adds neither an entry nor a fragment still refuses (exit 2). The sync to main deletes the
  hotfix's fragment files from main, so the next stable release doesn't ship them again

### init and doctor

- `shipmill init --fragments` (`src/shipmill/init.py`) writes `fragments = "changelog.d"`
  under `[changelog]` and a `changelog.d/README.md` that states the naming rule for the
  detected style and the accepted headings. `--force` overwrites the README as it does the
  other files. Without `--fragments`, init writes exactly what it writes today: fragments
  stay off by default
- `shipmill doctor` (`src/shipmill/doctor.py`), only when fragments are on (D-9): a
  `changelog fragments` line, OK with the count of fragments; FAIL naming each invalid file
  and its problem; WARN when the directory is missing (a release that deletes every
  fragment of a repo without a `README.md` leaves none), saying to add
  `changelog.d/README.md`. Without the key, doctor prints no such line

### Skills

- `skills/github-pr-triage/scripts/changelog_guard.py fragments --base REF [--dir DIR]
  [--heading NAME ...]`: checks the fragment files added or changed since REF against the
  naming and body rules, with the six Keep a Changelog headings plus each `--heading`, and
  that the PR added no lines under `## [Unreleased]` in `CHANGELOG.md`. Exit 0 when all
  hold, 1 with one line per problem, 2 on bad input (no such directory, a bad REF). It stays
  Python 3.10 compatible with no dependencies, like the other skill scripts
- `skills/github-issue-resolve/SKILL.md`: when the repo's `.github/shipmill.toml` sets
  `[changelog] fragments`, the PR adds a fragment instead of a CHANGELOG line
- `skills/github-pr-triage/references/landing.md` and `skills/github-pr-triage/SKILL.md`: for
  such a repo, PRs land without the serial rebase and `changelog_guard.py union` steps, and
  the reviewer runs `changelog_guard.py fragments` instead of `check`
- `skills/shipmill-setup/SKILL.md` and `docs/release-lanes.md` describe the key and
  `init --fragments`

### Dogfood on shipmill

The last build issue turns fragments on for shipmill itself: `.github/shipmill.toml` sets
`fragments = "changelog.d"`, `changelog.d/README.md` exists, and the rule in `CLAUDE.md`
says a PR adds a fragment rather than an entry under `## [Unreleased]`. Entries already
under Unreleased then still release, since pending reads both. Making fragments the `init`
default for new repos is a later decision, once shipmill has cut a stable release with
them.

## Acceptance criteria

- S-013-1: a policy with `[changelog] fragments = "changelog.d"` loads, and one whose `fragments` is empty, absolute, or contains a `..` part fails the load with exit 2 naming the key; a policy without the key reads no fragment directory, so `shipmill plan` output and the stable release commit's `CHANGELOG.md` are unchanged for a repo whose `changelog.d/` holds a fragment
- S-013-2: with fragments on, an empty Unreleased, and one valid `.added.md` fragment at main's head, `shipmill plan` gives the rc lane a candidate (not "nothing pending") whose version is a minor bump under init's `[bump]` lists, and the proposal issue body `propose` writes contains the fragment's entry text
- S-013-3: with fragments on and no fragment and nothing under Unreleased, `shipmill plan` skips the rc lane with a reason naming both Unreleased and the fragment directory
- S-013-4: a fragment whose name doesn't match `<number>-<slug>.<heading>.md`, whose heading is neither a Keep a Changelog heading nor a `[bump]` heading, whose body is empty or whitespace, or whose body holds a `### ` line or text outside a bullet makes `shipmill plan` and `shipmill prepare` exit 2 with a message naming the file, and `prepare` writes no file
- S-013-5: in dash style, a fragment `<number>-<slug>.md` whose body is one `### Title` block is one pending entry with that block as its text, and a dash fragment with a heading part, a body not starting with `### `, or two `### ` lines makes `shipmill plan` exit 2 naming the file
- S-013-6: a stable `prepare` with fragments on writes an X.Y.Z section holding every pending Unreleased entry and every fragment entry once, under headings in Keep a Changelog order with Unreleased entries before fragment entries under each heading, and its release commit deletes every fragment file at the base, including one whose entry a released section already held, and keeps `changelog.d/README.md`
- S-013-7: a stable promotion whose base holds an rc section of its version (D-25), an Unreleased entry, and fragments writes one X.Y.Z section that holds each distinct entry once, removes the rc section, and deletes the fragments
- S-013-8: an rc `prepare` with fragments on changes no file under the fragment directory and adds no CHANGELOG section, and `shipmill notes` for that rc's tag lists the fragment entries at the tag along with the Unreleased ones
- S-013-9: when a stable release is cut off main and synced back (`shipmill sync`), the sync commit deletes from main the fragment files the release commit deleted and keeps a fragment added to main after the release's base
- S-013-10: a hotfix of merges that each added a fragment ships those fragments' entries in its X.Y.Z section, `release/X.Y` gains no file under the fragment directory, the sync to main deletes those fragment files from main, and a chosen merge that adds neither an Unreleased entry nor a fragment makes `prepare` exit 2
- S-013-11: with fragments on, `shipmill doctor` prints a `changelog fragments` line that is OK with the fragment count for valid fragments, FAIL naming the file for an invalid one (and doctor exits 1), and WARN when the directory is missing; without the key doctor prints no `changelog fragments` line
- S-013-12: `shipmill init --fragments` writes `fragments = "changelog.d"` under `[changelog]` in `.github/shipmill.toml` and a `changelog.d/README.md` stating the naming rule for the detected style, and `shipmill init` without the flag writes byte-identical files to today's and no `changelog.d/`
- S-013-13: `changelog_guard.py fragments --base REF` exits 0 when every fragment added since REF is valid and `CHANGELOG.md` gained no line under `## [Unreleased]`, exits 1 printing one line per invalid fragment or added Unreleased line, accepts an extra heading only through `--heading`, and exits 2 when the directory or REF doesn't exist
- S-013-14: `skills/github-issue-resolve/SKILL.md` tells the implementer to add a fragment instead of a CHANGELOG line when `.github/shipmill.toml` sets `[changelog] fragments`, and `skills/github-pr-triage/references/landing.md` says such a repo skips the serial landing and `union` steps and runs `changelog_guard.py fragments`
- S-013-15: shipmill's own `.github/shipmill.toml` sets `fragments = "changelog.d"`, `changelog.d/README.md` exists, `CLAUDE.md` says a pull request adds a fragment under `changelog.d/`, and `shipmill doctor` on the repo reports no FAIL

## Out of scope

- Making fragments the `shipmill init` default: a later spec, once shipmill has cut a
  stable release with its own fragments (S-013-15)
- Front matter inside a fragment, or a heading chosen anywhere but the file name
- Converting a repo's existing Unreleased entries into fragments: pending reads both, so
  they release as they are
- A `shipmill fragment` command that writes a fragment; the skills and contributors write
  the file by hand
- `.gitattributes` merge drivers, which GitHub's merge button ignores
- Writing rc sections into the CHANGELOG: shipmill's rc lane writes none, with or without
  fragments

## Decisions relied on

- D-3
- D-4
- D-9
- D-25

## Issues

## Verification
