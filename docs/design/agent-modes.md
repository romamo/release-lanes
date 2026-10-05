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
| 1. Interactive | A new `claude --bg` session per launch: attachable, listed in `claude agents` | AskUserQuestion; the session waits, and the gate starts nothing for that repo until you answer | Built: `shipmill gate` |
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
```

```
shipmill --repo ~/PycharmProjects/treaty/tmp/shipmill-gate gate romamo/treaty --refresh
shipmill --repo ~/PycharmProjects/treaty/tmp/shipmill-gate launchd romamo/treaty --every 15
```

The gate runs in a dedicated checkout: a detached worktree inside the trusted repo, never
the user's working copy, where the session would branch and commit. Each run:

1. Checks that the checkout's `origin` is the repo
2. Lists this repo's gate sessions with `claude agents --json`, then the open issues
   labelled `shipmill-hold`. Any: **HELD**, stop (D-11). A session already running
   finishes; the reason names it with `claude stop <id>`
3. A session whose state is `blocked` waits on you: **WAITING**, stop. One that is
   `working` or busy: **RUNNING**, stop. Both stop before anything else is read or moved
4. With `--refresh`, moves the detached, clean checkout to the head of origin's default
   branch, so the session reads the current config, `CLAUDE.md`, and skills
5. Reads `[agents]`, then the state with github-ship-watch's `watch_state.py` (bundled in
   the wheel, so the gate and the script always come from the same version). The rows
   that need an agent are the ones it marks `agent: true` (a failed or stalled release
   bot, a release missing from PyPI or not announced, issues triage owes, a failed operate
   run, an open incident), plus open PRs when `prs = true`. A promotion waiting on a
   person, an unhealthy environment (operate's rollback owns it), and a postmortem due
   start nothing. None: **QUIET**, stop. No model call has happened
6. Compares the rows with the last launch's fingerprint. The same rows within
   `retry_hours`: **UNCHANGED**, stop. A session that left an item alone on purpose
   doesn't wake a new one every tick
7. **LAUNCH**: stops this repo's finished sessions (`claude stop` keeps their
   conversation), starts `claude --bg -n "shipmill <repo> <time>" "<prompt>"` in the
   checkout with the rows appended to the prompt, each by state and subject only (an
   issue title in a row's detail is text anyone who edits the issue controls; the session
   reruns watch_state.py and reads it as data, not instructions), and records the launch

`--dry-run` prints the decision and touches nothing. `--claude-arg` passes flags to the
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
- Auto memory is per repository and shared by its worktrees and subdirectories; the
  session works in its own worktree and still sees the whole history. Transcripts are per
  working directory
- A session needs the checkout to be trusted: run `claude` there once and accept

### State

The gate keeps one file, `$(git rev-parse --git-common-dir)/shipmill/gate.json`: the last
launch's fingerprint, session id, and time. It is never committed and is shared by every
worktree. Losing it costs at most one extra session; it never changes what is decided.
Everything else comes from GitHub and from `claude agents`.

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
