# Design: agent modes

Status: mode 1 in progress, 2026-10-04

shipyard is stateless code. It decides whether a repo needs an agent, and when it does, it
starts a session in a **persistent Claude Code install**: Claude Code on your Mac, or on an
always-on machine set up the same way, with its memory, transcripts, global `CLAUDE.md`,
skills, plugins, the clones of all your repos, and its peer sessions. The agent judges; the
code decides when it runs and with what.

## Modes

| Mode | Session | Questions for you | Status |
|---|---|---|---|
| 1. Interactive | A new `claude --bg` session per launch: attachable, listed in `claude agents` | AskUserQuestion; the session waits, and the gate starts nothing for that repo until you answer | Built: `shipyard gate` |
| 2. Noninteractive | The same, started with a tool allowlist, for a machine nobody watches | The `needs-decision` protocol: a label and a comment that mentions you; the item waits on GitHub and the session ends | Next, after mode 1 shows how often sessions wait on you |
| 3. Ephemeral | A fresh container per job (GitHub Actions) | The same protocol | Parked: [ephemeral-mode.md](ephemeral-mode.md) |

Mode 1 replaces `/loop`, whose iterations all run in one growing session. Each launch is a
fresh session; memory, `CLAUDE.md`, and the GitHub threads carry the history, and older
transcripts stay searchable.

## The gate

```
shipyard --repo ~/PycharmProjects/treaty gate romamo/treaty \
  --prompt '/github-issue-triage {repo} merge when green' [--prs]
```

Run it from launchd (or any scheduler) every 15 minutes. Each run:

1. Checks that the checkout's `origin` is the repo
2. Lists this repo's gate sessions with `claude agents --json`. A session whose state is
   `blocked` waits on you: **WAITING**, stop. One that is `working` or busy: **RUNNING**,
   stop. Both stop before the slow state read
3. Reads the state with github-ship-watch's `watch_state.py` (bundled in the wheel). The
   rows that need an agent are its action states, plus open PRs with `--prs`. None:
   **QUIET**, stop. No model call has happened
4. Compares the rows with the last launch's fingerprint. The same rows within
   `--retry-hours` (24): **UNCHANGED**, stop. A session that left an item alone on purpose
   doesn't wake a new one every tick
5. **LAUNCH**: stops this repo's finished sessions (`claude stop` keeps their
   conversation), starts `claude --bg -n "shipyard <repo> <time>" "<prompt>"` in the
   checkout with the rows appended to the prompt, and records the launch

`--dry-run` prints the decision and touches nothing. `--claude-arg` passes flags to the
session, such as `--permission-mode`.

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

The gate keeps one file, `$(git rev-parse --git-common-dir)/shipyard/gate.json`: the last
launch's fingerprint, session id, and time. It is never committed and is shared by every
worktree. Losing it costs at most one extra session; it never changes what is decided.
Everything else comes from GitHub and from `claude agents`.

## Next

1. **Setup:** shipyard-setup writes the launchd job (`StartInterval`, the checkout as
   `WorkingDirectory`) and asks for the prompt and scope. launchd runs a missed interval
   when the Mac wakes; cron doesn't
2. **Pull requests in the fingerprint:** `--prs` counts open PRs by number, so a new push
   to an open PR waits for `--retry-hours`. A `pr_state` that lists heads, reviews, and CI
   fixes that
3. **Trust filter:** pass issues and PRs from owners and collaborators automatically;
   leave the rest for an interactive session. An unattended session reads their text with
   the whole workspace in reach
4. **Mode 2:** the `needs-decision` protocol in the skills, then `--mode headless`
5. **Your own sessions:** the gate counts only the sessions it started. If you are
   triaging the same repo by hand, the launched session finds you through `ListAgents`, as
   the skills already require
