# shipyard

From a GitHub issue to a published release, run by agents and shipyard's release workflows:

| Stage | Part | Does |
|---|---|---|
| Triage | `github-issue-triage` skill | A verdict on every issue (implement, postpone, clarify), a comment and labels, one PR per fix |
| Fix | `github-issue-resolve` skill | One issue: verify against main, fix with a regression test, open a PR |
| Land | `github-pr-triage` skill | Review each PR in its own worktree, fix small problems, merge only on green CI |
| Release | shipyard (`prepare.yml`, `land.yml`) | Cut the release on its lane when the policy says one is due, run CI on it, tag, publish |
| Watch | `github-ship-watch` skill | On a loop or schedule: catch a failed or stalled release run, a release missing from PyPI, unannounced fixes, and untriaged issues; finish what the policy decided |
| Set up | `shipyard-setup` skill | Wire a repository to shipyard: package checks, CI, publishing, the policy, a dry run |

[docs/flow.md](docs/flow.md) shows what to say to run each stage, alone or all at once.

## Install the skills

As a Claude Code plugin, which also reaches scheduled cloud sessions:

```
/plugin marketplace add romamo/shipyard
/plugin install shipyard@shipyard
```

The skills are then `/shipyard:github-issue-triage` and so on. For a checkout you edit,
or for Codex, link each skill folder into `~/.agents/skills` (Claude Code reads it through
`~/.claude/skills`):

```bash
for s in ~/PycharmProjects/shipyard/skills/*/; do ln -sfn "$s" ~/.agents/skills/; done
```

The skills need `gh` signed in, and run their scripts with `uv run --no-project python`
or Python 3.10+.

## Release lanes

Release lanes for projects that keep a hand-written CHANGELOG. Every pull request adds its
own entry under Unreleased; a policy file decides when each lane releases, and shipyard's
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

### How a release works

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

#### When something is wrong in a batch

Escalate in this order:

1. **Channel**: the client who needs feature A pins the `dev` or `rc` build that has it
2. **Revert**: someone files a `release-blocker` issue, which holds `rc` and `stable`; the
   revert pull request removes the bad change and its CHANGELOG entry, and the next rc soaks
3. **Hotfix**: when only a stable release will do, ship feature A alone from `release/X.Y`

### Quick start

```bash
uvx --from git+https://github.com/romamo/shipyard@v0 shipyard init --ci ci.yml
uvx --from git+https://github.com/romamo/shipyard@v0 shipyard doctor
```

`init` writes `.github/shipyard.toml` in `mode = "dry-run"` and
`.github/workflows/release.yml`, which wires shipyard's reusable workflows to your CI.
`doctor` checks what shipyard needs:

- A CHANGELOG with an Unreleased section, in keep-a-changelog or dash style
- A stable `vX.Y.Z` tag to count from
- A CI workflow that runs on `workflow_call` with a `ref` input and checks that ref out
- A workflow for each `dispatch` entry that runs on `workflow_dispatch` with a `tag` input
- For each environment, a workflow that runs on `workflow_dispatch` with `tag` and
  `environment` inputs, and a job that sets `environment:`
- No `[tool.uv.sources]` entry taken from a local path, which CI and users don't have
- No branch named `shipyard` on origin, which would block the `shipyard/<tag>` work branches

Merge it, run the Release workflow by hand with dry-run on, read the release commit in the
run summary, then set `mode = "release"`.

An agent can do the whole setup with the skill in
[`skills/shipyard-setup`](skills/shipyard-setup/SKILL.md).

### Where the agents run

Releasing runs in GitHub Actions and needs nothing else. The skills (triage, landing,
shipped notices) run wherever you start them:

| Option | Runs | Good for |
|---|---|---|
| On demand | in your Claude Code session, when you invoke a skill | getting started; you see each step |
| `/loop 30m /github-ship-watch <owner/repo> — watch and triage` | in an open session | a working day; stops when the session closes |
| A `/schedule` routine | in the cloud, with your computer off | hands-off intake and shipped notices |

