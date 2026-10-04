# 2026-10-04: github-pages reported the wrong version for two minutes (planned rollback drill)

Incident: romamo/shipyard#81

## Summary

A planned drill for #53 deployed v0.10.1 to the github-pages environment with `simulate-failure`, so `health.json` reported `0.0.0-simulated-failure` instead of the deployed version. `shipyard operate` failed three checks in a row, rolled github-pages back to v0.10.0, opened #81, and held the stable lane while it was open. The site served the wrong version for about two minutes; nothing else was affected.

## Timeline

| Time | Event |
|---|---|
| 2026-10-04 21:24 | v0.10.1 released; `land` deployed it to github-pages, healthy |
| 2026-10-04 21:28 | A first drill deploy was dispatched without `--ref`, so GitHub recorded the deployment as `ref=main`; operate can't read a tag from that, so the drill restarted |
| 2026-10-04 21:28 | `deploy.yml --ref v0.10.1 -f simulate-failure=true` dispatched |
| 2026-10-04 21:29 | The faulty `health.json` live |
| 2026-10-04 21:30 | First failed `shipyard health` check (operate runs started by hand to keep the drill short) |
| 2026-10-04 21:31 | Third failed check: #81 opened, rollback to v0.10.0 dispatched |
| 2026-10-04 21:31 | v0.10.0 live again |
| 2026-10-04 21:32 | operate found github-pages healthy and commented on #81 |
| 2026-10-04 22:0x | While #81 was open, the planner held the stable lane ("0.11.0 held, open 'incident' issues: #81"); #81 closed, releasing the hold |

## Cause

Deliberate: the drill's `simulate-failure` input. v0.10.1 itself was fine. The drill checked that a deploy whose health check fails is rolled back without a person, that the incident is recorded, and that releases wait while it is open.

## What caught it

`shipyard operate`'s health check, which compares the version in `health.json` with the deployed tag. It failed on the first check after the faulty deploy went live.

## What would have caught it sooner

Nothing earlier was meant to: the fault was injected after the release gates on purpose. The time to rollback is what matters. On the 10-minute schedule alone, three failed checks take about 30 minutes; the drill took under 2 minutes only because operate was started by hand after each step. A health check started by the deploy itself would close most of that gap (#84).

## Actions

| Action | Kind | Link |
|---|---|---|
| deploy.yml refuses a run whose ref isn't the tag it deploys, so a hand-started deploy can't record `ref=main` | issue | romamo/shipyard#80 |
| A hand-closed incident doesn't read SUSPECT_CLOSE in triage_state | issue | romamo/shipyard#83 |
| Check an environment right after its deploy instead of waiting for the next scheduled operate run | issue | romamo/shipyard#84 |
