# Reviewer brief

Fill in the `{...}` fields and send one brief per PR, all in one message so the agents run in parallel (`subagent_type: general-purpose`, `isolation: "worktree"`). Don't hand the reviewer a verdict; hand it the PR's specific risks.

## Open PR

```text
Review GitHub PR #{N} of {owner/repo} ("{title}", fixes #{issue}). The repo is at {repo_path}, and you are in an isolated worktree of it. Goal: a merge-or-refuse verdict. Do NOT push, comment on GitHub, or merge.
{In a headless gate session:} Run one allowlisted command per Bash call: `git -C <dir>`, not `cd <dir> &&`; no variable assignments, `;` or `&&` chains, or pipes into tools not on the list. A chained call is denied whole when any part isn't on the list; rerun its commands as separate calls. A command that isn't on the list at all is a finding: stop that step and report it (github-issue-triage's needs-decision.md, A denied tool call is a decision).

{context: what the PR claims, what it's stacked on, whether the default branch has moved, and any peer session working near it (don't touch its worktree)}

1. `git fetch origin`, `git fetch origin pull/{N}/head:review-{N}`, then `git checkout review-{N}`. Rebase onto origin/{default} locally and report which files conflicted and how you resolved each:
   - CHANGELOG: keep every entry from both sides, and check that new entries sit under Unreleased, not inside a released section. {When `.github/shipmill.toml` sets `[changelog] fragments`: the PR adds its entry as a new fragment in that folder and leaves `CHANGELOG.md` alone; `changelog_guard.py check --base origin/{default}` must exit 0, and on a line added under Unreleased move the entry into the fragment it names}
   - A count or version both sides changed: recompute it from each side's baseline and confirm by running the test
2. Run the project's checks in the foreground (timeout 600000 ms): {check_commands}. Known flaky tests: {flakes}. For any other failure, say whether it also fails on origin/{default}.
3. Read `git diff origin/{default}...HEAD` and the linked issue (`gh issue view {issue}`). Look for:
   - Correctness: {pr_specific_risks, for example: edge-case inputs to test, concurrent callers, platform-only paths, and the cases that must NOT change}
   - Security: secrets reaching output, logs, error messages, or another concurrent caller's output; input validation on every entry path
   - Whether it resolves the issue fully or only partially
   - The repo's rules in {rules_files, e.g. AGENTS.md, CLAUDE.md}
   - The settled decisions the diff touches: run `{decisions_script} find $(git diff --name-only origin/{default}...HEAD)` and check the diff against each Rule. A departure is a finding unless the PR records a superseding entry the user approved. Also run `{decisions_script} check` if the PR edits the log
   - Whether the code matches the design in the issue's triage comment, for a contract change
   - For a PR that builds a spec (its issue links `docs/specs/NNN-<slug>.md`): run `{specs_script} coverage --spec NNN` and read each test it lists. Check the test really asserts its criterion: the script finds names, you judge substance. A criterion this PR delivers with no test, or a test whose assertions don't match its criterion's statement, is a finding. If the PR moves the spec to `status: built`, check that its Verification section says how each criterion was checked on the code, not just that the tests pass, and run `{specs_script} check`
   - Whether CHANGELOG, README, and docs agree with the code
4. Reproduce each real bug before fixing it. Commit each small fix with a regression test that fails on the old code, on review-{N}, and rerun the affected checks. Leave larger problems as findings.
5. Final report, under 250 words: verdict (MERGE / MERGE AFTER SMALL FIX / REFUSE), rebase status, test results, findings with file:line (fixed or not), and the HEAD SHA of review-{N}.
```

## Post-merge review

Use this for a PR that merged before anyone reviewed it. Same shape, but the branch starts from the default branch:

```text
Post-merge review of GitHub PR #{N} of {owner/repo} ("{title}"). It is already merged. Your job is to find bugs the merge shipped. Do NOT push, comment on GitHub, or open PRs.

1. `git fetch origin`, then `git checkout -B review-{N} origin/{default}`. Find what landed: `gh pr view {N} --json commits,mergeCommit,body`, plus any follow-up commits touching the same code ({follow_ups}).
2. {pr_specific_risks}. Also check it composes with PRs that merged next to it and touched the same code ({neighbours}).
3. Run the project's checks, then fix each real bug on review-{N} with a regression test (the CHANGELOG entry goes under Unreleased → Fixed, or in a new fragment under `### Fixed` when `.github/shipmill.toml` sets `[changelog] fragments`).
4. Final report, under 250 words: CLEAN / BUGS FOUND, findings with file:line, test results, HEAD SHA.
```

## Risks worth naming, by PR type

| PR changes | Ask the reviewer to check |
|---|---|
| Parsing or input types | Every input path (argv, JSON payload, stdin lines, tool calls, config and env, defaults); malformed values refused; schema matches what the parser accepts |
| Logging, errors, redaction | Secrets across concurrent callers and threads; secrets in the serialized form of a value object |
| Retry, locking, files | Platform-only branches; masking real errors; a no-op on other platforms; readers as well as writers |
| Audit or lint rules | False positives on already-compliant code; false negatives in nested types; suggested fixes that would themselves be rejected |
| Defaults or limits | Upgrade path for existing state; every doc and spec that names the old value |
