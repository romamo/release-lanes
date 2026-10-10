# S-017: Chains of work across items, repos and people

status: built

## Problem

Work that spans several items, repos and people has no chain shipmill can follow, so it
stops at the first hand-off (#334, accepted by the maintainer on 2026-10-10). In the
maintainer's words: "an external executor is the same as an agent, and an external task's
completion must also be tracked and start the next task in the chain".

What breaks today:

- **Links are read only from text.** `skills/github-issue-triage/scripts/triage_state.py`
  holds an issue on a `blocked` label, a hold sentence in a comment, or a body line
  starting "Depends on". A GitHub issue dependency (`blockedBy`) or a sub-issue set in the
  UI holds nothing. #71 writes sub-issues when it splits a spec, and nothing reads them
  back
- **A person's item has no lifecycle.** UNBLOCKED always means "comment implement and
  dispatch". An item only a person can do (a server change, a key rotation, a payment)
  needs the opposite: tell the person it is their turn, then check their result and close
  it, which unblocks the next item
- **A parent has no state.** Outside the spec gate's feature issue, nothing says a parent
  item is done when its last child closes, or shows how far its children got
- **A chain that crosses into a repo without a gate stops there.** Each repo's triage
  already reads `owner/repo#N` holds, so a downstream repo with a gate sees UNBLOCKED on
  its next pass; one without a gate never does, and nothing says so

The case that surfaced it: in repo A an agent builds a page; in repo B (server config) a
PR removes the rule that hides it; in repo A the maintainer merges that PR and reloads the
web server (a person's item); in repo C a page starts linking to the new one. Nothing told
the maintainer when step 1 shipped, nothing checked step 3 or closed it, and nothing
started step 4.

## Behaviour

### Native relations count as holds

`triage_state.py` reads, for every open issue, its GitHub `blockedBy` issues and its
`subIssues`, each with its repository, number, `state`, and `stateReason`, in the same
query that reads its comments, paging past 50 of either like it pages comments. Each one
holds the issue exactly as a `Depends on owner/repo#N` body line naming the same issue
does: BLOCKED while it is open, UNBLOCKED once every hold has closed, and
`owner/repo#N:not_planned` in the note for one closed as not planned. An untriaged issue
still reads NEW whatever holds it, as it does for a body line today.

Text and native relations are one set: an issue named both ways counts once, and neither
form overrides the other. A relation the UI set and no text names holds the issue; a text
line with no native relation holds it too. The note marks a hold only the forge records
with `(native)` and a sub-issue with `(child)`, so `owner/repo#12:open(child)`.

A repo or token that can't read the fields (GitHub Enterprise Server without issue
dependencies, an error naming `blockedBy` or `subIssues`) makes `triage_state.py` read
text holds only and print one `note: native relations unavailable: <first error line>`
line to stderr; every other gh failure exits 2 as today. GitLab's linked issues and epics
come through the code host port (#322) later, behind the same reading.

### `chains.py link` writes both forms

A new stdlib script, `skills/github-issue-triage/scripts/chains.py` (Python 3.10+, run
with `uv run --no-project python` like the other triage scripts), writes a link through
`gh`:

- `chains.py link <owner/repo#N> --blocked-by <owner/repo#M>` adds the native relation
  (GraphQL `addBlockedBy`, issue N blocked by M) and, unless N's body already names M on a
  `Depends on` line, appends `Depends on owner/repo#M` to it, so a reader of the issue
  and a forge without the relation both see it
- `chains.py link <owner/repo#P> --child <owner/repo#C>` adds C as a sub-issue of P
  (`addSubIssue`) and appends `Depends on owner/repo#C` to P's body the same way

Running it again changes nothing and exits 0 (`already linked`). It exits 2 naming the
reference for a malformed `owner/repo#N`, an issue that doesn't exist or is a pull
request, and N equal to M. When the forge refuses the native relation (no dependencies
or sub-issues there, or a cross-owner link it doesn't allow), it still writes the text
line, prints `text only: <first error line>`, and exits 0: the text line holds the issue
by itself. In a gate session `gh` already writes as the App (D-14), so the link and the
body edit read as shipmill's.

The spec gate's split (`skills/github-issue-triage/references/spec-gate.md`, Split it into
build issues) uses `chains.py link` instead of the GraphQL snippet: `--blocked-by` for each
`Depends on` line `specs.py split` printed, and `--child` to put each build issue under the
feature issue. A relation a person sets in the UI is never copied into text: both are read.

### A person's item

An open issue labelled `human` is a person's item: its assignees do it, and no implementer
is dispatched for it. It counts as triaged with or without a triage comment, so its body's
`Depends on` lines and native holds apply from the start. Its `## Check` section, when it
has one, is how its result is checked: a command or a URL with what it must return.

`triage_state.py` reads a person's item, after the needs-decision and UNTRUSTED states
(D-16, D-21) and the holds, as one of:

| State | Means | Action |
|---|---|---|
| BLOCKED | Some hold is open, as for any issue | No |
| HANDOFF_DUE | Nothing holds it (or its last hold closed), and no hand-off comment came after its newest hold closed | Yes |
| WITH_PERSON | A hand-off came after its newest hold closed, and no done report followed it | No |
| VERIFY_DUE | A done report came after the newest hand-off | Yes |
| NO_ASSIGNEE | Labelled `human` with no assignee | Yes |

A **hand-off** is a comment whose first line is `<!-- shipmill:handoff -->`, written by the `--bot-login` or a
trusted author (OWNER, MEMBER, COLLABORATOR; D-16): a marker comment by anyone else never
counts. A **done report** is a comment whose first line starts with `done` (any case) by
one of the item's assignees or a trusted author. An assignee need not be a collaborator:
a trusted person chose them, and the session acts on the trusted author's `## Check`, never
on the report's text.

The session that takes up each action (github-issue-triage's SKILL.md, the state table in
`references/triage-rubric.md`, and the comments in `references/comments.md`):

- **HANDOFF_DUE:** posts the hand-off: the marker, a mention of every assignee, which
  holds closed (`owner/repo#N`), what to do (the issue's steps, quoted, never rewritten),
  the `## Check` when there is one, and "Reply `done` here when it's finished, or close
  the issue". GitHub's mention is the notification; a mention from the App reaches the
  maintainer (D-19). shipmill never mentions the person again while the item waits on
  them: WITH_PERSON is report-only, and ship-watch lists it with who it waits on
- **VERIFY_DUE:** runs the `## Check`. It holds: a comment saying what was checked and what
  came back, then closes the issue as completed, which unblocks whatever it held. It fails:
  a new hand-off saying what failed and what came back, so the item reads WITH_PERSON again.
  A check the session's tools can't run (a host it can't reach, a command outside its
  allowlist) goes to the maintainer through the needs-decision protocol (D-21), the
  options being "close it on the report" and "it isn't done". With no `## Check`, the
  session closes the issue on the report, saying so
- **NO_ASSIGNEE:** asks the maintainer who does it, through the needs-decision protocol
  (D-21), so the item waits on the answer instead of being taken up on every tick

A person may also close their item themselves. For the `--closed N` most recently closed
issues, `triage_state.py` reads a `human` issue closed as completed by someone other than
the `--bot-login`, with a `## Check` section and no `<!-- shipmill:verified -->` comment
after its close, as **VERIFY_CLOSED** (an action, never SUSPECT_CLOSE). The session runs
the check: it holds, a `<!-- shipmill:verified -->` comment with what came back; it fails,
the session reopens the issue with a hand-off saying what failed, and every item the issue
holds reads BLOCKED again.

`watch_state.py`'s `TRIAGE_ACTION` gains HANDOFF_DUE, VERIFY_DUE, NO_ASSIGNEE,
and VERIFY_CLOSED, so the gate starts a session for them; WITH_PERSON is report-only and
listed in ISSUES_OPEN with the person it waits on. `triage_state.py` exits 1 on the new
action states, and `--wip N` never counts a person's item as in progress or ready: the
limit is on agents' work. Setup creates the `human` label wherever it creates
`needs-decision` (`skills/shipmill-setup/scripts/setup_state.py`).

### A parent and its progress

A parent is an issue with sub-issues, in its repo or another. Its children hold it like
any native relation, so it reads BLOCKED while a child is open and UNBLOCKED once the last
closes. Its note starts with its progress, `children 3/5 closed`, then lists each child as
`owner/repo#N:<state>(child)`, across repos. On UNBLOCKED with only children as holds,
the session closes the parent with a comment listing each child and how it closed; a
feature issue first has its spec checked as today (spec-gate.md, Verify the whole spec).
A child closed as not planned is listed as such and doesn't stop the close.

### Chains across repos

Each repo advances its own part: its triage reads its items' holds in any repo (text and
native), and its gate takes up what turns UNBLOCKED or HANDOFF_DUE there, checking D-16 on
its own items. An upstream item's text is never read, only its state, so a chain never
lets one repo's text steer a session in another.

`chains.py show <owner/repo#N>` prints the chain an item belongs to: from N, every issue
it waits on (text and native, recursively) and every issue that waits on it through a
native `blocking` relation or as its parent, recursively, each once. One row per item:
the reference, its title, its executor (`agent`, or `person: @login,...` for a `human`
item), and its state: CLOSED, BLOCKED (with the open holds), or READY. A READY item whose
repo's `.github/shipmill.toml` on the default branch has no `[agents]` table, or has no
such file, gets NO_GATE in a last column: nothing will take it up. The command exits 1
when any row is NO_GATE, 0 otherwise, and 2 on a malformed reference or a gh failure;
`--json` prints the rows as one JSON object. A repo it can't read (no access) is one row,
`owner/repo#N UNREADABLE`, with the chain continuing past what it could read.
github-ship-watch reports NO_GATE rows of the chains its repo's open items belong to as
an action row, CHAIN_NO_GATE, naming the item and the repo.

### Docs

`skills/github-issue-triage/SKILL.md` gains a Chains section: the two link forms and
`chains.py link`, the `human` label and its states, parents, and `chains.py show`.
`docs/flow.md` shows a person's item between two agents' items.

## Acceptance criteria

- S-017-1: an open, triaged issue with a native `blockedBy` issue that is open reads BLOCKED, and UNBLOCKED once that issue closes, with no text naming it
- S-017-2: an open, triaged issue with an open sub-issue reads BLOCKED, and UNBLOCKED once its last sub-issue closes, and its note starts with `children K/N closed`
- S-017-3: an issue named both by a `Depends on` line and a native relation is listed once in the note; one named only natively carries `(native)`, a sub-issue `(child)`, and one closed as not planned `:not_planned`
- S-017-4: an untriaged issue that is not labelled `human` reads NEW whatever native relations hold it
- S-017-5: when gh refuses the `blockedBy` or `subIssues` fields, `triage_state.py` classifies on text holds alone, prints one `note: native relations unavailable:` line to stderr, and doesn't exit 2
- S-017-6: `chains.py link A --blocked-by B` adds the native relation and a `Depends on B` line to A's body; run again it changes nothing, prints `already linked`, and exits 0
- S-017-7: `chains.py link P --child C` adds C as a sub-issue of P and a `Depends on C` line to P's body
- S-017-8: when the forge refuses the native relation, `chains.py link` still writes the text line, prints `text only:` with the error's first line, and exits 0
- S-017-9: `chains.py link` exits 2 naming the reference for a malformed reference, a missing issue, a pull request, and an issue linked to itself
- S-017-10: an open `human` issue with an assignee, no triage comment, and no open hold reads HANDOFF_DUE, and BLOCKED while a `Depends on` line or a native relation names an open issue
- S-017-11: a `human` issue with a hand-off after its newest hold closed and no done report after the hand-off reads WITH_PERSON, which is not an action, however old the hand-off is
- S-017-12: a comment starting `done` by an assignee or a trusted author after the newest hand-off makes a `human` issue read VERIFY_DUE; one by anyone else changes nothing
- S-017-13: a hand-off or verified marker comment by an author who is neither trusted nor the `--bot-login` is ignored
- S-017-14: a `human` issue with no assignee reads NO_ASSIGNEE
- S-017-15: a `human` issue closed as completed by someone other than the `--bot-login`, with a `## Check` section and no verified comment after its close, reads VERIFY_CLOSED, never SUSPECT_CLOSE; with a verified comment after the close it reads neither
- S-017-16: `triage_state.py` exits 1 when an issue reads HANDOFF_DUE, VERIFY_DUE, NO_ASSIGNEE, or VERIFY_CLOSED, and `--wip N` counts no `human` issue as in progress or ready
- S-017-17: `watch_state.py` counts HANDOFF_DUE, VERIFY_DUE, NO_ASSIGNEE, and VERIFY_CLOSED as triage actions and lists WITH_PERSON among the open issues with the assignees it waits on
- S-017-18: `chains.py show X` lists every item X waits on and every item that waits on X natively or as its parent, recursively and each once, with its executor and its state
- S-017-19: `chains.py show` marks a READY item NO_GATE when its repo's `.github/shipmill.toml` has no `[agents]` table or doesn't exist, and exits 1 then; `--json` prints the same rows as one JSON object
- S-017-20: github-ship-watch reports a CHAIN_NO_GATE action row naming the item and its repo for a NO_GATE item in a chain one of its repo's open items belongs to
- S-017-21: `setup_state.py` reports the `human` label missing wherever it reports `needs-decision` missing, and setup creates both
- S-017-22: github-issue-triage's SKILL.md, triage-rubric.md, comments.md, and spec-gate.md document the link forms and `chains.py link`, the `human` label with each state and the session's action for it (the hand-off's contents, verification, the needs-decision fallback), and parents; `docs/flow.md` shows a person's item in a chain
- S-017-23: a chain of four items across three repos, the third a `human` item, advances to its end with the person acting only on the third item: the second turns READY when the first closes, the person is mentioned when the second closes, their done report is checked and their item closed by a session, and the fourth is taken up after that

## Out of scope

- GitLab linked issues and epics: through the code host port (#322, spec 016) once it lands; text links work there already
- Milestones tied to releases and human items counting toward a milestone: #70 and the product-layer work
- A fleet pass that drives repos without a gate: `chains.py show` and CHAIN_NO_GATE say where a chain stalls; installing a gate there is the fix (shipmill-setup)
- Copying a UI-set relation into text, or text into a relation outside `chains.py link`: both forms are read, so neither needs the other
- Reminders: the maintainer chose one hand-off mention per unblock (#335); a repeat mention can come later behind its own option
- Desktop notifications for a person's item: GitHub's mention notifies; the gate's desktop notice stays for a waiting session (spec 003)
- Payment, credential, or other secret-bearing steps done by an agent: a `human` item keeps them with the person

## Decisions relied on

- D-5
- D-6
- D-10
- D-14
- D-16
- D-19
- D-21

## Issues

- shipmill/shipmill#340: S-017-1, S-017-2, S-017-3, S-017-4, S-017-5
- shipmill/shipmill#341: S-017-6, S-017-7, S-017-8, S-017-9
- shipmill/shipmill#342: S-017-10, S-017-11, S-017-12, S-017-13, S-017-14, S-017-15, S-017-16
- shipmill/shipmill#344: S-017-17, S-017-21
- shipmill/shipmill#343: S-017-18, S-017-19, S-017-20
- shipmill/shipmill#345: S-017-22, S-017-23

## Verification

Checked on main at ef6cd76 plus #345's PR: each criterion's code read against its text, and
the tests `specs.py coverage --spec 017` lists for it run there and passing.

- S-017-1: `native_holds` and `classify_open` in `triage_state.py` read `blockedBy` as a hold with no text; tests/test_triage_native.py's S-017-1 tests pass
- S-017-2: `classify_open` counts sub-issues as `(child)` holds and starts the note with `children K/N closed`; test_s017_2_* pass
- S-017-3: holds are keyed case-folded, so text and native merge; the note marks `(native)`, `(child)`, `:not_planned`; test_s017_3_* pass, and the S-017-23 chain's notes show `acme/srv#2:closed` once
- S-017-4: `held` is false for an untriaged non-human issue, so `native_holds` isn't read; test_s017_4 passes
- S-017-5: `fetch` retries on `TEXT_QUERY` after `NativeUnavailable` with one stderr note, and `chains.py show` likewise; the S-017-5 tests in test_triage_native.py and test_chains_show.py pass
- S-017-6: `chains.py link` writes each missing half and prints `already linked` when both exist; test_s017_6_* pass, and the S-017-23 chain links acme/site#4 twice, the second printing `already linked`
- S-017-7: `link --child` runs `addSubIssue` and appends the line; test_s017_7 passes
- S-017-8: a refusal (undefinedField, UNPROCESSABLE, FORBIDDEN) still writes the line and prints `text only:`; test_s017_8_* pass
- S-017-9: `parse_ref`, `resolve`, and the self-link check exit 2 naming the reference before any write; test_s017_9 passes
- S-017-10: a `human` issue counts as triaged and reads HANDOFF_DUE once every hold closed, BLOCKED before; test_s017_10_* pass, and the S-017-23 chain's acme/app#3 reads BLOCKED, then HANDOFF_DUE when acme/srv#2 closes
- S-017-11: `person_state` dates hand-offs against the newest hold's close and reads WITH_PERSON, not in `ACTION`; test_s017_11_* pass, and the S-017-23 chain stays WITH_PERSON with every repo exiting 0 over three ticks
- S-017-12: `done_report` accepts an assignee or a trusted author only; test_s017_12_* pass, and the S-017-23 chain's done report by a non-collaborator assignee reads VERIFY_DUE
- S-017-13: `by_trusted` gates both markers; test_s017_13_* pass
- S-017-14: NO_ASSIGNEE is read before any hold; test_s017_14 passes
- S-017-15: `classify_closed` sends a `human` close to `verify_closed`, never SUSPECT_CLOSE; test_s017_15_* pass, and the S-017-23 chain's session close reads neither
- S-017-16: `ACTION` holds the four action states and `wip_room` skips `human` issues; test_s017_16_* pass
- S-017-17: `watch_state.py`'s `TRIAGE_ACTION` holds the four and ISSUES_OPEN lists WITH_PERSON with its assignees; test_s017_17_* pass
- S-017-18: `walk` follows holds up and `blocking`/`parent` down, each once; test_s017_18_* pass, and the S-017-23 chain shows all four items from its first and from its third
- S-017-19: `show_rows` reads each READY item's repo config once and marks NO_GATE, exit 1; test_s017_19_* pass
- S-017-20: `watch_state.py`'s `chain_rows` runs `chains.py show --trusted-only` and reports CHAIN_NO_GATE; the S-017-20 tests pass
- S-017-21: `setup_state.py` wants `human` wherever it wants `needs-decision`, and shipmill-setup's SKILL.md creates both; test_s017_21 passes
- S-017-22: github-issue-triage's SKILL.md has a Chains section; triage-rubric.md has Links between issues and A person's item with each state and its action; comments.md has the hand-off, verification, needs-decision, and parent's-close comments; spec-gate.md links build issues with `chains.py link`; docs/flow.md has A chain across repos and people; test_s017_22 passes
- S-017-23: the real `triage_state.py` (through a stand-in gh) and `chains.py` (through its runner) over one scripted forge of three repos drive the four-item chain to its end, the person posting one comment; test_s017_23 passes
