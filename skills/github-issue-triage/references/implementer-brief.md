# Implementer brief

Send one brief per agent (at most two issues each, grouped by the files they touch), all in one message (`subagent_type: general-purpose`, `isolation: "worktree"`). Fill in the `{...}` fields. Name each issue's specific risks: the brief is the only context the agent gets.

```text
Repo {owner/repo} (remote origin), {one line: what the project is and its release phase}. You are in an isolated git worktree. Implement {one GitHub issue | two GitHub issues, each on its own branch and PR}, branched from the latest origin/{default} (`git fetch origin && git switch -c <branch> origin/{default}`). Other PRs merge while you work: right before pushing, `git fetch origin && git rebase origin/{default}` and re-run the checks.

Follow the github-issue-resolve skill's phases 2 to 6 (verify the claim on the current default branch, implement with a regression test, verify, commit, push, PR), with these overrides:
- Do NOT comment on, label, or close issues, and do NOT merge. Report back instead
- Before pushing, all of these must pass: {check_commands, with any env var the checks need}. {snapshot or regeneration notes, e.g. public-API snapshot, generated docs}
- {If the repo keeps a CHANGELOG: an entry under its unreleased heading, in the right subsection, ending with the issue ref `(#N)`. Omit this line otherwise}
- Commits: named paths only (`git commit -m "..." -- <paths>`), with a message ending {attribution line}
- PR body: summary, test notes, `Fixes #N`, ending {PR attribution line}
- Keep gh calls to a handful and never poll: the API rate limit is shared
- Rules: {rules_files, e.g. AGENTS.md}; {project rules in one line, e.g. uv run, fail fast, no monkeypatching}
- Settled decisions this change must respect: {the output of `decisions.py find` for the touched paths and areas, or "none recorded"}. Departing from one, or from the design below, is a "decision for you": stop and report it, don't ship it
- {For a spec's build issue:} The spec is `docs/specs/{NNN}-{slug}.md`, merged. Acceptance criteria this issue delivers: {the output of `specs.py criteria NNN`, or the ids this issue covers}. Each one needs a test that proves it and names its id: `test_sNNN_k_...` (Go `TestSNNN_k...`), or a `proves: S-NNN-k` comment line above the test; `specs.py coverage --spec NNN` must list one for each. {For the spec's last open build issue: also follow spec-gate.md's "Verify the whole spec": set `status: built`, fill in its Issues and Verification sections, and run `specs.py check` and `specs.py coverage`.} Departing from a criterion, or finding one that can't hold, is a "decision for you"

Issue #{N} (branch `{fix|feat}/{N}-{slug}`): {the problem in two sentences, with the repro}. {The agreed plan from the triage comment, including its Design section for a contract change}. {Risks: the paths that must not change, every input path to cover, platform-only branches, docs and specs that name old values}. {Out of scope: what is deferred}.

Final report: each PR URL, 2 to 3 lines on each, and a "Decisions for you" list naming every behaviour change, spec deviation (with its `S-NNN-k` id), public-API change, and anything you couldn't verify.
```

## Risks worth naming, by issue type

| Issue concerns | Tell the implementer |
|---|---|
| A default or limit | Every doc, test, and spec that names the old value; whether the spec fixes the default |
| An input type | Every input path (argv, JSON payload, exec lines, MCP, settings, defaults); refusals for malformed values; the schema matching the parser |
| An output or schema change | Whether existing schema locks or snapshots break. Additive only, unless the user agreed |
| An audit or lint rule | False positives on the project's own code and examples; suggested fixes that registration would itself reject |
| A Windows-only race | A retry helper with injected clock and sleep, so it's testable on any OS, and a catch only on the narrow error. The PR's Windows CI job is the real test |
| Logging or redaction | Concurrent callers and threads; library loggers; nothing unredacted reaching stderr under any verbosity |
