# YYYY-MM-DD: <what users saw, in one line>

<!--
Copy this file to docs/postmortems/YYYY-MM-DD-<slug>.md, named for the day the incident
opened. The watch (github-ship-watch, POSTMORTEM_DUE) reads the Incident lines below on the
default branch: keep one line per incident this postmortem covers, exactly in this form, with
the real repo and issue number. A postmortem merges through a pull request like any change;
the maintainer's merge is the approval.
-->

Incident: owner/repo#N

## Summary

Two or three sentences: what broke, for whom, for how long, and how it ended.

## Timeline

All times UTC, from the incident issue's comments and the environment's deployment statuses.

| Time | Event |
|---|---|
| YYYY-MM-DD HH:MM | The release that carried the fault deployed (deployment status `success`) |
| YYYY-MM-DD HH:MM | The first failed `shipyard health` check |
| YYYY-MM-DD HH:MM | The incident issue opened |
| YYYY-MM-DD HH:MM | The rollback or the fix deployed |
| YYYY-MM-DD HH:MM | Healthy again; the incident closed |

## Cause

The fault and the change that introduced it (PR, commit, or release), and why the gates let it
through.

## What caught it

The check, alert, or person that noticed first, and how long after the fault shipped.

## What would have caught it sooner

The earliest point the fault was detectable (a test, a review rule, a health check, a bake
time), and why it wasn't there.

## Actions

Each action is either a rule or a piece of work, never both:

- A rule the team now follows is a decision: propose it here as `D-n` (the next number in
  docs/decisions.md), then record it with `decisions.py add` in its own pull request
- Any other action is an issue: link it (`owner/repo#N`) once it is open

| Action | Kind | Link |
|---|---|---|
| <the rule, as one sentence> | decision | D-n |
| <the work, as one sentence> | issue | owner/repo#N |
