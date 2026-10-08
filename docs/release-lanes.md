# Release lanes

The reference for shipmill's release side: lanes, the policy file, environments, operate,
autonomy, versions, and the CLI. To set it up, start with [install.md](install.md).


Release lanes for projects that keep a hand-written CHANGELOG. Every pull request adds its
own entry under Unreleased; a policy file decides when each lane releases, and shipmill's
reusable GitHub Actions workflows cut the release, run your CI on it, tag it, and publish
it.

| Lane | Version | Released from | Typical trigger |
|---|---|---|---|
| `dev` | `1.5.0.dev412` | main's head | after every batch of merges |
| `rc` | `1.5.0rc2` | main's head | each workday morning |
| `stable` | `1.5.0` | the base of an rc that soaked without a blocker | weekly, a milestone, or by hand |
| `hotfix` | `1.4.1` | `release/1.4` plus the pull requests you name | by hand only |

Installers skip `dev` and `rc` versions unless asked, so a client who needs a feature today
pins the pre-release, and everyone else gets stable releases that real users already ran.

## How a release works

Every release is a **stamp commit**: a base commit plus the version files, the policy's
`version_lines`, and, for a stable release, the CHANGELOG section. CI runs on that commit
before anything is tagged.

- **Pre-releases (`dev`, `rc`) never touch main.** Their commit is reachable only from the
  tag, and their entries stay under Unreleased until a stable release
- **A stable release promotes a soaked rc.** It stamps the commit the rc was built from, so
  it ships exactly the code the rc did. If main has not moved, main fast-forwards to the
  release; otherwise the release is tagged and a sync commit on main moves the released
  entries into the new section and points main's version at it
- **A hotfix** starts from `release/X.Y` (or the last `X.Y.Z` tag), applies each named pull
  request's change, takes their CHANGELOG entries, releases `X.Y.(Z+1)`, and syncs main the
  same way

### When something is wrong in a batch

Escalate in this order:

1. **Channel**: the client who needs feature A pins the `dev` or `rc` build that has it
2. **Revert**: someone files a `release-blocker` issue, which holds `rc` and `stable`; the
   revert pull request removes the bad change and its CHANGELOG entry, and the next rc soaks
3. **Hotfix**: when only a stable release will do, ship feature A alone from `release/X.Y`

## The policy

`.github/shipmill.toml`, read on every run. The shipmill CLI your Release workflow runs
comes from the `tool` input of `prepare.yml` and `land.yml` (default
`git+https://github.com/shipmill/shipmill@v0`), not from the `uses:` ref, so pinning
shipmill means setting both the `uses:` refs and `tool:`.

The keys:

```toml
name = "demo"
mode = "release"                  # off | dry-run | release
branch = "main"
version_files = "pyproject"       # pyproject.toml + uv.lock, or "none" (tags and CHANGELOG only)
after_stamp = ["uv lock --check"] # run after the version is written; a failure stops the release

[changelog]
style = "keep-a-changelog"        # "## [1.2.0] - date" + compare links; or "dash": "## 1.2.0 — date"
fragments = "changelog.d"         # optional: one <name>.md per PR, pending with Unreleased (spec 013)

[bump]                            # how far the next version moves from the last stable one
from = "headings"                 # the pending entries' ### headings...
major = ["Breaking"]
minor = ["Added", "Changed", "Deprecated", "Removed"]
patch = ["Fixed", "Security"]
# from = "paths"                  # ...or whether files under these changed since the last stable tag
# minor_paths = ["schemas/*"]

[gates]
blocker_label = "release-blocker" # an open issue with it holds blocker_lanes
blocker_lanes = ["rc", "stable"]
freeze = ["2026-12-24..2027-01-02"]
freeze_lanes = ["dev", "rc", "stable"]

[lanes.dev]
quiet_minutes = 30                # due once main has been quiet this long
dispatch = ["publish.yml"]        # workflows started with -f tag=v<version>

[lanes.rc]
schedule = ["Mon-Fri 07:00 Europe/Kyiv"]
github_release = true             # a GitHub release, marked pre-release

[lanes.stable]
promote_from = "rc"               # omit to release main's head directly
min_soak_days = 3
schedule = ["Mon 07:00 Europe/Kyiv"]
milestone = true                  # due once the milestone named "1.5.0" has no open issues
github_release = true
dispatch = ["publish.yml"]

[lanes.hotfix]                    # by hand only
github_release = true

[[version_lines]]                 # other lines a release rewrites; each matches exactly once
file = "README.md"
pattern = 'demo v\d+\.\d+\.\d+'
replace = "demo v{version}"       # also {name}, {semver}, {minor} (X.Y), {date}
```

