# Design gate and decisions log

Implementers are good at writing code and bad at staying consistent across thirty PRs written by thirty agents. The gate settles the design once, in the issue, before any agent starts; the log keeps what the user decided so the next design and the next review start from it.

## When an issue goes through the gate

An **implement** verdict goes through the gate when the change touches a contract other people rely on:

- A command, flag, argument, exit code, or env var name
- An output, wire, or file format, or a schema version
- A default or a limit
- A public API (exported names, signatures, raised errors)
- Stored state that an upgrade must read
- A new dependency, or a new module that other modules will call

A bug fix that restores documented behaviour skips the gate. So does an internal refactor with no visible change.

## The gate

1. **Read what's settled.** `decisions.py find <the paths and areas the change touches>`, plus the spec sections the issue names. A repo without a log has nothing settled yet; the first decision creates it
2. **Write the design in the triage comment**, under the plan (see the Implement template in [comments.md](comments.md)):
   - The contract change, exactly: the new flag, the field and its type, the default
   - At most two alternatives, and why this one
   - Compatibility: who breaks, and the migration or deprecation
   - The decisions it relies on (`D-n`), and how it will be tested
3. **Decide who decides.** Dispatch without asking only when the design follows from the spec and the log. Ask with AskUserQuestion, before dispatch, when:
   - it departs from a `D-n` entry or the spec
   - two designs are both reasonable and users would see the difference
   - it breaks existing users

   Put your recommendation first. Until the answer arrives, the verdict comment says **implement** with the open question, and no agent starts on it
4. **Hand the design to the implementer** as the agreed plan in its brief. A change of design during implementation is a "decision for you", never a quiet deviation

## Recording a decision

Record when the user settles something that later work must respect: an answer to a gate question, to a PR's "decisions for you", or to a **clarify** question about intended behaviour. Don't record what the code already makes obvious, or a one-off call about a single PR.

```bash
uv run --no-project python <skill>/scripts/decisions.py add \
  --title "Representation is --format" \
  --rule "Output representation is chosen with --format; no command adds --output for it" \
  --why "--output names a destination file in the spec; one meaning per flag" \
  --applies "src/cli/*, CLI flags" --enforced "review" --source owner/repo#123
```

- **Rule** is one sentence a reviewer can check a diff against. "Prefer clarity" isn't a rule
- **Why** lets a later change tell whether the rule still holds when the situation changes
- **Applies to** uses paths or globs where it can, so `find` on a diff's file list returns it
- **Enforced by** names the test or lint that fails on a violation, or "review" until one exists
- **To change a rule, supersede it** (`--supersedes D-n`) with the user's answer as the source. Never edit an old entry's rule

**One decisions PR per pass.** Ids are sequential, so two PRs that each add an entry both take the same `D-n` and collide when the second merges. The orchestrating session records every decision of a pass in one docs PR of its own, opened before it dispatches implementers, and gives each implementer the rule's text and id in its brief; implementer agents never edit the log. Before opening it, check for an open PR that already edits the log, by the files it changes rather than its title (a title like "Record D-1 and D-2" never says "decisions"):

```bash
gh pr list --state open --json number,title,files \
  --jq '.[] | select(any(.files[]; .path == "DECISIONS.md" or .path == "docs/decisions.md")) | "#\(.number) \(.title)"'
```

If there is one, add to that PR rather than starting a second. Run `decisions.py check` before pushing; github-pr-triage runs it on review too.