A routine merges only when its prompt says "merge when green"; without it, it stops at open
pull requests. A cloud routine may not load the plugin, so its prompt clones shipyard
itself (the prompt is in [docs/flow.md](docs/flow.md#keeping-it-running)). Start with a
routine that doesn't merge, read its first runs, then decide.

### The policy

`.github/shipyard.toml`, read on every run. A repository set up earlier may keep the
same keys in `.github/release-policy.toml`, which still works; `doctor` suggests the
rename, and a repository may not have both. Rename only once the shipyard CLI your Release
workflow runs is a release that reads `shipyard.toml`: that CLI comes from the `tool` input
of `prepare.yml` and `land.yml` (default `git+https://github.com/romamo/shipyard@v0`), not
from the `uses:` ref, so pinning shipyard means setting both the `uses:` refs and `tool:`.

The keys:

```toml
name = "demo"
mode = "release"                  # off | dry-run | release
branch = "main"
version_files = "pyproject"       # pyproject.toml + uv.lock, or "none" (tags and CHANGELOG only)
after_stamp = ["uv lock --check"] # run after the version is written; a failure stops the release

[changelog]
style = "keep-a-changelog"        # "## [1.2.0] - date" + compare links; or "dash": "## 1.2.0 — date"

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

#### Environments

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
release, it starts the workflow of every environment whose `lane` is the release's, with
`-f tag=v<version> -f environment=<name>`, and lists it as `deploy.yml@staging`. The
workflow's job sets `environment: ${{ inputs.environment }}`, so GitHub records a deployment
for each run: those deployments, not shipyard, say what runs where. shipyard does not
promote `from` environments yet; `health` and `bake_minutes` are read for that.

### Autonomy and the stop switch

`[autonomy]` in the same file sets how far shipyard goes by itself at each stage: `observe`
reports only, `propose` opens an issue saying what it would do, and `act` does it. Every
stage defaults to `act`, which is how shipyard has always released:

```toml
[autonomy]
release = "propose"               # observe | propose | act
deploy.production = "act"         # per deploy environment; an unlisted one acts
rollback = "act"
```

- **`release = "observe"`**: a due lane is reported in the run summary ("is due, but release
  autonomy is observe") and nothing is pushed
- **`release = "propose"`**: shipyard opens one issue per due lane, "Ready to release vX on
  `<lane>`", with the version and its entries, and updates that same issue on later runs. A
  person releases by starting the lane by hand: `gh workflow run release.yml -f lane=<lane>
  -f dry-run=false`. A lane started by hand is a person acting, so it releases under
  `observe` and `propose`; `lane=policy` follows the policy like a scheduled run
- **The stop switch**: any open issue labelled `shipyard-hold` turns every `act` into
  `propose` for the repository. One label, no commit, so it works from a phone. While it is
  open no release happens by itself, the run summary names the hold, and a lane started by
  hand is refused too: close the hold to release

Opening the proposal issue needs `issues: write` on the prepare job in your
`.github/workflows/release.yml` (`shipyard init` writes it; a repository set up earlier
grants `issues: read` and gets a doctor warning). `doctor` prints the effective autonomy per
stage and warns while a hold is open. `deploy` and `rollback` are read and checked now, and
take effect once shipyard deploys and rolls back.

### Versions

- The next stable version is the last stable tag bumped by the pending entries, or the open
  pre-release series if that is higher, so `1.0.0rc9` leads to `1.0.0` even when the
  entries alone would give `0.2.0`
- Before 1.0, a major bump moves the minor
- A `dev` build after `1.5.0rc2` is `1.5.0rc3.devN`, so it sorts between the two rcs; `N`
  counts main's first-parent commits
- A hotfix is always a patch on its `X.Y` series

### CLI

The workflows call these; you can run them locally too.

| Command | What it does |
|---|---|
| `init` | Write the policy and the calling workflow |
| `doctor` | Check the repository is ready; exit 1 on a failure |
| `plan` | Decide whether a lane releases now; JSON on stdout |
| `propose` | Open or update the issue for each release a plan proposed |
| `prepare` | Stamp a planned release; `--commit`, `--push` |
| `land` | Push, tag, sync main, and publish a release commit that passed CI |
| `cleanup` | Delete a release commit's work branch |
| `sync` | Bring a stable release made off main into main, to recover a failed sync |
| `notes` | Print a release's notes |
| `settle-minutes` | Print how long to wait after a push |

Exit codes: `0` done, a plan may decide to skip; `1` doctor found a failure; `2` bad input
or a state shipyard refuses to act on.

### Limits

- GitHub's token can't push past branch protection: let `github-actions[bot]` bypass the
  rules on main and `release/*`, or the landing fails
- A push made with the workflow token starts no workflow: that is why publishing goes
  through `dispatch`, and why the sync commit on main runs no CI of its own
- A publish workflow must accept a tag whose commit is not on main: pre-releases and
  promoted stable releases are tagged off main
- The version comes from `pyproject.toml` or from tags; other version files are covered by
  `version_lines`

## License

MIT
