# S-012: Every agent session writes to GitHub as the App

status: approved

## Problem

Spec 004 made sessions that `shipmill gate` starts write as the repo's GitHub App. A session
a person starts by hand still writes with that person's `gh` login, so the issues, comments,
labels, and pull requests its skills post show the maintainer as author. On 2026-10-07 an
interactive session filed #231 to #247 that way, and the audit trail can no longer tell a
human decision from agent text, which is what D-14 and spec 004 set out to prevent
(#250).

The usual workaround fails the wrong way. `GH_TOKEN=$(shipmill app-token ...) gh ...`
outside a checkout gets an empty token, because `app-token` caches under
`git rev-parse --git-common-dir` and exits 2 with no checkout, and `gh` with an empty
`GH_TOKEN` falls back to the person's login without a word. #250 itself was filed that way.

## Behaviour

### `shipmill app-token` with no checkout

`shipmill app-token <owner/repo> --app-id <id>` works outside a git checkout. In a checkout
the token cache stays where spec 004 put it. With no checkout it goes to
`$XDG_CACHE_HOME/shipmill/<owner>/<repo>/app-token.json` (`~/.cache` when `XDG_CACHE_HOME`
is unset), same mode `0600`, same reuse rule. Every other failure still exits 2 (D-14).

### `shipmill gh`

A new command in `src/shipmill/cli.py` runs `gh` with the App's identity when the repo has
one:

```bash
shipmill gh issue comment 250 --body-file tmp/comment.md
shipmill gh pr create -R owner/repo --title ... --body-file ...
```

Everything after `gh` goes to `gh` unchanged. The command:

1. Reads `[agents] app_id` from `.github/shipmill.toml` in the checkout (`--repo`, default
   the current folder). The token's repo is the `-R`/`--repo` value in the `gh` arguments
   when given, else `origin`'s `owner/name`
2. With `app_id` set: mints or reuses the token as `shipmill app-token` does (key from
   `--app-key`, default `~/.config/shipmill/app-<app_id>.pem`), then runs the `gh` on `PATH`
   with `GH_TOKEN` set to it and exits with `gh`'s code. Any failure to get the token (no
   key, a key others can read, the App not installed on that repo, a missing permission,
   GitHub's error) exits 2 naming the cause and its fix, and never runs `gh`
3. With `app_id` unset, or no `.github/shipmill.toml`: runs `gh` as it is, with the host's
   login, and prints nothing extra. That is the repo's chosen identity, not a fallback
4. Never runs `gh` with an empty `GH_TOKEN`: one set but empty in its own environment is
   removed before `gh` runs when `app_id` is unset, and replaced when it is set

Inside a gate session the `gh` on `PATH` is already spec 004's helper; `shipmill gh` there
mints the same token and changes nothing.

### Skills

The six skills that write to GitHub (`skills/github-issue-triage/SKILL.md`,
`skills/github-issue-resolve/SKILL.md`, `skills/github-pr-triage/SKILL.md`,
`skills/github-ship-watch/SKILL.md`, `skills/product-intake/SKILL.md`,
`skills/shipmill-setup/SKILL.md`) say, in the same short GitHub section: when the repo's
config sets `[agents] app_id`, every `gh` call that writes (an issue, a comment, a label, a
pull request, a review, a merge, a release) runs as `shipmill gh ...`, in every session, gated
or not. Reads may keep plain `gh`. A `shipmill gh` that exits 2 is a stop: the session never
retries the write with plain `gh`.

### Relayed decisions

When a person answers a question in the session (a needs-decision answer, a "decision for
you" on a PR) and the session posts that answer on GitHub as the App, the comment's first
line is `Decision by @<login>, relayed by <agent>`, where `<agent>` names the tool (`Claude
Code`). `skills/github-issue-triage/references/needs-decision.md` and
`skills/github-issue-triage/references/comments.md` carry the line in their templates.

### `status` and `doctor`

With `app_id` set, `shipmill status` and `shipmill doctor` read the issues, pull requests, and
issue comments of the last 7 days and flag the ones that carry an agent's marker but were
written by a person (a `User`, not a `Bot`). The markers: a body that contains
`Generated with [Claude Code]`, or one whose first line starts with `Triage:` or with the
needs-decision marker. `status` adds a WARN row `AGENT_AS_PERSON` and `doctor` a `WARN` check
of the same name, each naming the count, up to three URLs, and the fix: post through
`shipmill gh` (this spec). A comment whose first line is `Decision by @...` isn't flagged
when its author is the App. With `app_id` unset neither reads anything for this.

### Docs

- `docs/install.md` and `skills/shipmill-setup/SKILL.md`, The gate: `shipmill gh`, the
  cache folder with no checkout, and that the App's identity covers interactive sessions too
- `CHANGELOG.md`: one entry per build PR

## Acceptance criteria

- S-012-1: `shipmill app-token <owner/repo> --app-id <id>` run outside any git checkout prints a token and caches it in `$XDG_CACHE_HOME/shipmill/<owner>/<repo>/app-token.json` (or under `~/.cache` with `XDG_CACHE_HOME` unset) with mode `0600`; inside a checkout the cache path is spec 004's
- S-012-2: with `app_id` set, `shipmill gh <args>` runs the `gh` on `PATH` with exactly `<args>` and `GH_TOKEN` set to a token limited to the `-R`/`--repo` repo when the arguments name one, else to `origin`'s repo, and exits with `gh`'s exit code
- S-012-3: with `app_id` set, a missing key, a key readable by group or others, an App not installed on the repo, or a missing permission makes `shipmill gh` exit 2 naming the cause and its fix, and `gh` is never run
- S-012-4: with `app_id` unset or no `.github/shipmill.toml`, `shipmill gh <args>` runs `gh` with `<args>` and no `GH_TOKEN`, even when its own environment had `GH_TOKEN` set to an empty string, and mints no token
- S-012-5: each of the six skills' `SKILL.md` says that with `[agents] app_id` set every `gh` call that writes runs as `shipmill gh`, in every session, and that a `shipmill gh` exit 2 is never retried with plain `gh`
- S-012-6: `needs-decision.md` and `comments.md` give a relayed answer's first line as `Decision by @<login>, relayed by <agent>`
- S-012-7: with `app_id` set, `shipmill status` prints a WARN row `AGENT_AS_PERSON` and `shipmill doctor` a `WARN AGENT_AS_PERSON` check when an issue, pull request, or issue comment of the last 7 days carries an agent marker and has a `User` author, each naming the count, at most three URLs, and the fix; one written by a `Bot`, or older than 7 days, isn't counted
- S-012-8: with `app_id` unset, `status` and `doctor` make no request for the `AGENT_AS_PERSON` check and print no such row
- S-012-9: `docs/install.md` and `skills/shipmill-setup/SKILL.md` document `shipmill gh`, the no-checkout cache folder, and that the App's identity covers sessions a person starts

## Out of scope

- Putting a `gh` helper on an interactive session's `PATH` automatically (a plugin hook): the skills call `shipmill gh` instead, which works in any agent tool; a hook can come later as its own spec
- Commits and pushes from an interactive session: they keep the person's git identity; spec 004's git env stays gate-only
- Rewriting the author of what was already posted: GitHub can't, and `AGENT_AS_PERSON` stops counting it after 7 days
- The skills' own read-only scripts (`triage_state.py` and the rest): reads keep the host's login, as spec 004's gate reads do

## Decisions relied on

- D-4
- D-14

## Issues

- shipmill/shipmill#262: S-012-1, S-012-2, S-012-3, S-012-4
- shipmill/shipmill#263: S-012-5, S-012-6, S-012-9
- shipmill/shipmill#264: S-012-7, S-012-8

## Verification
