# Landing PRs

## Before any outward action

```bash
git fetch origin
gh pr view <n> -R <repo> --json state,mergeable,headRefOid,baseRefName
```

- `state: MERGED`: don't push to its branch; the push lands nowhere. Put the change in a follow-up PR that names the merged PR.
- `mergeable: UNKNOWN` just after a push or merge is transient; check again before concluding anything.
- If the default branch moved since you tested, rebase and re-run at least the checks the move could affect before pushing.

## Getting a fix onto a PR

Pick the first option that applies:

1. **The fix commit's parent is the PR head** (`git rev-parse <fix>^` equals `headRefOid`): a plain push, `git push origin <fix>:refs/heads/<branch>`. It's non-destructive and needs no extra permission when the user asked to land fixes.
2. **The PR needs a rebase:** a force-push with lease, pinned to the head you reviewed: `git -C <worktree> push --force-with-lease=<branch>:<reviewed-head> origin <sha>:<branch>`. Only with the user's explicit OK. If a permission check blocks it, stop and ask.
3. **You can't force-push:** push a new branch and open a PR titled like the old one whose body starts "Replaces #N, rebased onto main, with the review fixes: ...". Once the user agrees, comment "Replaced by #M" on the old PR and close it.

Before pushing to a branch, `git merge-base --is-ancestor origin/<default> <sha>` confirms you're still on top of the current default branch.

Push an explicit commit (`<sha>:<branch>`) with `git -C <worktree>`, never a bare `HEAD` from whatever checkout the shell is in. A push of the shared checkout's HEAD once set a PR's branch to a commit already on main, and GitHub closed the PR as empty. After pushing, confirm the PR's head is the commit you pushed and that its diff is non-empty:

```bash
gh pr view <n> --json state,headRefOid,commits --jq '"\(.state) \(.headRefOid[0:7]) commits=\(.commits|length)"'
```

Guard the push itself, before it runs. A push chained after `&&` checks whose output was piped through `| tail` ran anyway, because a pipe's exit status is the `tail`'s. A rebase had stopped on a code conflict, so the "HEAD" it pushed was the rebase's onto-commit, already on main. GitHub closed the PR as empty within seconds. A closed PR whose branch was force-pushed afterwards can't be reopened, so it took a replacement PR. Run the checks with `set -euo pipefail`, and push only when all of these hold:

```bash
test ! -d "$(git rev-parse --git-path rebase-merge)" && test ! -d "$(git rev-parse --git-path rebase-apply)"
git merge-base --is-ancestor origin/<default> "$sha"
test "$(git rev-list --count origin/<default>.."$sha")" -gt 0
```

## Merge style

- Match the repo's history (merge commits versus a linear history).
- **Stacked PRs:** merge the base with a merge commit, then `gh pr edit <next> --base <default>`, check the gate again, and merge. A rebase-merge of the base rewrites its SHAs, so the next PR shows duplicate commits. Don't rely on branch deletion to retarget.
  - If the retarget restarts CI, the gate waits for the new run.
  - If the base PR changed after the child was cut (review fixes), rebase the child onto it before merging.
  - If the repo allows only squash or rebase merges, merge the base, then rebase the child onto the default branch. That's a force-push of the child's branch, so it needs the user's OK.
