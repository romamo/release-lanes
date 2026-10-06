# Design: agent modes

Status: mode 1 built (`shipmill gate`, `shipmill launchd`), 2026-10-04

shipmill is stateless code. It decides whether a repo needs an agent, and when it does, it
starts a session in a **persistent Claude Code install**: Claude Code on your Mac, or on an
always-on machine set up the same way, with its memory, transcripts, global `CLAUDE.md`,
skills, plugins, the clones of all your repos, and its peer sessions. The agent judges; the
code decides when it runs and with what.

## Modes

| Mode | Session | Questions for you | Status |
|---|---|---|---|
| 1. Interactive | A new `claude --bg` session per launch: attachable, listed in `claude agents` | AskUserQuestion; the session waits, the gate notifies you and starts nothing for that repo until you answer or `max_wait_minutes` (15 by default) stops it | Built: `shipmill gate` |
| 2. Noninteractive | The same, started with a tool allowlist, for a machine nobody watches | The `needs-decision` protocol: a label and a comment that mentions you; the item waits on GitHub and the session ends | Next, after mode 1 shows how often sessions wait on you |
| 3. Ephemeral | A fresh container per job (GitHub Actions) | The same protocol | Parked: [ephemeral-mode.md](ephemeral-mode.md) |

Mode 1 replaces `/loop`, whose iterations all run in one growing session. Each launch is a
fresh session; memory, `CLAUDE.md`, and the GitHub threads carry the history, and older
transcripts stay searchable.

## The gate

The repo decides what the session does, in the `[agents]` section of
`.github/shipmill.toml` (D-4); the host decides where and how often it runs:

```toml
[agents]
prompt = "/github-issue-triage {repo} merge when green"
prs = true          # open pull requests count as work
retry_hours = 24    # unchanged findings start a new session after this
notify = true       # a desktop notification when a session waits on you
remind_hours = 4    # repeat it while the session still waits (1..168)
max_wait_minutes = 15  # stop a session that waited this long (0..10080); 0: never
# app_id = 123456      # sessions write as this GitHub App; unset: as the host's gh login
```

```
shipmill --repo ~/PycharmProjects/treaty/tmp/shipmill-gate gate romamo/treaty --refresh
shipmill --repo ~/PycharmProjects/treaty/tmp/shipmill-gate launchd romamo/treaty --every 15
```

The gate runs in a dedicated checkout: a detached worktree inside the trusted repo, never
the user's working copy, where the session would branch and commit. Each run:

1. Checks that the checkout's `origin` is the repo
2. Lists this repo's gate sessions with `claude agents --json`. For each one whose state
   is `blocked` (it waits on you), reads `[agents]` from the checkout as it stands and
   times the wait in `waiting.json`. With `notify = true` it sends a desktop notification
   (`osascript` on macOS, `notify-send` elsewhere) at once and every `remind_hours` while
   the session waits; a failed send is printed and tried again next tick. A session that
   has waited `max_wait_minutes` (15 by default; 0 never stops) is stopped with `claude stop <id>`, its entry
   dropped, and a last notification sent; a failing `claude stop` exits 2 and starts
   nothing. The gate writes nothing to GitHub for it. The rest of the tick runs without
   the stopped sessions, so it can launch, under the usual rules
3. Lists the open issues labelled `shipmill-hold`. Any: **HELD**, stop (D-15). A session
   already running finishes; the reason names it with `claude stop <id>`. Step 2 runs on
   a held tick too, so a session that reached `max_wait_minutes` is stopped, held or not
4. A session still `blocked` waits on you: **WAITING**, stop. One that is `working` or
   busy: **RUNNING**, stop. Both stop before the state is read or the checkout moves
5. With `--refresh`, moves the detached, clean checkout to the head of origin's default
   branch, so the session reads the current config, `CLAUDE.md`, and skills
6. Reads `[agents]`, then the state with github-ship-watch's `watch_state.py` (bundled in
   the wheel, so the gate and the script always come from the same version). The rows
   that need an agent are the ones it marks `agent: true` (a failed or stalled release
   bot, a release missing from PyPI or not announced, issues triage owes, a failed operate
   run, an open incident), plus open PRs when `prs = true`. A promotion waiting on a
   person, an unhealthy environment (operate's rollback owns it), and a postmortem due
   start nothing. None: **QUIET**, stop. No model call has happened
7. Compares the rows with the last launch's fingerprint. The same rows within
   `retry_hours`: **UNCHANGED**, stop. A session that left an item alone on purpose
   doesn't wake a new one every tick
8. **LAUNCH**: stops this repo's finished sessions (`claude stop` keeps their
   conversation), starts `claude --bg -n "shipmill <repo> <time>" "<prompt>"` in the
   checkout with the rows appended to the prompt, each by state and subject only (an
   issue title in a row's detail is text anyone who edits the issue controls; the session
   reruns watch_state.py and reads it as data, not instructions), and records the launch.
   With `app_id` set, the App's checks and the session's helpers come first (below)

### The session's identity

Without `app_id`, a session uses `gh` and `git` as they're signed in on the host: the
maintainer's account, with every repo and scope it has. Its pull requests, merges, and
commits read as the maintainer's, who then can't approve them. With `app_id`, the session
writes as a GitHub App's bot (D-14). The maintainer creates the App once (no webhook, the
permissions in shipmill-setup's table, installed only on the repos it works on) and keeps
its private key on the host, mode `0600`, at `~/.config/shipmill/app-<app_id>.pem` or the
path `--app-key` names. The key is a host secret; `app_id` is a committed decision, like
the prompt.

