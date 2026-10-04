# GitHub issue-to-ship flow

Three skills cover the path from a new issue to a published release, and a fourth watches it, in any GitHub repo with the `gh` CLI:

| Skill | Job |
|---|---|
| `github-issue-triage` | The backlog: a verdict on every issue (implement, postpone, clarify), comments and labels, one PR per fix |
| `github-issue-resolve` | One issue in depth: verify, fix with a regression test, open a PR |
| `github-pr-triage` | Landing: review, CI gate, rebase, merge, and the release |
| `github-ship-watch` | The routine: a stuck release run, a missing upload, unannounced fixes, waiting issues |

Before the first release, a new package goes through `shipyard-setup`: package checks, CI and publish wiring, and shipyard's release policy. In a repo that runs shipyard (`.github/shipyard.toml`, or its alias `.github/release-policy.toml`), shipyard tags; the skills here only start its Release workflow and never tag by hand. The `oss-package-engineer` skill is retired: its release path is github-pr-triage's "tag X" or shipyard, and its package checks live in shipyard-setup.

Start each one with its slash command or with plain wording. The words you use set how far it goes. Below, `<owner/repo>` is the GitHub repo, `<X>` the version to release, and `<prev>` the previous tag.

## Everything in one message

```
/github-issue-triage <owner/repo> — triage the new issues, merge the PRs when they're green, then tag <X>
```

- **"triage"** runs github-issue-triage: verdicts, comments and labels, and a PR for each "implement"
- **"merge when green"** hands the PRs to github-pr-triage, which merges each one only when every CI job passes
- **"tag X"** adds the release: readiness check, tag, registry check, and the "Released in" notices on fixed issues

Plain wording works the same, and the repo can be left out inside its checkout: "triage the new issues, merge when green, then tag 2.4.0".

## One stage at a time

`$S` below is the `skills` folder of this repo (or `~/.agents/skills`, which links to it); run the scripts with `uv run --no-project python` or plain `python3` (3.10+).

| Stage | Say | Stops at |
|---|---|---|
| Status only | `python3 $S/github-issue-triage/scripts/triage_state.py <owner/repo>` | A list of each issue's state; nothing changes |
| Triage the backlog | `/github-issue-triage <owner/repo>` or "triage the new issues" | Comments, labels, and open PRs; nothing merged |
| One issue | `/github-issue-resolve 57` or "fix #57" | One PR and the issue comment |
| Triage one issue only | "triage #57, don't fix" | The verdict comment |
| Review PRs | `/github-pr-triage` or "review open PRs" | Verdicts and local fixes, no pushes |
| Merge | "merge #75" or "merge the PRs when they're green" | Merged on green CI |
| Release | "tag <X>" | Readiness check, tag, publish, notices |
| Tell reporters only | `python3 $S/github-pr-triage/scripts/shipped.py <owner/repo> <prev> <tag> --install '<install cmd>'` | Prints the plan; add `--post` to comment |
| Release check only | `python3 $S/github-pr-triage/scripts/release_ready.py <owner/repo> <sha> <X>` | A pass/fail report |

## Other stacks

| Situation | Flag |
|---|---|
| Version in a file other than `pyproject.toml`, `Cargo.toml`, or `package.json` | `release_ready.py --manifest <file>` |
| Version only in the tag (Go) | `release_ready.py --no-manifest` |
| No Keep a Changelog file | `release_ready.py --no-changelog` |
| Tags aren't `vX.Y.Z` (CalVer, `release-1.2`) | `triage_state.py --stable-tag-regex '<regex>'` |
| Triage comments use another prefix, or deferrals another label | `triage_state.py --marker '<prefix>' --postponed-label <label>` |

The bump, registry, and install commands per ecosystem (Python, Node, Rust, Go, none) are in `github-pr-triage/references/landing.md`, under Ecosystems. GitLab and other hosts aren't supported: every script calls `gh`.

## Issue states

`triage_state.py` sorts every issue into one state and exits 1 when any need action.

| State | Meaning | Next step |
|---|---|---|
| NEW | No triage comment yet | Triage it |
| NEEDS_PR | Triaged "implement", no PR yet | Dispatch an implementer |
| IN_PROGRESS | An open PR fixes it | Wait, or review the PR |
| DONE_NOT_CLOSED | Its PR merged, the issue is still open | Close it, citing the PR |
| SUSPECT_CLOSE | Closed by a commit that only quoted "Fixes #N" | Check the fix landed; reopen if not |
| BLOCKED | Waiting on an open upstream issue, or on a spec PR not yet merged | Nothing, until it closes or merges |
| SPEC_REFUSED | The PR it waits on (its spec PR) closed without merging | Decide again: revise the spec in a new PR, postpone, or won't fix |
| UNBLOCKED | The upstream issue it waited on has closed, or its spec PR merged | Resume |
| POSTPONED | Labelled `postponed` | Skip it |
| REVISIT | Postponed before the newest stable release | Decide again |
| TRIAGED | Triaged, with nothing pending | Nothing |

## Keeping it running

`github-ship-watch` is the routine: each pass checks the release runs, the newest releases, and the issue intake, finishes what the policy already decided (a stalled lane, a flaky release job, the shipped notices), and hands flagged issues to triage when asked.

- `shipyard gate` on launchd: a new background session on this machine only when the repo needs one, with the prompt from the config's `[agents]` section; quiet ticks make no model call (shipyard-setup, The gate)
- `/loop 30m /github-ship-watch <owner/repo> — watch and triage`: every 30 minutes in this session
- `/schedule`: a cloud routine, with the prompt below that clones shipyard
- Status only: `python3 $S/github-ship-watch/scripts/watch_state.py <owner/repo>` (exit 1 when anything needs action)

To enable the plugin in every local session of a repo, commit this to its `.claude/settings.json`:

```json
{
  "extraKnownMarketplaces": {
    "shipyard": { "source": { "source": "github", "repo": "romamo/shipyard" }, "autoUpdate": true }
  },
  "enabledPlugins": { "shipyard@shipyard": true }
}
```

A cloud routine may not apply a repo's plugin keys ([plugins for organizations](https://code.claude.com/docs/en/plugins/org.md) lists where each surface reads them), so give the routine a prompt that doesn't depend on them:

```
Clone https://github.com/romamo/shipyard into tmp/shipyard (or pull it if present), then follow
tmp/shipyard/skills/github-ship-watch/SKILL.md for <owner/repo> — watch and triage
```

## Safeguards

- A change to a flag, format, default, public API, or stored state is designed in its issue first and checked against the repo's decisions log (`DECISIONS.md` or `docs/decisions.md`); what you settle is recorded there, so it isn't asked again
- A feature (new behaviour beyond a bug fix or a contract tweak) gets a spec first: a file `docs/specs/NNN-<slug>.md` with numbered acceptance criteria, merged through its own PR before any implementer starts. Merging the spec is your approval, and the issue reads BLOCKED until then
- Scope is read narrowly: "triage" never merges, and "merge" never tags, unless you say so
- A change that departs from a spec, breaks existing users, or belongs to a held PR stops and asks you, whatever the scope
- When another session works the same repo, the skills message it first, and your word in the current session wins
- Mark a held issue with a comment line like `On hold: <why>, decided in owner/repo#N`, or the `blocked` label, so the script can report BLOCKED and later UNBLOCKED
