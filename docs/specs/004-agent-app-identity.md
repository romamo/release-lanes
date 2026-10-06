# S-004: Gated sessions write as a GitHub App

status: approved

## Problem

A session `shipmill gate` starts uses `gh` and `git` as they're signed in on the host:
the maintainer's account. Every pull request, comment, label, review, and merge an agent
makes is authored by the maintainer, and its commits carry the maintainer's git identity.
That causes three problems:

- Nobody can tell agent work from the maintainer's own: not on GitHub, not in
  github-ship-watch's metrics, which count a merge made with a person's token as that
  person (`skills/github-ship-watch/SKILL.md`, Human touch)
- The maintainer can't approve an agent's pull request, since GitHub refuses a review from
  its author, so a ruleset that requires an approval can't gate agent work
- The session holds the maintainer's full token: every repo and scope the maintainer has,
  not just the repo it works on

The maintainer asked for agent pull requests to be authored by a GitHub App.

## Behaviour

### What Claude Code provides

Verified on Claude Code 2.1.289: `claude --bg --settings '<json>'` applies the JSON's
`env` block to the background session, and its Bash tool sees those variables, `PATH`
included (`printenv` in a session launched with `{"env": {"PATH": "/opt/probe-bin:..."}}`
printed `/opt/probe-bin` first). The settings JSON is a process argument that `ps` can
show, so the gate never puts a token in it. It puts a `PATH` there whose `gh` mints the
token at call time.

### Config

One new key in the `[agents]` section of `.github/shipmill.toml` (D-4), parsed by
`AgentsConfig` in `src/shipmill/agents.py`:

```toml
[agents]
app_id = 123456   # sessions write as this GitHub App; unset: as the host's gh login
```

| Key | Type | Default | Allowed |
|---|---|---|---|
| `app_id` | integer | unset | 1 or more |

The App's identity is a committed decision, like the prompt. The private key is a host
secret and never goes in the repo: `shipmill gate --app-key <path>` names it, defaulting to
`~/.config/shipmill/app-<app_id>.pem`. `shipmill launchd --app-key <path>` passes it into
the job's arguments. A key option given while `app_id` is unset exits 2.

### Creating the App

shipmill doesn't create the App; the maintainer does, once, in GitHub's settings. The
setup docs give the steps: no webhook, these repository permissions, installed only on the
repos it works on, a private key downloaded to the key path with mode `0600`.

| Permission | Access | Why |
|---|---|---|
| Contents | write | push branches, merge |
| Pull requests | write | open, review, and merge pull requests |
| Issues | write | comments, labels, closing |
| Actions | write | rerun a failed release job (github-ship-watch) |
| Workflows | write | push a change under `.github/workflows/` |
| Checks | read | CI state |
| Commit statuses | read | CI state |
| Discussions | read | product-intake's input |
| Metadata | read | required by GitHub |

### At launch

When `app_id` is set and the tick decides LAUNCH, the gate, before stopping or starting
any session:

1. Reads the key file. A missing key exits 2 naming its path, and so does a key readable
   by group or others (the error says to `chmod 600` it). The key's contents never appear
   in output
2. Signs an App JWT (RS256, issued 60 seconds in the past, valid 9 minutes) with
   `openssl dgst -sha256 -sign <key>`, so the package keeps no dependencies. A missing
   `openssl` exits 2 naming it
3. Reads `GET /app` for the App's slug and `GET /repos/<owner>/<repo>/installation` for the
   installation and its permissions. An App not installed on the repo exits 2 with
   `app <slug> is not installed on <owner/repo>`. An installation missing any permission
   in the table exits 2 naming each one with its access
4. Reads `GET /users/<slug>[bot]` for the bot account's id, which makes the commit email
   `<id>+<slug>[bot]@users.noreply.github.com`
5. Writes the session's helper scripts (below) and launches with
   `--settings '{"env": {...}}'` holding:
   - `PATH`: the helpers' folder, then the gate's own `PATH`
   - `GIT_AUTHOR_NAME`, `GIT_COMMITTER_NAME`: `<slug>[bot]`
   - `GIT_AUTHOR_EMAIL`, `GIT_COMMITTER_EMAIL`: the bot's noreply email
   - `GIT_CONFIG_COUNT`, `GIT_CONFIG_KEY_n`, `GIT_CONFIG_VALUE_n`: an empty
     `credential.https://github.com.helper` (dropping the host's helpers), then the gate's
     helper, then `url.https://github.com/.insteadOf` for `git@github.com:` and
     `ssh://git@github.com/`, so a push over SSH goes through the App too

A failure at any step exits 2, stops no session, starts none, and doesn't fall back to the
host's identity (D-14). When `app_id` is unset, the gate launches as it does today, with no
`--settings` env.

Only sessions change identity. The gate's own reads (`watch_state.py`, the hold check,
`claude agents`) keep the host's `gh` login.

### Tokens

`shipmill app-token <owner/repo> --app-id <id> --app-key <path>` prints an installation
token for that repo. The token is limited to that one repository (`repositories: [<name>]`
in `POST /app/installations/<id>/access_tokens`) and has the table's permissions.
It caches the token in `$(git rev-parse --git-common-dir)/shipmill/app-token.json`,
written with mode `0600`, and mints a new one when the cached token has under 10 minutes
left. Installation tokens last an hour and a session can run longer, which is why the
session never gets a token directly: each call mints or reuses one through the helpers.
`--git-credential get` prints `username=x-access-token` and `password=<token>` lines in
git's credential format; `store` and `erase` print nothing and exit 0. A malformed cache
exits 2 naming its path and saying to delete it.