On LAUNCH, before stopping or starting any session, the gate checks the key's mode, signs
an App JWT with `openssl dgst -sha256 -sign` (so the package keeps no dependencies), reads
the App's slug, its installation on the repo and the installation's permissions, and the
bot account's id. It then writes two helpers, mode `0700`, to the state folder's `bin/`: a
`gh` that sets `GH_TOKEN` from `shipmill app-token` and runs the host's `gh`, and
`git-credential-shipmill`, which answers git's credential protocol the same way. The
session gets them through `--settings` env: `bin/` first on `PATH`, `<slug>[bot]` and
`<id>+<slug>[bot]@users.noreply.github.com` as git author and committer, and, through
`GIT_CONFIG_*`, the App's credential helper in place of the host's for github.com, with
`git@github.com:` and `ssh://git@github.com/` remotes sent over https. The env holds no
token: the settings JSON is a process argument `ps` can show, and an installation token
lasts an hour while a session can run longer, so each call mints or reuses one. A failure
at any step exits 2, stops no session, starts none, and never falls back to the host's
login. The gate's own reads (`watch_state.py`, the hold check, `claude agents`) keep the
host's `gh` login.

`--dry-run` with `app_id` set runs the checks on a tick that would launch, writes no
helpers and no token cache, and prints `would launch as <slug>[bot]`. The decision line of
a launch ends ` as <slug>[bot]`, and `--json` reports `identity`: the bot login, or `null`
for the host's `gh` login. `shipmill launchd --app-key <path>` puts the key, made
absolute, in the job's gate arguments.

`--dry-run` prints the decision and touches nothing: it sends no notification, stops no
session, and writes no file, and says `would notify <id>` or `would stop <id>` instead.
`--json` lists each blocked session under `waiting`. `--claude-arg` passes flags to the
session, such as `--permission-mode`; it is the host's choice, so it stays a flag.

`shipmill launchd` writes `~/Library/LaunchAgents/dev.shipmill.gate.<owner>.<repo>.plist`
with `StartInterval`, `RunAtLoad`, and a log under `~/Library/Logs/shipmill/`. launchd gives
jobs a minimal PATH, so the job carries one built from where claude, gh, git, and uvx live,
skipping temporary folders: cmux, for one, puts a `claude` shim in one that vanishes when
the app restarts. Install refuses a checkout that isn't dedicated or has no `[agents]`
section, so a job never fails the same way on every tick.

### What Claude Code provides

Verified on Claude Code 2.1:

- `claude --bg` starts a session in the background and prints its id; `claude attach
  <id>` opens it, `logs` shows its output, `stop` ends it and keeps the conversation
- `claude agents --json` lists live sessions with `status` (`busy`, `idle`, `waiting`) and, for
  background ones, `state` (`working`, `blocked`, `done`, `failed`). A background session
  stays alive and `idle` with state `done` after its prompt finishes, so the gate stops it
  before the next launch
- A blocked session shows no question, no time it blocked (`startedAt` is when the
  session started), and no issue or pull request it works on. `claude logs <id>` is raw
  terminal output and the session's text, so the gate never reads or forwards it. The
  gate times a wait itself from the first tick that saw the session blocked, and what it
  reports is the session's id and name, how long it waited, and `claude attach <id>`,
  which shows the question
- Auto memory is per repository and shared by its worktrees and subdirectories; the
  session works in its own worktree and still sees the whole history. Transcripts are per
  working directory
- A session needs the checkout to be trusted: run `claude` there once and accept
- `claude --bg --settings '<json>'` applies the JSON's `env` block to the background
  session, and its Bash tool sees those variables, `PATH` included (verified on 2.1.289).
  The JSON is a process argument that `ps` can show, so the gate puts a `PATH` there whose
  `gh` mints a token at call time, never a token

### State

The gate keeps its state in `$(git rev-parse --git-common-dir)/shipmill/`, never committed
and shared by every worktree:

- `gate.json`: the last launch's fingerprint, session id, and time. Losing it costs at
  most one extra session; it never changes what is decided
- `waiting.json`: one entry per blocked gate session, `{"<id>": {"since": ..., "notified":
  ...}}`, where `since` is the first tick that saw it blocked and `notified` the last
  notification sent (`null` while none was). An entry is dropped once its session is no
  longer blocked, so a session that blocks again starts a new wait. Losing the file
  restarts every wait: at most one extra notification and a later stop
- `app-token.json`, with `app_id` set: the App's installation token, limited to the one
  repo, its expiry, and which App and repo it is for, mode `0600` and replaced atomically.
  `shipmill app-token` reuses it while it has at least 10 minutes left and mints a new one
  after that. Losing it costs one more token
- `bin/`, with `app_id` set: the session's `gh` and `git-credential-shipmill` helpers,
  mode `0700`, rewritten before each launch. They run the gate's own shipmill (`python -m
  shipmill app-token`) with the App's id, key path, repo, and checkout as arguments, and
  hold no token

A file that isn't its shape exits 2 naming its path. Everything else comes from GitHub and
from `claude agents`.

## Next

1. **Pull requests in the fingerprint:** `prs = true` counts open PRs by number, so a new
   push to an open PR waits for `retry_hours`. A `pr_state` that lists heads, reviews, and CI
   fixes that
2. **Trust filter:** pass issues and PRs from owners and collaborators automatically;
   leave the rest for an interactive session. An unattended session reads their text with
   the whole workspace in reach
3. **Mode 2:** the `needs-decision` protocol in the skills, then `--mode headless`
4. **Your own sessions:** the gate counts only the sessions it started. If you are
   triaging the same repo by hand, the launched session finds you through `ListAgents`, as
   the skills already require
