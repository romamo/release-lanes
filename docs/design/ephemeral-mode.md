# Design: shipmill on GitHub Actions (ephemeral mode)

Status: parked, 2026-10-04. [agent-modes.md](agent-modes.md) is the plan: sessions run in a
persistent Claude Code install, which has the memory, repos, and peers that decision quality
depends on. Revive this only when one of these holds: a team shares the App, the machine
that hosts Claude Code can't stay on, or outside contributions need work while nobody is
at a terminal.

shipmill runs a repo from issue to release inside the repo's own GitHub Actions, as its own
GitHub App. Code decides whether anything needs an agent; Claude Code runs only for
judgment, one bounded work item per job; every fact the next run needs lives on GitHub.

## Goals

- A quiet tick costs seconds of runner time and no model call
- Agents only judge: verdicts, designs, code, reviews, failure diagnosis. Code does the
  state checks, repairs the policy already decided, pushes, merges, and bookkeeping
- Every run starts in an empty container and needs no state from earlier runs
- Everything shipmill writes on GitHub comes from `<slug>[bot]`, so the user's own replies,
  approvals, and notifications stay theirs
- A human decision blocks one item, not the pipeline, and costs nothing while it waits

## Non-goals

- Agents other than Claude Code (the skills stay in the portable `SKILL.md` format, but
  only Claude Code is tested)
- Hosts other than GitHub
- Replacing shipmill's release workflows: they keep cutting releases by policy; this design feeds them
  merged PRs and kicks it

## Overview

```
 events ─┐   schedule (*/15, backstop)   workflow_dispatch (chaining, by hand)
         ▼
 ┌──────────────────────── .github/workflows/shipmill-agents.yml ─────────────────────────┐
 │ gate (code only, ~20 s)                                                          │
 │   app token → shipmill gate → repairs done + work list (JSON output)             │
 │        │ empty list: the run ends here                                           │
 │        ▼                                                                         │
 │ triage ─── one Claude job: verdicts for NEW / UNBLOCKED / REVISIT issues          │
 │ implement ─ matrix, one Claude job per NEEDS_PR issue → a PR                     │
 │ review ──── matrix, one Claude job per PR that needs review → a PR review        │
 │ diagnose ── matrix, one Claude job per red run the gate couldn't classify       │
 │ land (code only) ─ shipmill land-queue: rebase, CHANGELOG union, gate, merge      │
 │        │ anything changed: dispatch the workflow again                           │
 └──────────────────────────────────────────────────────────────────────────────────┘
 shipmill release (.github/workflows/release.yml, unchanged) ← kicked by gate and land
```

### Triggers

```yaml
on:
  schedule: [{ cron: "*/15 * * * *" }]
  issues: { types: [opened, edited, reopened, labeled, unlabeled] }
  issue_comment: { types: [created] }
  workflow_run: { workflows: [CI], types: [completed] }
  workflow_dispatch:
concurrency: { group: shipmill-gate }   # a newer pending run replaces the older one
```

- Events make a human's reply or a finished CI run act within a minute; the schedule is
  the backstop, since GitHub delays and can drop scheduled runs under load
- No `pull_request_target` and no `pull_request`: fork PRs never start a job that holds
  secrets. A PR's state reaches the gate through `workflow_run` on CI and the schedule
- Events caused by `<slug>[bot]` itself skip at the job's `if:`. Pushes, comments, and
  labels made with an App token do start workflows, unlike `GITHUB_TOKEN`; without the
  filter every bot comment would start a run. A stage that changed something starts the
  next round on purpose with `gh workflow run shipmill-agents.yml`, a step in code
- The default concurrency behaviour fits the gate: a burst of events collapses into one
  pending run, and that run re-reads all state anyway

## The gate

`shipmill gate <owner/repo> --scope <scope> --json` runs code only, from
`uvx --from git+https://github.com/shipmill/shipmill@v0`. It needs no checkout for the state
read; it uses a blobless clone (`--filter=blob:none`) only for the repairs that touch git.

