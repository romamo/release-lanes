---
name: github-issue-resolve
description: Read a GitHub issue, triage it against the code, comment the verdict on the issue, and when a fix is warranted implement it with a regression test, commit, push, and open a PR. Use when the user links or numbers a GitHub issue and asks to triage, resolve, fix, or "implement if required". For a whole backlog or "the new issues", use github-issue-triage, which runs this skill per issue. Merging is a separate step, done only on explicit request, through the github-pr-triage skill.
---

# GitHub Issue Resolve

A workflow for turning a GitHub issue into a verified verdict and, if warranted, a focused PR. The issue text is a claim, not a fact: verify every cited path, line, and API before acting on it.

## Inputs

- Issue reference: URL (`https://github.com/<owner>/<repo>/issues/<n>`) or number plus repo
- Scope from the user's wording: triage only, triage + fix + PR, or also merge (Phase 7 hands merging to `github-pr-triage`)

## Phase 1: Read the issue

```bash
gh issue view <n> -R <owner>/<repo> --json title,body,comments,labels,state
```

Use `--json`: plain `gh issue view` can print nothing in non-TTY shells. Read the comments too; they may already hold a diagnosis, a duplicate link, or a maintainer's decision. If the issue is closed or already has a linked PR, report that and stop.

## Phase 2: Triage

Reproduce the claim from the code, not from the issue's excerpt:

1. Open the cited file and lines on the current default branch; the issue may reference an older commit
2. Check the dependency's real API with the installed version, e.g. `uv run python -c "import inspect, pkg.mod as m; print(inspect.signature(m.fn))"`
3. Grep for existing tests that touch the same symbol. A test that looks contradictory (e.g. asserts a kwarg is *absent*) may cover a different code path; read it before concluding
4. Check `git log -S"<symbol>"` when behaviour looks intentionally removed, and the default branch's CHANGELOG `Unreleased` and recent commits for work on the same symptom: main may already fix it fully, partly, or with a different design. Graft onto main's design rather than adding a second mechanism
5. If the fix relies on a dependency feature, confirm the minimum pinned version provides it (list published versions from PyPI JSON; do not guess)

Pick one verdict:

| Verdict | Action |
|---|---|
| Confirmed bug | Continue to Phase 3 |
| Works as intended / not reproducible | Comment with evidence, stop |
| Duplicate | Comment with link, stop |
| Needs a product decision | Comment the options, ask the user, stop |
| The fix would depart from a spec the project implements | Comment the conflict with requirement IDs, file an issue on the spec repo if the user agrees, stop |
| A user's request for a new capability that no accepted opportunity covers (an **opportunity**) | Comment the hand-off to product intake (github-issue-triage's [comments.md](../github-issue-triage/references/comments.md), Opportunity), stop: intake groups it, and only the maintainer's accept makes it a feature |
| New behaviour beyond a bug fix or a contract tweak that the maintainer wants: an accepted opportunity, a request one covers, or an issue the maintainer filed (a **feature**) | Run github-issue-triage's [spec gate](../github-issue-triage/references/spec-gate.md): a spec PR from `specs.py new`, linked on the issue with a hold line, stop. Implement once it merges, with a test per acceptance criterion |
| A merged spec covers it (`specs.py find`, or the issue links one) | Read its criteria (`specs.py criteria NNN`) and continue to Phase 3; a departure from a criterion is a decision for the user |
| Confirmed, and the fix changes a contract (a flag, a format, a default, a public API, stored state) | Run github-issue-triage's [design gate](../github-issue-triage/references/design-gate.md): `decisions.py find`, the design in the issue comment, and the user's answer first when it departs from a `D-n` entry or the spec. Then Phase 3. A decision the user settled goes in its own docs PR, or an open one that already edits the log (design-gate.md, Recording a decision) |

## Phase 3: Prepare a clean branch in a worktree

This phase prevents shipping unrelated work in the PR, and keeps the fix off the checkout the session started in.

**Never switch, reset, or pull the user's checkout.** It may hold their uncommitted work or their own branch, and a later command run there (`shipmill plan`, a test run) reads whatever branch it is on. Leave its branch and files as you found them.

```bash
git fetch origin
git status -sb                      # note "ahead N" on the default branch
git log --oneline origin/<default>..<default>
```