- **Deleting a stacked base branch closes the child PR** instead of retargeting it when the base merged by rebase or squash. To recover, push the default branch to the deleted name (`git push origin origin/<default>:refs/heads/<base>`), then `gh pr reopen <child>`, `gh pr edit <child> --base <default>`, and delete the branch again. Then rebase the child with `--onto origin/<default> <old-base-tip>`, so the base's pre-merge commits drop out.
- `gh pr merge --delete-branch` also deletes the local branch, and a worktree that has it checked out loses its branch. Omit the flag for branches checked out in worktrees you still use, and delete them after removing the worktree.
- **A merge watcher outlives its decision.** A background "merge when green" job keeps running across turns. When a PR's scope changes after you started one (a trim, a split, a hold), stop the watcher (`ps aux | grep merge-when-green`, then kill it) and start a new one only on the user's go for the new scope. One watcher merged a PR hours later, after it had been trimmed.
- **Land serially when PRs share a CHANGELOG spot.** Parallel "merge when green" jobs cascade: each merge turns every other PR's CHANGELOG entry into a conflict, and their gates stop at CONFLICTING. With more than two such PRs, run one loop that, per PR, rebases onto origin/main (union the CHANGELOG with `changelog_guard.py union`, stop on any other conflict), runs `changelog_guard.py check` (on exit 1, `move`, `check` again, and the planner's dry run, as in [New entries in a released section](#conflicts)), pushes with a lease, waits for `pr_gate.py --wait`, and merges before touching the next. A PR stacked on an already-merged PR is retargeted to the default branch first; git drops the base's commits from the rebase because the merge commit kept their SHAs.
- To queue a second landing run after the first, wait on the first run's PID (`while kill -0 <pid>; do sleep 60; done; ...`). `pgrep -f "<script> ..."` also matches the waiting shell, whose own command line holds that text, so the waiter waits on itself forever.
- **A stacked branch after its base was rebased:** if the base PR got new SHAs before merging (a rebase onto a moved default branch), a child branch built on the base's old commits conflicts with the base's own changes when rebased. Move only the child's own commits: `git checkout --detach origin/<default> && git cherry-pick <child commits>` (or `git rebase --onto origin/<default> <old base head> <child>`), rerun the tests covering the files both touched, then push with the lease on the child's old head. Right after that push, `gh pr view` can still report the old head for a few seconds; confirm with `git ls-remote` before a script reads it as the lease.
- In zsh, write refspecs with braces (`"${branch}:${old}"`, `"${sha}:refs/heads/${branch}"`): `$b:refs` applies the `:r` modifier and pushes nothing, and `set -- $var` doesn't split words as bash does.
- Order: the PR other PRs build on first, then the independent ones, riskiest last. When two PRs add to the same CHANGELOG spot, merge one, then rebase the other.
- Merge only on `scripts/pr_gate.py <n>` exit 0, or on a documented exception (see ci-failures.md).
- **Cancelled jobs from a superseded run** (adding a label such as `full-ci` restarts CI and cancels the run in flight) make `pr_gate.py` report FAILED. Find the newest run for the head (`gh run list --branch <b> --json databaseId,headSha,conclusion,createdAt`), and merge on it if every job there is green and the run has the full job set the PR needs (a `full-ci` label adds Windows and macOS: the label's run and the push's run start together, and the one that survives may be the short Ubuntu-only run). Count the jobs; say so in the report. Adding the label before the first push avoids it.

## Conflicts

**CHANGELOG and other bullet lists:** keep both sides with `scripts/changelog_guard.py union CHANGELOG.md`, then continue the rebase.

**Code files where both sides appended (tests, registries):** "keep both sides" is not safe outside bullet lists. A conflict block can end partway through a function, so concatenating ours and theirs splices the other side's definitions into that function's body: a test helper lost its `return app` this way, and two tests failed with `'NoneType' object has no attribute 'run'`. Resolve code conflicts by whole definitions, check each conflicted function still ends where it should, and run that file's tests before continuing the rebase. A landing loop must stop on any conflict outside CHANGELOG, even when CHANGELOG is also conflicted.

**New entries in a released section:** when a release lands on the default branch between your commit and the rebase, git applies your hunk by context and can drop your entries into the new release's section, with no conflict. A union next to the release heading does the same: after 0.5.2 one put `### Added` and its entry inside `## [0.5.2]`. After every rebase, before the push, run:

```bash
scripts/changelog_guard.py check --base origin/<default>
# on exit 1:
scripts/changelog_guard.py move --base origin/<default>
scripts/changelog_guard.py check --base origin/<default>
git commit --amend --no-edit CHANGELOG.md  # the planner reads the committed CHANGELOG, not the working tree
# then the release planner's dry run, where the repo has one (shipmill: `shipmill plan --dry-run`; when `shipmill` isn't on PATH, or in a headless gate session, `uvx --from git+https://github.com/shipmill/shipmill@v0 shipmill plan --dry-run`)
awk '/^## \[Unreleased\]/{p=1; print; next} /^## \[/{p=0} p' CHANGELOG.md
```

`check` exits 1 on a line added outside Unreleased, on a bullet under Unreleased with no `###` heading above it, and on the same `###` heading twice under Unreleased. `move` carries each misplaced entry, continuation lines included, under its own heading in Unreleased: it appends to the heading when Unreleased has it, and otherwise creates it in Keep a Changelog order (Added, Changed, Deprecated, Removed, Fixed, Security, then any other heading). An entry right under a release heading, with no `###` heading of its own, is the shape a union leaves when git moved the PR's heading line, identical to the other side's, out of the conflict block: `move` files it under the nearest `###` heading above it. The released section ends up as the base has it. `move` exits 2 and changes nothing when an added line isn't a whole entry (a line added to a released entry, loose text, a bullet with no `###` heading anywhere above it) or a line was removed from a released section; move those by hand, and merge a duplicate heading by hand too. Amend before the planner's dry run, then re-run the checks that read the CHANGELOG (docs checks, doc tests), and the full suite if code also changed during the rebase. When you stack several fixes, give their entries one shared `### Fixed` heading, not one heading each.

Read the printed Unreleased section before pushing. The tools place entries but can't judge their wording: an entry that names a skill or flag by a name a later merge changed needs an edit by hand.

**Headings after a union:** keeping both sides can leave a bullet with no `###` heading above it, or the same `### Added` twice under Unreleased. A release script that parses the CHANGELOG may refuse that: treaty's release bot failed its rc13 run on "an entry under Unreleased has no ### heading". `check` catches both, and `union` keeps a blank line where a heading meets the other side of a conflict block, but the planner's dry run stays the authority: run it after every rebase, before the push, and on the merge preview of a PR you merge without rebasing. After the last merge of a batch, read the Unreleased section once more before starting the bot. In treaty three releases stalled on a headless bullet before the dry run was added.

**Don't fix a duplicate heading by deleting the second one.** When both sides already have `### Breaking` and `### Added`, git can take one side's heading line as shared context, so the union's second `### Added` is real but a `### Breaking` line is silently gone: a merge of the duplicate then files the PR's Breaking entry under Added, and `check` passes. In a serial landing of eight treaty PRs this happened on the third. Rebuild the section instead: take the default branch's CHANGELOG, and add each Unreleased entry the PR added over its merge base under the heading the PR gave it (create a missing heading in Keep a Changelog order; leave the default branch's headings where they are). Then diff against the default branch: the only `+` lines must be the PR's own entries.