1. **Read.** One GraphQL pass plus a few REST calls: issues with comments and labels, open
   PRs with heads, reviews, and check rollups, recent workflow runs and jobs, tags, the
   release policy, `.github/shipmill.toml`
2. **Classify.** The existing logic of `triage_state.py`, `watch_state.py`, and a new
   `pr_state.py`, moved into the package
3. **Repair** what the policy already decided (below), within the scope
4. **Select** the work list: owed items, minus the ones waiting on a human, minus the ones
   over their attempt limit, capped per stage
5. **Output** the list as job outputs, and a table in the job summary

Exit 0 always on a successful read, with the list in the output; exit 2 on a GitHub or
input failure, which fails the run visibly.

### Repairs in code

| Finding | Repair |
|---|---|
| DONE_NOT_CLOSED | Close the issue, citing the merged PR |
| SUSPECT_CLOSE whose linked PR merged | Nothing; the close was right. Only an unmerged fix becomes work |
| BOT_STALLED | `gh workflow run release.yml -f lane=<lane>` for the lane the plan names |
| UNANNOUNCED | `shipped.py --post` |
| PUBLISHED, not install-checked | Clean install of the version, per ecosystem; a failure becomes work |
| Red run on a PR whose failing tests all appear in `flaky_tests` | Rerun the failed jobs once (`run_attempt` 1 only), current head only |
| REVISIT whose last verdict is newer than the stable tag | Nothing (fixed in #12) |

### What counts as work

| Item | Owed when | Stage |
|---|---|---|
| Issue NEW, UNBLOCKED, REVISIT | always | triage |
| Issue NEEDS_PR | no open PR links it | implement |
| SUSPECT_CLOSE, fix not merged | always | triage |
| PR from the bot or a trusted author | no bot review at the current head | review |
| PR with a bot review MERGE at the current head, green | scope includes merge | land (code) |
| PR conflicting outside CHANGELOG after land-queue's rebase | always | review, with the conflict |
| Red run not repaired above, or failed again after a rerun | always | diagnose |
| BOT_FAILED | always | diagnose |

An item is **waiting**, and skipped, when:

- it has the `needs-decision` label and no comment from a trusted human is newer than the
  bot's last comment on it
- it has the `postponed` or `blocked` label (BLOCKED, POSTPONED)
- its author is untrusted and no trusted human has approved it (Trust, below)
- its stage failed twice in the last 24 hours (counted from this workflow's job names,
  such as `implement #12`, through the Actions API)

## State lives on GitHub

Every run starts empty. The gate derives each fact from GitHub:

| Fact | Source |
|---|---|
| Verdict | The bot's newest comment starting `Triage:` |
| Waiting on a human | `needs-decision` label, plus the author of the newest comment |
| Reviewed at a commit | The bot's PR review: its `commit_id`, and the verdict in the first line of its body (`Review: MERGE`) |
| Attempts | Failed jobs named `<stage> #<n>` in this workflow's recent runs |
| Rerun already tried | The run's `run_attempt` |
| Notices posted | `shipped.py`'s existing check for "Released in" comments |
| Settled design rules | The decisions log in the repo |

Local mode (crontab on a checkout) runs the same gate and may cache under
`$(git rev-parse --git-common-dir)/shipmill/`. The cache only saves API calls; losing it
changes no decision.

## Agent jobs

Each agent job has the same shape:

```yaml
- uses: actions/create-github-app-token@v3
  id: app
  with:
    client-id: ${{ vars.SHIPMILL_APP_CLIENT_ID }}
    private-key: ${{ secrets.SHIPMILL_APP_KEY }}
    permission-contents: write
    permission-pull-requests: write
    permission-issues: write
- uses: actions/checkout@v5
  with: { token: "${{ steps.app.outputs.token }}", fetch-depth: 0, filter: "blob:none" }
- run: uvx --from git+https://github.com/shipmill/shipmill@v0 shipmill agent-env >> "$GITHUB_ENV"
  env: { GH_TOKEN: "${{ steps.app.outputs.token }}" }   # git author as <id>+<slug>[bot]
- run: |
    npm install -g @anthropic-ai/claude-code
    git clone --depth 1 --branch v0 https://github.com/shipmill/shipmill "$RUNNER_TEMP/shipmill"
    claude -p "/shipmill:github-issue-resolve ${{ matrix.issue }} — $(cat brief.md)" \
      --plugin-dir "$RUNNER_TEMP/shipmill" \
      --permission-mode dontAsk --allowedTools "$SHIPMILL_TOOLS" \
      --max-turns 80 --output-format json > result.json
  env:
    GH_TOKEN: ${{ steps.app.outputs.token }}
    CLAUDE_CODE_OAUTH_TOKEN: ${{ secrets.CLAUDE_CODE_OAUTH_TOKEN }}
  timeout-minutes: 50
- run: shipmill agent-report result.json >> "$GITHUB_STEP_SUMMARY"   # cost, outcome
```

- **Plain `claude -p`, not `claude-code-action`.** The action documents no way to act as
  our own App, and its plugin inputs add little over `--plugin-dir`
- **Skills by explicit slash command.** In `-p` mode a plugin skill runs only when the
  prompt names it (`/shipmill:<skill>`)
- **One item per job.** The App token lasts one hour and is revoked at the job's end, so
  every job mints its own and stays under 50 minutes. Parallel work is a matrix
  (`max-parallel: 3`), not subagents inside one run; a job's failure touches one item
- **The brief comes from code.** `shipmill brief <stage> <n>` writes the repo facts
  (default branch, release phase, check commands, CHANGELOG rule, rules files,
  `decisions.py find` output) and the item's plan from its triage comment. The agent
  doesn't rediscover them
- **Tools are an allowlist** with `--permission-mode dontAsk`: Read, Edit, Write, Grep,
  Glob, and Bash for `git`, `gh`, `uv`, and the repo's check commands. Never
  `bypassPermissions`
- **Cost is recorded.** `total_cost_usd` from the JSON result goes to the job summary;
  the gate sums recent runs against `max_daily_cost_usd`

### Stages

| Stage | Skill and scope | Writes |
|---|---|---|
| triage | `github-issue-triage`, "decide only" over the listed issues | Verdict comments, labels, `needs-decision` questions |
| implement | `github-issue-resolve`, phases 2 to 6, no issue comments | A branch and PR; the "decisions for you" list as a PR comment |
| review | `github-pr-triage`, review one PR, push small fixes | Fix commits, then a PR review at the final head |
| diagnose | `github-pr-triage` red-CI rules, one run | A rerun, or a comment naming the PR-caused failure, or a flake issue |

The skills gain a headless rule: when no one can answer AskUserQuestion, a decision for
the user becomes the `needs-decision` protocol below, and the item stops there.

### Land, in code

`shipmill land-queue` takes the PRs with a bot review MERGE at their current head, in
order (stack bases first), and for each:

1. Rebase onto the default branch; union CHANGELOG conflicts; any other conflict stops
   this PR and makes it review work with the conflict named
2. `changelog_guard check` and the release planner's dry run on the result
3. Push with a lease on the reviewed head, then confirm the head and a non-empty diff
4. Wait for `pr_gate` on the new head; merge in the repo's style; retarget a stacked child
5. Close the issue if the merge didn't, then go to the next PR

A rebase that changed code moves the head past the reviewed commit, so the PR needs a
review again before it merges; a CHANGELOG-only rebase does not (the diff outside
CHANGELOG is compared). After the last merge, it kicks the release lane the batch is due
on.

## Human decisions

- The agent asks in a comment that mentions the repo's `deciders`, adds `needs-decision`,
  and stops. Options come first, with its recommendation
- The gate skips the item while the newest comment is the bot's. A trusted human's reply
  fires `issue_comment`, and the next run hands the item back to the agent with the reply
  in the brief
- Settled answers that set a rule go to the decisions log, as today

## Trust and security

Issue and PR text from anyone reaches an agent that holds a write token, so it is
untrusted input to a model.

- **Who can start work.** `trusted = ["OWNER", "MEMBER", "COLLABORATOR"]` by
  `author_association`. Untrusted authors' issues get triage verdicts (comments and
  labels only); implement starts after a trusted human adds `shipmill:go`. Untrusted PRs
  get a review without running their code