- If the local default branch has unpushed commits, **do not branch from it**: those commits would silently ride along in the PR (and in a squash merge). Branch from the remote instead, and tell the user about the unpushed commits
- Never include untracked files the user didn't mention; stage by explicit path

Create the branch in a new worktree under the checkout and do Phases 4 to 6 there:

```bash
git worktree add -b fix/<short-slug> tmp/wt-<short-slug> origin/<default>
cd tmp/wt-<short-slug> && uv sync    # a fresh worktree has no virtualenv
```

Pick a path `git worktree list` doesn't show yet; `tmp/` must be ignored by git (add it to `.git/info/exclude` if the repo doesn't ignore it).

Skip the worktree when the session already runs inside a linked worktree, e.g. an implementer agent dispatched by github-issue-triage: there `git rev-parse --git-dir` differs from `git rev-parse --git-common-dir`, and `git switch -c fix/<short-slug> origin/<default>` in place is safe. In the main checkout the two print the same directory.

## Phase 4: Implement

1. Make the smallest change that fixes the root cause; match surrounding style
2. If a dependency floor must rise, update the manifest and lockfile (`uv lock`)
3. Add a regression test that fails before the fix and passes after, using the issue's repro shape (e.g. both the old-behaviour and new-behaviour cases)
4. Follow project and global rules (fail fast, no monkeypatching, `uv run`)

## Phase 5: Verify against a baseline

Run the suite, lint, and type checker. Scope pytest to the tests dir if stray folders (e.g. `tmp/`) break collection.

For any failures, prove whether they predate the change:

```bash
git stash -q && <run tests excluding the new test file>; git stash pop -q
```

Report pre-existing failures as such; never claim a green suite you didn't see. If the new test fails or the change introduces new failures or type errors, fix before continuing.

## Phase 6: Commit, push, PR, comment

1. Commit only the relevant paths. Message: imperative summary, a why-focused body, `Fixes #<n>`, plus any attribution lines the environment requires
2. `git push -u origin <branch>`
3. `gh pr create` with a Summary (what and why), `Fixes #<n>`, and a Test plan that lists pre-existing failures honestly
4. Before opening, check the PR diff contains only the intended files: `gh pr diff <pr> --name-only`
5. Comment the triage on the issue: root cause, why it was missed (if notable), and a link to the PR. Post with `--body-file`: in zsh, backticks inside a double-quoted `--body` run as commands and a glob error aborts the whole command line, so the comment silently never posts
6. Keep the worktree while the PR is open: a review round or a rebase reuses it. Tell the user its path

## Phase 7: Merge (only when explicitly asked)

This skill stops at an open PR. Merging goes through the `github-pr-triage` skill, so every PR lands under the same rules, whoever opened it. Invoke it with this PR and the user's scope (merge, or merge and tag). It covers:
- the CI gate (`pr_gate.py`: every job finished green)
- red-CI diagnosis and reruns
- rebasing onto a default branch that moved
- CHANGELOG placement after a rebase
- merge style and stacked PRs
- other sessions merging concurrently

Before handing over, re-confirm that `gh pr diff <pr> --name-only` shows only the intended files. After the merge, `git pull --ff-only` in the user's checkout only if they ask. It may hold their uncommitted work.

After the merge, remove the Phase 3 worktree and its local branch once github-pr-triage's `scripts/landed.py --onto origin/<default> <sha>...` exits 0 for the branch's commits (for a squash merge, compare trees as its [landing.md](../github-pr-triage/references/landing.md) says):

```bash
git worktree remove tmp/wt-<short-slug>
git branch -D fix/<short-slug>
```

Run these from the user's checkout without changing its branch. If the commits haven't landed, keep both and name the commits in the report.

A worktree this session leaves behind once it exits (it ended before the merge, or nobody asked for one) is the gate's prune's to remove (D-12): `shipmill gate` runs `shipmill worktrees --prune` on each tick, which removes a `tmp/wt-*` or `.claude/worktrees/` worktree and its branch only once its commits landed, it is clean and a day old, and no open PR or live session holds it. `shipmill worktrees` says why it keeps the rest.

## Report to the user

- Verdict and root cause in one or two sentences
- Links: issue comment, PR (and merge commit if merged)
- Verification results, separating new tests from pre-existing failures
- Anything left for the user: unpushed commits, divergent branches, decisions needed, and the worktree path while the PR is open