A lane releases when one of its **triggers** is due (quiet time, a schedule window that
opened after its last release, a finished milestone) and no **gate** holds it. Any lane can
also be started by hand from the Release workflow, which skips the triggers but not the
gates. One run releases one lane, in the order hotfix, stable, rc, dev; GitHub's cron only
starts a run, and the policy decides whether a window is open.

### Environments

An optional `[environments]` section names where releases deploy, each through a deploy
workflow of your own:

```toml
[environments.staging]
lane = "rc"                       # deploy every release of this lane when it lands
workflow = "deploy.yml"
health = "https://staging.example.com/health"

[environments.production]
from = "staging"                  # promoted from staging instead of from a lane
workflow = "deploy.yml"
health = "https://example.com/health"
bake_minutes = 60                 # how long staging stays healthy before promotion
```

An environment sets exactly one of `lane` (a lane the policy enables) or `from` (another
environment, in a chain that ends at a `lane` one, with no cycle). After `land` publishes a
release, it starts the workflow of every environment whose `lane` is the release's, on the
tag, with `-f tag=v<version> -f environment=<name>`, and lists it as `deploy.yml@staging`.
The workflow's job sets `environment: ${{ inputs.environment }}`, so GitHub records a
deployment for each run, whose ref is the tag: those deployments, not shipmill, say what
runs where.

The deployment's ref is the run's, not the tag it checks out, so a run started by hand
without `--ref <tag>` would record a branch that names no release. The `ref` job fails such
a run before the deploy job starts (and makes its deployment), with the command to run
instead; `doctor` warns about a deploy workflow without that guard:

```yaml
on:
  workflow_dispatch:
    inputs:
      tag:
        type: string
        required: true
      environment:
        type: string
        required: true
jobs:
  ref:
    runs-on: ubuntu-latest
    steps:
      - name: Fail a run that isn't on the tag it deploys
        env:
          TAG: ${{ inputs.tag }}
          ENVIRONMENT: ${{ inputs.environment }}
        run: |
          if [ "$GITHUB_REF" != "refs/tags/$TAG" ]; then
            echo "::error::run it on the tag: gh workflow run deploy.yml --ref $TAG -f tag=$TAG -f environment=$ENVIRONMENT"
            exit 1
          fi
  deploy:
    needs: ref
    runs-on: ubuntu-latest
    environment: ${{ inputs.environment }}
    steps:
      - uses: actions/checkout@v7
        with:
          ref: refs/tags/${{ inputs.tag }}
  operate: # with from or health: the first health check, right after the deploy
    needs: deploy
    runs-on: ubuntu-latest
    permissions:
      actions: write
    steps:
      - env:
          GH_TOKEN: ${{ github.token }}
          GH_REPO: ${{ github.repository }}
        run: gh workflow run operate.yml -f dry-run=false
```

A GitHub environment with deployment protection must allow the release tags, since `land`
starts the workflow on the tag: enabling GitHub Pages, for one, creates `github-pages`
allowing only `main`, and rejects a run on `v1.2.0`. Add a tag rule `v*` under Settings →
Environments → `<environment>` → Deployment branches and tags, or run
`gh api -X POST repos/<owner>/<repo>/environments/<environment>/deployment-branch-policies -f name='v*' -f type=tag`.

### Operate: health, bake, promotion, and rollback