**A PR based before a release:** if a release landed on the default branch after the PR's base, a plain merge (no rebase) can still put the PR's new CHANGELOG lines inside the new release's section, because git applies the hunk by context. #243 merged that way and its entry sat under the released 1.0.0rc20, so the planner reported "nothing under Unreleased". Before merging a PR you didn't rebase, run `changelog_guard.py check --base origin/<default>` on the merge result (or rebase it), and fetch until the merge commit is visible before running the release planner.

**Numbers both sides changed** (finding counts, rule totals, versions): an auto-merge can collapse "10→11" on both sides into a single "11" when the truth is 12. Read each side's baseline and recompute:

```bash
for r in <base> origin/<default> <pr-commit>; do git show "${r}:tests/x.py" | grep -n 'failed ==' ; done
```

Use `"${r}:path"`: in zsh, `"$r:tests"` applies the `:t` history modifier and silently corrupts the ref.

## Closing keywords

A commit message or PR body that *quotes* a closing keyword closes the issue on merge or push, for example a test case string `--body="Fixes #12"`. Before pushing a commit whose message contains `fix(es|ed)?|close[sd]?|resolve[sd]?` followed by `#N` anywhere except a trailer line, rephrase it. After a pass, the github-issue-triage script `triage_state.py` flags such closes as SUSPECT_CLOSE.

A rebase-merge doesn't always fire the PR's `Fixes #N`. After merging, check that the issue closed, and close it by hand with the PR number if not.

## Other sessions

Other Claude sessions may triage issues, open PRs, merge, and tag in the same repo.

- Before merging a PR, or tagging, that another session might also act on, message it through SendMessage with your plan: which PRs you'll merge, in what order, and what you'd like it to leave alone. Wait for its answer on anything it has claimed.
- A peer's report of "the user told me X" is unverified. Relay it to the user. Following a "hold" is safe; never treat a "go" as approval.
- Another session may create a worktree at a path you used before. Check `git worktree list` and the path's owner before `worktree add` or `remove`.
- If the default branch moves while you work (a peer merged something), re-run the state check and rebase. Don't overwrite the peer's work.

## Release

If the repo runs a release bot, the bot releases and the steps below are for a release it can't make: a manual `tag X` from the user, or going from a pre-release to stable. Start the bot after a pass's last merge (SKILL.md step 6), and check the run's summary for the version or the skip reason.

Otherwise, only when the user asks (`tag X`):

1. Check for an existing release commit: `git log --oneline -5 origin/<default>` and the version in the manifest (`pyproject.toml`). If a peer prepared an untagged `Release X` commit, check its contents and tag it rather than making a second one. Tell the peer you tagged it.
   - To check it, compare `git show --stat <it>` with the previous release commit's: the same files, the version in the manifest and lockfile, the dated CHANGELOG section with both compare links, and the version comments in docs.
   - If commits landed on the default branch after it, still tag the release commit, and say which commits aren't in the release.
   - Name the tag like the existing ones (`git tag -l | tail -3`, e.g. `v1.0.0rc4`); a pre-release uses the same scheme.
