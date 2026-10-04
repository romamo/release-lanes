# Opportunity issues and intake comments

Write each body to a file in the repo's `tmp/` and post it with `--body-file`: backticks inside a double-quoted zsh `--body` run as commands. Follow the user's writing rules: no trailing periods on list items, em dashes only rarely.

## The opportunity issue

Title: the outcome the users want, in their words, as a short phrase ("Open reports in a spreadsheet"), never a solution unless every request names the same one.

```markdown
## Problem

{What the users can't do today, quoting the requests: "{quote}" (#{n}).}

## Who is affected

{The kinds of users the requests come from, and how often they meet the problem, as far as the requests say.}

## Evidence

- #{n} {request title}, +1 {count}
- https://github.com/{owner}/{repo}/discussions/{n} {discussion title}, +1 {count}

{N} requests, +1 {total} as of {date}.

## Success

{What a user can do once this ships, checkable: "a report opens in a spreadsheet with one column per field".}

## Notes

{Solutions the requests propose, the D-n rules it runs into, related opportunities. Optional.}
```

`intake_state.py` reads members only from the Evidence section, so link every request there and nowhere else counts. Other sections may link issues freely.

```bash
gh issue create -R {owner/repo} --title "{outcome}" --label opportunity --body-file tmp/opportunity-{slug}.md
```

## Intake's triage line on the opportunity

```markdown
Triage: **opportunity**. {N} requests ask for this outcome; it waits for the maintainer's call: accept with the `planned` label, or decline by closing it as not planned with the reason in a comment. Once accepted, it goes through the spec gate as a feature.
```

Never use the word "implement" here: `triage_state.py` reads the newest triage comment's verdict from it.

## On each grouped request

```markdown
Triage: **opportunity**: grouped into #{opportunity} with {N-1} other requests for the same outcome. Follow that issue for the maintainer's decision{; this one is a pure duplicate, so it carries the `duplicate` label}.
```

## On a request for a declined outcome

```markdown
Triage: **opportunity**: this asks for the same outcome as #{opportunity}, which was declined: "{the recorded reason}". It is linked there as further evidence; the maintainer reopens #{opportunity} if the decision changes.
```

## On a request whose outcome shipped

```markdown
Triage: **opportunity**: this outcome shipped through #{opportunity}{, released in {version}}. If something is still missing, say what, and it will be grouped as a new request.
```