With `from` or `health` in use, run `shipmill init --operate` (the CLI from `uv tool install
shipmill`, or `uvx --from git+https://github.com/shipmill/shipmill@v0 shipmill` without it;
see [install.md](install.md#before-you-start)). It writes
`.github/workflows/operate.yml`, which runs `shipmill operate` every 10 minutes, one run at
a time. The deploy workflow's `operate` job above also starts it once a deploy succeeds, so
the first health check runs right after the deploy rather than at the next scheduled run;
the schedule stays the backstop and runs the checks after it. It is a job of its own, so a
failure to start operate leaves the deployment's success alone, and it needs
`actions: write`; `doctor` warns when an environment with `from` or `health` has a deploy
workflow that doesn't start it. It can't deploy twice: operate deploys a tag to an
environment at most once (below), and a run it starts finds the environment on the tag just
deployed. Each run, for every environment:

- **Current deployment**: the newest GitHub deployment there that reached `success`, and
  its tag (the deployment's ref)
- **Health**: a GET of `health` that answers 2xx within 10 s. When the body is a JSON
  object with a string `version`, it must name the deployed release (`1.2.0` or `v1.2.0`),
  so a stale instance answering 200 isn't counted healthy; any other body isn't read. An
  environment without `health` is reported as such and bakes on time alone
- **Health history**: recorded as statuses on that deployment, written only when the state
  changes: `in_progress` while it bakes, `success` once baked. A failed check writes
  `failure` each time until `rollback_after` stand in a row, so those statuses are the
  count of checks failed in a row and a failing stretch writes at most that many. Nothing
  is kept outside GitHub
- **Bake and promotion**: an environment with `from` gets the source's tag once the source
  has been healthy on it for `bake_minutes`, counted from the source deployment's success,
  every check since passing. A failed check restarts the bake from the next passing one
- **A missed deploy**: an environment with `lane` that is behind its lane's newest tag
  (land's dispatch failed, say) gets that tag, 30 minutes after it was tagged. Hotfix
  tags look like stable ones, so a `hotfix` environment isn't checked

Either deploy follows `deploy.<environment>` in `[autonomy]` under the hold: `act` (the
default) starts the environment's workflow on the tag; `propose`, or `act` while a
`shipmill-hold` issue is open, opens one "Ready to promote vX to `<environment>`" issue and
keeps it up to date; `observe` only reports. shipmill deploys a tag to an environment at
most once: a deployment of that tag there, in any state, means it was tried, and so does a
run of its workflow on the tag (still queued, or failed before its deploy job made a
deployment); a failed one is not retried. Approve a proposal with `gh workflow run operate.yml -f
approve=<environment> -f dry-run=false`: it deploys the proposed tag once and closes the
issue, under `propose` or `observe`, but not while a hold is open. A proposal also closes,
with a comment, once the environment runs its tag (or a later one), whoever deployed it. The
run summary lists
each environment's tag, health, and what the run did; `shipmill operate --dry-run` shows the
same and changes nothing.

**Rollback and incidents.** An environment that fails `rollback_after` health checks in a
row (3 by default: 10 to 20 minutes after the deploy when the deploy workflow starts
operate, 20 to 30 on the schedule alone) is rolled back to the
previous tag that reached `success` there, skipping one whose last check failed:

```toml
[operate]
rollback_after = 3                # failed health checks in a row before a rollback
incident_label = "incident"       # the label of the incident issue, which holds releases
```

- **The rollback** follows `rollback` in `[autonomy]` under the hold: `act` (the default)
  starts the environment's workflow on that tag; `propose`, or `act` while a
  `shipmill-hold` issue is open, has the incident say which rollback it would start, and
  `gh workflow run operate.yml -f approve-rollback=<environment> -f dry-run=false` starts
  it once (not while a hold is open); `observe` only says so. A rollback is the one deploy
  exempt from "at most once per tag": its tag ran there before by definition. It is still
  started once: a deployment of that tag after the bad one, or a run of the workflow on it
  since the checks began failing (still queued, say), means it was
- **The incident** is one issue per environment and bad tag, labelled `incident_label`: the
  environment, the tag, the failing check's status, the start of its body (capped, on one
  line, with secret-looking values redacted), the health URL without its query string or credentials, and the rollback's run or
  why there was none. It opens under every autonomy level and under the hold. Later runs
  don't open another: they comment when the environment is healthy again, and when the
  rollback's tag fails its checks too, where shipmill stops instead of rolling back a
  second time. Closing an incident while its checks still fail silences that stretch of
  failures; failing again after the close (on either tag) opens a new incident
- **An open incident holds releases**: the planner treats `incident_label` like
  `blocker_label`, for the lanes in `[gates] blocker_lanes`. Close it by hand, or let a
  hotfix pull request's "Fixes #N" close it, and releases flow again. A repository with no
  `health` URL can't open one, so its plan doesn't read the label

The operate job needs `deployments: write` (statuses) and `actions: write` (deploys), and
`issues: write` to open a proposal or an incident; `init --operate` grants them, and
`doctor` warns when one is missing, only once an environment uses `from` or `health`
(`issues: write` once one has `health` or a stage proposes).

## Autonomy and the stop switch

`[autonomy]` in the same file sets how far shipmill goes by itself at each stage: `observe`
reports only, `propose` opens an issue saying what it would do, and `act` does it. Every
stage defaults to `act`, which is how shipmill has always released:

```toml
[autonomy]
release = "propose"               # observe | propose | act
deploy.production = "act"         # per deploy environment; an unlisted one acts
rollback = "act"
intake = "propose"                # observe | propose: the product-intake skill
```

- **`release = "observe"`**: a due lane is reported in the run summary ("is due, but release
  autonomy is observe") and nothing is pushed
- **`release = "propose"`**: shipmill opens one issue per due lane, "Ready to release vX on
  `<lane>`", with the version and its entries, and updates that same issue on later runs. A
  person releases by starting the lane by hand: `gh workflow run release.yml -f lane=<lane>
  -f dry-run=false`. A lane started by hand is a person acting, so it releases under
  `observe` and `propose`; `lane=policy` follows the policy like a scheduled run. Once the
  release is tagged, the land workflow closes the lane's issue with a comment naming the tag
  and the run, and says so when the issue proposed another version
- **`intake`** (the product-intake skill) defaults to `propose`: it opens and updates
  opportunity issues, and only the maintainer accepts or declines one, so `act` is refused.
  `observe` reports the groups it would make
- **The stop switch**: any open issue labelled `shipmill-hold` turns every `act` into
  `propose` for the repository. One label, no commit, so it works from a phone. While it is
  open no release happens by itself, the run summary names the hold, and a lane started by
  hand is refused too: close the hold to release. The one exception is a hotfix started by
  hand, since a hold usually means an incident and a hotfix is its cure

Every proposal issue, for a release or a deploy, carries the `shipmill-proposal` label
(created on first use), and shipmill finds it again by that label, however many issues the
repository has. A proposal opened before the label is found once by scanning the newest
500 open issues and gets the label on that update.

Opening the proposal issue needs `issues: write` on the prepare job in your
`.github/workflows/release.yml`; `shipmill init` writes it. A repository set up earlier
grants `issues: read`, which is enough until a stage is set to `propose` or a hold is
opened; from then on `doctor` warns until the prepare job grants `issues: write`. Under
`release = "propose"` closing the issue once released needs `issues: write` on the land job
too; `doctor` warns until it has it. `doctor`
prints the effective autonomy per stage and warns while a hold is open. Each `deploy.<name>`
must name an environment in `[environments]`. `deploy` and `rollback` take effect in
`shipmill operate` (see Operate above).

## Roadmap

`[roadmap]` sets the capacity the `product-intake` skill plans milestones within. It
proposes the next milestone from accepted opportunities, ranked by evidence per load, as an
issue the maintainer approves; `doctor` validates the section:

```toml
[roadmap]
wip = 5       # issues open at once across the open milestones (1..100)
cadence = 2   # weeks from one milestone's due date to the next (1..26)
```

## Versions

- The next stable version is the last stable tag bumped by the pending entries, or the open
  pre-release series if that is higher, so `1.0.0rc9` leads to `1.0.0` even when the
  entries alone would give `0.2.0`
- Before 1.0, a major bump moves the minor
- A `dev` build after `1.5.0rc2` is `1.5.0rc3.devN`, so it sorts between the two rcs; `N`
  counts main's first-parent commits
- A hotfix is always a patch on its `X.Y` series

## CLI

The workflows call these; you can run them locally too.

| Command | What it does |
|---|---|
| `init` | Write the policy and the calling workflow; `--operate` writes the operate one |
| `operate` | Check environment health, promote after the bake, roll back; `--approve <env>`, `--approve-rollback <env>`, `--dry-run` |
| `doctor` | Check the repository is ready; exit 1 on a failure |
| `plan` | Decide whether a lane releases now; JSON on stdout |
| `propose` | Open or update the issue for each release a plan proposed |
| `prepare` | Stamp a planned release; `--commit`, `--push` |
| `land` | Push, tag, sync main, and publish a release commit that passed CI |
| `close-proposal` | Close the lane's proposal issue once its release landed, under `release = "propose"` |
| `cleanup` | Delete a release commit's work branch |
| `sync` | Bring a stable release made off main into main, to recover a failed sync |
| `notes` | Print a release's notes |
| `settle-minutes` | Print how long to wait after a push |
| `gate` | Start a Claude Code session for a repo only when its state needs one, with the config's `[agents]` prompt ([agent modes](design/agent-modes.md)) |
| `launchd` | Run `gate` for a dedicated checkout every few minutes on a Mac |

Exit codes: `0` done, a plan may decide to skip; `1` doctor found a failure; `2` bad input
or a state shipmill refuses to act on.

## Limits

- GitHub's token can't push past branch protection: let `github-actions[bot]` bypass the
  rules on main and `release/*`, or the landing fails
- A run cancelled before its cleanup job gets a runner leaves its `shipmill/<tag>` work
  branch on origin. The next run's prepare deletes it and pushes its own when no other run
  of your Release workflow is queued or in progress, which takes `actions: read` on the
  prepare job in `.github/workflows/release.yml` (`shipmill init` writes it). Without that
  permission the run stops at the branch, stays green, and says so in a "Work branch held"
  warning and the run summary; delete the branch by hand, or add the permission
- A push made with the workflow token starts no workflow: that is why publishing goes
  through `dispatch`, and why the sync commit on main runs no CI of its own
- A publish workflow must accept a tag whose commit is not on main: pre-releases and
  promoted stable releases are tagged off main
- The version comes from `pyproject.toml` or from tags; other version files are covered by
  `version_lines`