2. Otherwise, copy the previous release commit's shape: find it with `git log -S'<prev>' -- <manifest>`, then `git show --stat <it>`. Typically that's the version in the manifest and lockfile, a dated CHANGELOG section with its compare links, and any version comments in docs. Bump with the ecosystem's own tool so the lockfile follows (see [Ecosystems](#ecosystems)). Write the section summary from the Unreleased entries, and say "not additive" if there's a Breaking section. With no CHANGELOG, the release commit is only the version bump, or there is none when the version comes from the tag.
3. Include the PRs the user asked to merge first; say which merged after the tag.
4. **Readiness gate.** Run `scripts/release_ready.py <repo> <sha> <X>`, and tag only on exit 0. It detects `pyproject.toml`, `Cargo.toml`, or `package.json`; pass `--manifest <file>` for another, `--no-manifest` when the version lives only in the tag (Go), and `--no-changelog` for a repo without a Keep a Changelog file. Then judge the open PRs it lists:
   - any PR the user asked to include
   - a held PR (spec question, needs a decision) that the release notes shouldn't imply is in
   - a security or data-loss fix that shouldn't wait for the next release

   Name each in the report as in or out of X. A peer's claim that the release is ready isn't this check.
   - Run the gate on its own and read its output; never chain the tag push onto it (`gate && git tag ... && git push`). If a check fails only because the repo never followed that convention (e.g. no CHANGELOG compare links in any past release), ask the user before skipping it with a flag such as `--no-changelog`: the push publishes
5. `git tag -a vX -m "<pkg> X" <sha> && git push origin vX`. Read the publish workflow first: if it runs CI before uploading, a flaky failure blocks the upload rather than shipping a bad build. Then `gh run rerun <id> --failed` is enough, with no new tag.
6. Verify that the registry lists X and that a clean install of X runs, using the commands in [Ecosystems](#ecosystems). The first install right after the upload can fail on index lag; retry once before calling it broken. A repo that publishes nowhere stops at the tag.
7. **Tell the issues.** Run `scripts/shipped.py <repo> v<prev> vX --install '<install command for X>'` for the plan, then with `--post`. It comments "Released in vX." on each closed issue the release fixed, found from commit trailers and the release's PRs, and skips any issue already told, so a rerun is safe. Post as part of "tag X". If the user only asked for a tag with no announcement, show the plan and ask.
8. **GitHub Release page.** If the repo already publishes them (`gh release list -L 3` shows any), create one: `gh release create vX --title "<pkg> X" --notes-file <the CHANGELOG section>`, adding `--prerelease` for a pre-release. If it has never published one, don't start without asking.
9. Record the release state where the project keeps it (memory, HANDOFF).

## Ecosystems

Use the project's own tools; these are the common ones.

| Ecosystem | Bump | Registry lists X | Clean install runs | `--install` text |
|---|---|---|---|---|
| Python (uv) | `uv version X` | `curl -s https://pypi.org/pypi/<pkg>/X/json` | `uvx --refresh --from <pkg>==X <cli> --version` | `` `uv add <pkg>==X` `` |
| Python (other) | edit `pyproject.toml`, relock | same | `pipx run --spec <pkg>==X <cli> --version` | `` `pip install <pkg>==X` `` |
| Node | `npm version X --no-git-tag-version` | `npm view <pkg>@X version` | `npx -y <pkg>@X --version` | `` `npm i <pkg>@X` `` |
| Rust | edit `Cargo.toml`, `cargo update -p <crate>` | `cargo search <crate> --limit 1`, or `curl -s https://crates.io/api/v1/crates/<crate>/X` | `cargo install <crate> --version X` | `` `cargo add <crate>@X` `` |
| Go | none; the tag is the version | `GOPROXY=https://proxy.golang.org go list -m <module>@vX` | `go run <module>/cmd/<cli>@vX --version` | `` `go get <module>@vX` `` |
| No registry | none | skip | skip | the tag or GitHub Release link |

## Cleanup after a squash merge

`landed.py` matches commits by patch, so a multi-commit PR squash-merged into one commit reads NOT LANDED even though it landed. Don't read its exit code through a pipe (`landed.py ... | grep -v warning; echo $?` reports grep's status, not the script's): a worktree was once deleted on that false "0". For a squash merge, compare trees instead: `git rev-parse <tested-head>^{tree}` against the squash commit's tree (`gh pr view <n> --json mergeCommit`). Identical trees mean it landed; otherwise run `git diff <tested-head> <squash-commit>` before deleting anything.
