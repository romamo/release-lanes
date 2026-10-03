# Reviewer brief

Fill in the `{...}` fields and send one brief per PR, all in one message so the agents run in parallel (`subagent_type: general-purpose`, `isolation: "worktree"`). Don't hand the reviewer a verdict; hand it the PR's specific risks.

## Open PR

```text
Review GitHub PR #{N} of {owner/repo} ("{title}", fixes #{issue}). The repo is at {repo_path}, and you are in an isolated worktree of it. Goal: a merge-or-refuse verdict. Do NOT push, comment on GitHub, or merge.

{context: what the PR claims, what it's stacked on, whether the default branch has moved, and any peer session working near it (don't touch its worktree)}

1. `git fetch origin && git fetch origin pull/{N}/head:review-{N} && git checkout review-{N}`. Rebase onto origin/{default} locally and report which files conflicted and how you resolved each:
   - CHANGELOG: keep every entry from both sides, and check that new entries sit under Unreleased, not inside a released section
   - A count or version both sides changed: recompute it from each side's baseline and confirm by running the test
2. Run the project's checks in the foreground (timeout 600000 ms): {check_commands}. Known flaky tests: {flakes}. For any other failure, say whether it also fails on origin/{default}.
3. Read `git diff origin/{default}...HEAD` and the linked issue (`gh issue view {issue}`). Look for:
   - Correctness: {pr_specific_risks, for example: edge-case inputs to test, concurrent callers, platform-only paths, and the cases that must NOT change}
   - Security: secrets reaching output, logs, error messages, or another concurrent caller's output; input validation on every entry path
   - Whether it resolves the issue fully or only partially
   - The repo's rules in {rules_files, e.g. AGENTS.md, CLAUDE.md}
   - Whether CHANGELOG, README, and docs agree with the code
4. Reproduce each real bug before fixing it. Commit each small fix with a regression test that fails on the old code, on review-{N}, and rerun the affected checks. Leave larger problems as findings.
5. Final report, under 250 words: verdict (MERGE / MERGE AFTER SMALL FIX / REFUSE), rebase status, test results, findings with file:line (fixed or not), and the HEAD SHA of review-{N}.
```

## Post-merge review

Use this for a PR that merged before anyone reviewed it. Same shape, but the branch starts from the default branch:

```text
Post-merge review of GitHub PR #{N} of {owner/repo} ("{title}"). It is already merged. Your job is to find bugs the merge shipped. Do NOT push, comment on GitHub, or open PRs.

1. `git fetch origin && git checkout -B review-{N} origin/{default}`. Find what landed: `gh pr view {N} --json commits,mergeCommit,body`, plus any follow-up commits touching the same code ({follow_ups}).
2. {pr_specific_risks}. Also check it composes with PRs that merged next to it and touched the same code ({neighbours}).
3. Run the project's checks, then fix each real bug on review-{N} with a regression test (the CHANGELOG entry goes under Unreleased → Fixed).
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