- **No fork code with secrets.** No `pull_request_target`; review jobs run checks only for
  heads in the repo itself
- **Least privilege per job.** Each job's token asks only for the permissions it needs.
  No job asks for `workflows`, so nothing the agent pushes can change a workflow file
- **The private key never reaches Claude's environment.** Only the token step reads it
- **Budgets.** `--max-turns`, `timeout-minutes`, items per stage per run, and
  `max_daily_cost_usd`

## Configuration

The `[agents]` section of `.github/shipmill.toml` (D-4), read on every run:

```toml
[agents]
mode = "dry-run"               # off | dry-run (job summary only) | live
scope = "triage"               # watch | triage | implement | merge
deciders = ["romamo"]
trusted = ["OWNER", "MEMBER", "COLLABORATOR"]
checks = ["uv run pytest -q", "uv run ruff check", "uv run mypy"]
flaky_tests = []
max_items = { triage = 10, implement = 3, review = 3, diagnose = 2 }
max_daily_cost_usd = 20
```

`dry-run` mirrors shipmill's release `mode`: the gate and the agents run, nothing is written to
GitHub, and the job summary shows the comments, PRs, and merges it would have made.

## Setup

The `shipmill-setup` skill changes from starting a `/loop` to:

1. Register the App with the manifest flow, with the permissions above and no webhook.
   The redirect goes to the repo's page, and the user pastes the `code` from the address
   bar (a localhost redirect isn't documented as allowed). `POST
   /app-manifests/{code}/conversions` returns the id, client id, and key within an hour
2. Install it on the repo; set `SHIPMILL_APP_CLIENT_ID` (variable), `SHIPMILL_APP_KEY`, and
   `CLAUDE_CODE_OAUTH_TOKEN` (from `claude setup-token`) or `ANTHROPIC_API_KEY`
3. Under merge scope, add the App to the ruleset's bypass list for the default branch
4. Write `shipmill-agents.yml` and the `[agents]` section in `dry-run`, in the setup PR
5. After the merge, run it by hand once and read the summary with the user; the user
   switches to `live`

## Code changes

| Change | Where |
|---|---|
| Move the skill scripts into the package as subcommands, typed and tested | `src/shipmill/` |
| `gate`, `pr-state`, `brief`, `land-queue`, `agent-env`, `agent-report` | new subcommands |
| `pr_gate`: judge the newest run per check on the head (cancelled twins) | existing |
| `changelog_guard check`: validate headings with shipmill's parser | existing |
| `init --agents`: write `shipmill-agents.yml` and the `[agents]` section | existing `init` |
| Headless rule and the `needs-decision` protocol | the four agent skills |
| Remove `agents/openai.yaml`, the Codex install notes, and `<skill>/scripts` paths | skills, README, docs |

## Plan

1. **Spike** (a test repo, an hour): confirm what the docs leave open, below
2. **Gate, read-only:** App token, the workflow, `shipmill gate` in dry-run with the
   summary table. No agent jobs yet
3. **Repairs and land-queue,** still code only
4. **Agent stages:** triage, then implement, review, diagnose, each behind `scope`
5. **Decisions, trust, budgets**
6. **Setup skill, docs, the Codex cleanup, script migration**

## Open questions

The docs don't settle these; the spike answers them:

- Does an @mention in a comment by `<slug>[bot]` notify the mentioned user? If not, the
  agent also requests the decider's review on PRs, or assigns the issue
- Does a human's approval satisfy required reviews on a PR the App opened?
- Does bot activity count toward the 60-day rule that disables scheduled workflows in
  public repos? The event triggers keep working either way
- Does the manifest flow accept a `http://localhost` redirect, which would remove the
  copy-paste step?

And these are the user's:

- Untrusted issues: triage verdicts automatically (recommended), or nothing until a
  trusted human labels them
- Claude credential: the subscription token, or an API key with its own budget
- The default tick: 15 minutes (recommended), or slower for private repos on paid minutes
