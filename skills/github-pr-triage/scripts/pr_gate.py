#!/usr/bin/env python3
"""Merge gate: is each PR open, mergeable, and green on every CI check that has finished?

Exit codes: 0 every PR is ready; 1 a check failed; 3 a check is still pending (or --wait
timed out); 4 a PR is not open or conflicts; 2 bad arguments or a gh failure.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from dataclasses import dataclass

GREEN = {"SUCCESS", "NEUTRAL", "SKIPPED"}
READY, FAILED, PENDING, BLOCKED = 0, 1, 3, 4


@dataclass(frozen=True)
class Check:
    name: str
    done: bool
    conclusion: str


@dataclass(frozen=True)
class Gate:
    number: int
    state: str
    mergeable: str
    base: str
    head: str
    checks: tuple[Check, ...]

    @property
    def verdict(self) -> int:
        if self.state != "OPEN" or self.mergeable == "CONFLICTING":
            return BLOCKED
        if any(c.done and c.conclusion not in GREEN for c in self.checks):
            return FAILED
        if not self.checks or any(not c.done for c in self.checks) or self.mergeable != "MERGEABLE":
            return PENDING
        return READY


def check_of(entry: dict) -> Check:
    """A CheckRun (status, conclusion) or a StatusContext (state)"""
    if "state" in entry and "status" not in entry:
        state = str(entry["state"])
        return Check(str(entry.get("context", "?")), state != "PENDING", state)
    status = str(entry.get("status", ""))
    return Check(str(entry.get("name", "?")), status == "COMPLETED", str(entry.get("conclusion") or ""))


def fetch(number: int, repo: str | None) -> Gate:
    cmd = ["gh", "pr", "view", str(number), "--json", "state,mergeable,baseRefName,headRefOid,statusCheckRollup"]
    if repo:
        cmd += ["-R", repo]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        sys.exit(f"pr_gate: gh failed for #{number}: {proc.stderr.strip()}")
    data = json.loads(proc.stdout)
    checks = tuple(check_of(e) for e in data.get("statusCheckRollup") or ())
    return Gate(number, data["state"], data["mergeable"], data["baseRefName"], data["headRefOid"][:7], checks)


def render(gate: Gate) -> str:
    green = sum(c.done and c.conclusion in GREEN for c in gate.checks)
    failed = [c for c in gate.checks if c.done and c.conclusion not in GREEN]
    pending = [c for c in gate.checks if not c.done]
    label = {READY: "READY", FAILED: "FAILED", PENDING: "PENDING", BLOCKED: "BLOCKED"}[gate.verdict]
    lines = [
        f"#{gate.number} {label} state={gate.state} mergeable={gate.mergeable} base={gate.base} "
        f"head={gate.head} checks={len(gate.checks)} green={green} pending={len(pending)} failed={len(failed)}"
    ]
    lines += [f"  failed: {c.name} ({c.conclusion})" for c in failed]
    lines += [f"  pending: {c.name}" for c in pending]
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("prs", nargs="+", type=int, help="PR numbers")
    parser.add_argument("-R", "--repo", help="owner/name; default: the current checkout's repo")
    parser.add_argument("--wait", action="store_true", help="poll until no check is pending")
    parser.add_argument("--interval", type=int, default=30, help="seconds between polls (default 30)")
    parser.add_argument("--timeout", type=int, default=1800, help="seconds before --wait gives up (default 1800)")
    args = parser.parse_args()
    if args.interval < 1 or args.timeout < 1:
        parser.error("--interval and --timeout must be positive")
    deadline = time.monotonic() + args.timeout
    while True:
        gates = [fetch(n, args.repo) for n in args.prs]
        worst = max(g.verdict for g in gates)
        if not args.wait or worst != PENDING or time.monotonic() >= deadline:
            print("\n".join(render(g) for g in gates))
            return worst
        time.sleep(args.interval)


if __name__ == "__main__":
    sys.exit(main())