The gate writes two helpers, mode `0700`, to `$(git rev-parse --git-common-dir)/shipmill/bin/`
before each launch:

- `gh`: sets `GH_TOKEN` from `shipmill app-token ...` and runs the `gh` the gate resolved on
  its own `PATH` with the same arguments. When minting fails, it prints the error and exits
  non-zero without running `gh`
- `git-credential-shipmill`: runs `shipmill app-token ... --git-credential <op>`

Both helpers run the same shipmill that launched them (`<sys.executable> -m shipmill`,
through a new `src/shipmill/__main__.py`) with the App id, key path, repo, and checkout as
arguments. They hold no token.

### Output

The decision line of a launch gains ` as <slug>[bot]` when `app_id` is set. `--json` gains
`"identity"`: the bot login, or `null` for the host's `gh` login. `--dry-run` with `app_id`
set runs steps 1 to 4 (so it proves the key, the installation, and the permissions), writes
no helpers and no token cache, and prints `would launch as <slug>[bot]`.

### Docs

- `skills/shipmill-setup/SKILL.md`, The gate: creating the App with the permission table,
  installing it, the key path and mode, `app_id`, and `--app-key` on `shipmill launchd`
- `docs/install.md`: `app_id` in its `[agents]` block and the key path
- `docs/design/agent-modes.md`: State (`app-token.json`, `bin/`) and What Claude Code
  provides (`--settings` env reaches a background session)
- `skills/github-ship-watch/SKILL.md`, Human touch: an App's `<slug>[bot]` counts as a bot
  without `--bot`, so with `app_id` set the metrics tell agent work from the maintainer's

## Acceptance criteria

- S-004-1: `AgentsConfig` reads `app_id` as unset when `[agents]` omits it and as the given integer when present, and refuses a non-integer or a value below 1 with exit 2 naming the key
- S-004-2: `shipmill gate --app-key <path>` with `app_id` unset exits 2; with `app_id` set and no `--app-key`, the gate reads `~/.config/shipmill/app-<app_id>.pem`
- S-004-3: with `app_id` set, a missing key file, or one readable by group or others, exits 2 naming the path, launches no session, and stops none
- S-004-4: the App JWT is signed through `openssl dgst -sha256 -sign <key>` with `iat` 60 seconds before now and `exp` 9 minutes after it, and a missing `openssl` exits 2 naming it
- S-004-5: an App not installed on the repo exits 2 with `app <slug> is not installed on <owner/repo>`, and an installation missing a permission from the table exits 2 naming each missing one with its access; neither launches or stops a session
- S-004-6: on LAUNCH with `app_id` set, `claude --bg` gets `--settings` whose `env` puts the helpers' folder first on `PATH`, sets the git author and committer to `<slug>[bot]` and `<id>+<slug>[bot]@users.noreply.github.com`, and sets the credential helper and the SSH-to-HTTPS rewrites through `GIT_CONFIG_*`; no token appears in the launch's arguments
- S-004-7: with `app_id` unset, the gate's `claude --bg` command line is the one it runs today, and it mints no token and writes no helpers
- S-004-8: `shipmill app-token` asks GitHub for a token limited to the one repository, caches it in `app-token.json` with mode `0600`, reuses it while it has at least 10 minutes left, and mints a new one after that; a malformed cache exits 2 naming its path
- S-004-9: `shipmill app-token --git-credential get` prints `username=x-access-token` and `password=<token>`, and `store` and `erase` print nothing and exit 0
- S-004-10: the `gh` helper runs the resolved `gh` with its arguments and `GH_TOKEN` set to the minted token, and when minting fails it exits non-zero without running `gh`; neither helper's file contains a token
- S-004-11: a launch with `app_id` set prints ` as <slug>[bot]` on the decision line, `--json` reports `identity` as the bot login (or `null` with `app_id` unset), and `--dry-run` checks the key, installation, and permissions, writes no helpers or cache, and prints `would launch as <slug>[bot]`
- S-004-12: the gate's own GitHub reads (the findings and the hold check) run without the App's token whether `app_id` is set or not
- S-004-13: `shipmill launchd --app-key <path>` puts `--app-key <path>` in the job's `shipmill gate` arguments
- S-004-14: `skills/shipmill-setup/SKILL.md`, `docs/install.md`, `docs/design/agent-modes.md`, and `skills/github-ship-watch/SKILL.md` document the App's creation and permissions, `app_id`, the key path and mode, the token cache and helpers, and that the App's bot counts as a bot in the metrics

## Out of scope

- Creating the App for the maintainer (a manifest flow): it needs a browser, and it happens once per App
- Running the gate's own reads, or the release workflows, as the App: the workflows already run as `github-actions[bot]`, and the gate's reads write nothing
- Signed (verified) commits: git can't sign as the App; committing through the API could, as a later spec
- Sessions that work across repos (github-ship-watch's fleet mode) under the App: the token is limited to the gate's repo, so such a session fails on the others rather than reaching them
- Rulesets that require an approval on agent pull requests: the maintainer can now add one, but shipmill-setup doesn't set it up
- A Linux keychain or macOS Keychain for the private key: a file with mode `0600`, as ssh keys use

## Decisions relied on

- D-4
- D-14

## Issues

## Verification
