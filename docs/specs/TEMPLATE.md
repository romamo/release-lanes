# S-NNN: <title>

status: draft

## Problem

Who needs this and what goes wrong without it, in a few sentences. Link the issue it came
from.

## Behaviour

What the feature does, as users and other tools see it: commands, flags, outputs, files,
defaults, and errors. Name every path or area it touches in backticks, such as
`src/pkg/cli.py` or `CLI flags`, so `specs.py find` lists this spec for a change there.

## Acceptance criteria

Each criterion is one checkable statement with its own id, numbered from 1. A test that
proves one names the id.

- S-NNN-1: <a statement a test can check, e.g. "tool run exits 2 when the config is missing">

## Out of scope

- <what this spec deliberately leaves out, and where it went>

## Decisions relied on

- none

## Issues

The build issues, filled in once they are filed: one `- owner/repo#N: S-NNN-1, S-NNN-2` per
line, naming the criteria that issue delivers. Each criterion belongs to exactly one.

## Verification

Filled in when the spec reaches `built`: one line per criterion id, saying how it was
checked against the default branch (beyond its tests) and the result.
