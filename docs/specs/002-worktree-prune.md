# S-002: Prune landed worktrees

status: approved

## Problem

Every session shipmill's skills start can leave a git worktree and a local branch behind.
github-issue-resolve removes its own `tmp/wt-<slug>`, and github-issue-triage and
github-pr-triage remove the worktrees their own agents created once the PR merges; a
session that exits first leaves its worktrees with no owner, and a later pass won't touch
what it didn't create. `shipmill gate` starts sessions unattended, so worktrees under
`.claude/worktrees/` and `tmp/wt-*` pile up with nothing to collect them: this repo's
checkout held five from merged PRs (#112, #113, #115) plus github-pr-triage's `review-<n>`
worktrees. shipmill/shipmill#116 asks for a command that removes only what provably landed,
run by the gate on each tick, and a report of what stays behind too long.

## Behaviour

### The command

`shipmill [--repo <checkout>] worktrees [--prune [--dry-run]] [--json]`, a new subcommand in
`src/shipmill/cli.py` backed by a new module `src/shipmill/worktrees.py`. `--repo` is any
checkout of the repository (the main one or a linked worktree, such as the gate's
`tmp/shipmill-gate`); the command reads every worktree of the repository from
`git worktree list --porcelain`, through `src/shipmill/gitrepo.py`.

Before judging, it reads the default branch the way `gate.refresh` does
(`git ls-remote --symref origin HEAD`), fetches it, and compares against the fetched
`origin/<default>`. It then gives each worktree one verdict, REMOVABLE or KEPT, with the
reason for KEPT. A worktree is REMOVABLE only when every check below passes; the first check
that fails, in this order, is its reason:

| Check | Kept with reason |
|---|---|
| It is the main checkout | `main checkout` |
| It is the checkout `--repo` names | `current checkout` |
| Its path is `<W>/.claude/worktrees/<name>` or `<W>/tmp/wt-<name>`, where `<W>` is the main checkout or another worktree of the repository | `not a shipmill worktree` |
| Its directory exists (git doesn't list it `prunable`) | `directory missing` |
| It isn't locked (no `locked` line) | `locked: <git's lock reason>` |
| Its HEAD is on a branch | `detached HEAD` |
| Its branch isn't the default branch | `holds the default branch` |
| It was created 24 hours ago or earlier | `created <N>h ago` |
| No live Claude Code session's `cwd` is the worktree or inside it | `live session <name>` |
| `git status --porcelain` in it prints nothing (untracked files count) | `uncommitted changes` |
| Every commit on its branch landed on `origin/<default>` (below) | `<N> commit(s) not landed` |
| No open pull request has its branch as head | `open PR #<n>` |

Why only `.claude/worktrees/` and `tmp/wt-*`: those are the paths shipmill's skills and
Claude Code's agent isolation create. The gate's own dedicated checkout (`tmp/shipmill-gate`
in `skills/shipmill-setup/SKILL.md`, The gate; required detached and clean by
`gate.require_dedicated` in `src/shipmill/gate.py`) is a linked worktree that a looser rule
would judge on the remaining checks alone, and a user's own linked worktrees are theirs.

Age is the modification time of the worktree's `commondir` file under
`<git-common-dir>/worktrees/<id>/`, which git writes once when it creates the worktree. The
24-hour floor covers a session that just created a worktree and hasn't committed yet: its
branch has no commit outside the default branch, which the landed check alone would pass.

**Landed.** The branch's commits are `git rev-list --no-merges origin/<default>..<branch>`;
none means its tip is on the default branch. The rules are those of
`skills/github-pr-triage/scripts/landed.py`, reimplemented in `src/shipmill/worktrees.py`
(the package doesn't import skill scripts), with its window of the 500 newest non-merge
commits of `origin/<default>`. A commit landed when:

- It is an ancestor of `origin/<default>`, or
- Its `git patch-id --stable` matches a commit in the window, or
- Its patch id without `CHANGELOG.md` matches one in the window computed the same way (a
  commit that changes only `CHANGELOG.md` has no such id and counts as not landed)

When some commit fails those, the branch still landed as a squash merge if the patch id of
its whole diff (`git diff <merge-base> <tip>`) matches one commit in the window, exactly or
without `CHANGELOG.md`. landed.py matches commit by commit, so a multi-commit PR squashed
into one reads NOT LANDED there; this rule closes that gap without trusting the PR state.

**Live sessions.** The command runs `claude agents --json` (no `--cwd`, which filters to
background sessions only) and reads every row's `cwd`. On this machine it prints a JSON
array of the active sessions, interactive and background, each with `pid`, `cwd`, `kind`,
`name`, `status`, `sessionId`, `startedAt`, and for background sessions `id` and `state`. A
subagent running in an isolated worktree has no row of its own (its parent's `cwd` is where
the parent started); Claude Code locks such a worktree (`locked claude agent <name> (pid
...)`) while the agent runs, which the lock check covers. If `claude` isn't on `PATH`, exits
non-zero, prints something other than a JSON array, or prints a row without a string
`cwd`, the command exits 2 with a message naming the problem and removes nothing.

**Open pull requests.** One `gh pr list --state open --json number,headRefName` call
through `src/shipmill/github.py`; a pull request matches when its head is the worktree's
branch name or that branch's upstream branch on origin. A gh failure exits 2 and removes
nothing.

**Output.** A table, one row per worktree, with the columns `verdict`, `path` (relative to
the main checkout), `branch` (`-` when detached), `age` (whole hours under two days, else
days), and `reason`. `--json` prints one object instead:
`{"worktrees": [{"path", "branch", "head", "verdict", "reason", "created", "age_hours"}]}`,
with `created` in ISO 8601 with offset, `branch` and `reason` null when they don't apply.

**Prune.** `--prune` removes each REMOVABLE worktree with `git worktree remove <path>`
(never `--force`, so git refuses anything that changed since the check), then deletes the
local branch it held with `git branch -D` (the landed check already proved its commits on
the default branch; `-d` would refuse a squash-merged branch). Those rows print with the
verdict REMOVED; KEPT rows print unchanged. No other branch is deleted. `--prune --dry-run`
runs every check and prints the REMOVABLE rows as WOULD_REMOVE, removing and deleting
nothing; `--dry-run` without `--prune` is refused with exit 2. If a removal or branch
deletion fails, the prune stops there and exits 2 naming the worktree and git's error;
what it removed before stays removed.

**Exit codes**, as the other commands: 0 done, whether or not anything was kept or removed;
2 bad arguments, a refused state, or a failing `git`, `gh`, or `claude` (raised as
`ReleaseError`, printed by `cli.run`).

### The gate

`gate()` in `src/shipmill/gate.py` prunes on each tick with the same code, after
`check_checkout` and before the hold check, so it prunes while a `shipmill-hold` issue is
open: D-11 stops the gate from starting sessions, and a prune starts none and removes only
landed, clean work. `shipmill gate --dry-run` runs the prune as `--prune --dry-run` does.
The gate's text output adds a `pruned <path> (<branch>)` line per removed worktree (`would
prune` under `--dry-run`); its `--json` record gains `"pruned": [<path>, ...]`, the paths
removed or, under `--dry-run`, the ones it would remove. A prune error exits 2 like any
other gate error, and the gate starts no session on that tick.

### The report

`skills/github-ship-watch/scripts/watch_state.py`, for a repo whose release workflow is
shipmill's (where it already runs `uvx --from <tool> shipmill plan`), runs
`uvx --from <tool> shipmill --repo <repo-dir> worktrees --json` and adds one
`WORKTREE_STALE` row per worktree whose verdict is KEPT, whose path is a shipmill worktree
(the reasons `main checkout`, `current checkout`, and `not a shipmill worktree` never
report), and whose age is over 7 days: the subject is its path, the detail its reason. The
row is report-only: not in `ACTION`, not in `AGENT`, so it never starts a gate session. The
7 days are a constant in the script. A failing `shipmill worktrees` fails the watch run as
a failing plan does.

### Docs

- `skills/github-ship-watch/SKILL.md`: the `WORKTREE_STALE` row and what a person does with
  it (finish, push, or remove the work by hand)
- `skills/shipmill-setup/SKILL.md`, The gate: the gate prunes landed worktrees on each tick,
  and `shipmill worktrees` shows why the rest are kept
- `skills/github-issue-resolve/SKILL.md`, `skills/github-pr-triage/SKILL.md`, and
  `skills/github-issue-triage/SKILL.md` keep their own cleanup (D-12); each says that what a
  session leaves behind once it exits is the gate's prune's to remove

## Acceptance criteria

- S-002-1: `shipmill worktrees` prints one table row per worktree of the repository with its verdict, path relative to the main checkout, branch, age, and the reason for each KEPT row, exits 0, and removes nothing
- S-002-2: `shipmill worktrees --json` prints one JSON object whose `worktrees` list holds each worktree's path, branch, head, verdict, reason, created time, and age in hours
- S-002-3: the main checkout is KEPT as `main checkout`, the checkout `--repo` names as `current checkout`, and a linked worktree outside `.claude/worktrees/` and `tmp/wt-*` (such as a detached, clean `tmp/shipmill-gate` whose every other check passes) as `not a shipmill worktree`
- S-002-4: a candidate worktree whose directory is missing, that is locked, that is on a detached HEAD, or that holds the default branch is KEPT with `directory missing`, `locked: <reason>`, `detached HEAD`, or `holds the default branch`
- S-002-5: a candidate worktree created less than 24 hours ago, by its `commondir` file's modification time, is KEPT as `created <N>h ago`
- S-002-6: a candidate worktree whose path is, or contains, the `cwd` of a session `claude agents --json` lists is KEPT as `live session <name>`
- S-002-7: a candidate worktree with a modified, staged, or untracked file is KEPT as `uncommitted changes`
- S-002-8: a candidate worktree whose branch has a commit not on `origin/<default>` by ancestry, by patch id, or by patch id without `CHANGELOG.md`, and whose whole diff matches no default-branch commit either, is KEPT as `<N> commit(s) not landed`; a commit that changes only `CHANGELOG.md` counts as not landed
- S-002-9: a candidate worktree whose branch was rebase-merged, merged with a merge commit, or squash-merged from several commits (with or without a hand-resolved `CHANGELOG.md`), and that passes every other check, is REMOVABLE
- S-002-10: a candidate worktree whose branch, or its upstream branch, is the head of an open pull request is KEPT as `open PR #<n>`
- S-002-11: when `claude` is missing, `claude agents --json` exits non-zero, prints anything but a JSON array, or prints a row without a string `cwd`, `shipmill worktrees` (with or without `--prune`) exits 2 with a message naming the problem and removes no worktree and no branch; a failing `gh pr list` does the same
- S-002-12: `shipmill worktrees --prune` removes every REMOVABLE worktree and the local branch it held, prints them as REMOVED, leaves every KEPT worktree, every other local branch (including landed branches no worktree holds), and every remote branch in place, and exits 0
- S-002-13: `shipmill worktrees --prune --dry-run` prints the REMOVABLE worktrees as WOULD_REMOVE and removes no worktree and no branch; `--dry-run` without `--prune` exits 2
- S-002-14: when `git worktree remove` or the branch deletion fails for a worktree, `--prune` exits 2 naming that worktree and git's error, and removes no worktree after it
- S-002-15: `shipmill gate` prunes on each tick, including a tick that reports HELD while a `shipmill-hold` issue is open, prints a `pruned <path> (<branch>)` line per removed worktree, and lists them under `pruned` in `--json`
- S-002-16: `shipmill gate --dry-run` removes no worktree and lists the worktrees it would prune; a prune error makes `shipmill gate` exit 2 without launching a session
- S-002-17: `watch_state.py` reports one report-only `WORKTREE_STALE` row (agent false, not an action) per KEPT shipmill worktree older than 7 days, naming its path and reason, and none for the main checkout, the current checkout, or a worktree that is not a shipmill worktree
- S-002-18: `skills/github-ship-watch/SKILL.md` documents the `WORKTREE_STALE` row, and `skills/shipmill-setup/SKILL.md`'s gate section documents the gate's prune and `shipmill worktrees`, and the github-issue-resolve, github-pr-triage, and github-issue-triage skills each say that the worktrees a session leaves behind once it exits are the gate's prune's to remove

## Out of scope

- Local branches no worktree holds, landed or not: the prune deletes only the branch a pruned worktree held
- Remote branches of merged pull requests: shipmill/shipmill#117
- A config key for the 7-day report threshold or the 24-hour floor: both are constants; a key in `.github/shipmill.toml` (D-4) is a later change if a repo needs another value
- Worktrees whose directory is missing: reported, left to `git worktree prune` by hand
- Making github-issue-resolve lock its `tmp/wt-*` worktree while its session runs: the 24-hour floor and the `cwd` check cover it for now
- Pruning across a fleet of repos or checkouts other than the one `--repo` names

## Decisions relied on

- D-11
- D-12

## Issues

## Verification
