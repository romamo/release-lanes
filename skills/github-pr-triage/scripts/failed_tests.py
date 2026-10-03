#!/usr/bin/env python3
"""Failing tests of a GitHub Actions run, per job: test names, assertion lines, and the
pytest summary, from `gh run view --log-failed`.

Exit codes: 0 failures listed; 1 no failing test found in the log; 3 the run is still in
progress (its logs appear only once it completes); 2 bad arguments or a gh failure.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from collections import OrderedDict

HEADER = re.compile(r"^_+ (test\S*) _+$")  # pytest pads a long name with a single "_"
FAILED_LINE = re.compile(r"^FAILED (\S+)")
ERROR_LINE = re.compile(r"^E {2,}(.*)$")
LOCATION = re.compile(r"^(\S+\.py:\d+): (\w+)$")
SUMMARY = re.compile(r"^=+ (.*\b(?:failed|error|errors)\b.*) =+$")
TIMESTAMP = re.compile(r"^\d{4}-\d\d-\d\dT[\d:.]+Z ?")
MAX_E_LINES = 4


def log_lines(run_id: str, repo: str | None) -> list[tuple[str, str]]:
    cmd = ["gh", "run", "view", run_id, "--log-failed"]
    if repo:
        cmd += ["-R", repo]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        message = proc.stderr.strip() or proc.stdout.strip()
        if "still in progress" in message:
            print(f"run {run_id} is still in progress; logs appear once it completes")
            sys.exit(3)
        sys.exit(f"failed_tests: gh failed: {message}")
    pairs = []
    for raw in proc.stdout.splitlines():
        parts = raw.split("\t", 2)
        if len(parts) != 3:
            continue
        job, _step, rest = parts
        pairs.append((job, TIMESTAMP.sub("", rest)))
    return pairs


def summarize(pairs: list[tuple[str, str]]) -> OrderedDict[str, dict[str, list[str]]]:
    jobs: OrderedDict[str, dict[str, list[str]]] = OrderedDict()
    current: dict[str, str] = {}
    shown: dict[tuple[str, str], int] = {}
    for job, line in pairs:
        entry = jobs.setdefault(job, {"tests": [], "details": [], "summary": []})
        text = line.rstrip()
        if m := HEADER.match(text):
            entry["tests"].append(m.group(1))
            current[job] = m.group(1)
            entry["details"].append(f"[{m.group(1)}]")
        elif m := FAILED_LINE.match(text):
            name = m.group(1)
            if not any(name.endswith(t) for t in entry["tests"]):
                entry["tests"].append(name)
        elif (m := ERROR_LINE.match(text)) and job in current and m.group(1).strip():
            key = (job, current[job])
            if shown.get(key, 0) < MAX_E_LINES:
                shown[key] = shown.get(key, 0) + 1
                entry["details"].append(f"  E {m.group(1).strip()}")
        elif m := LOCATION.match(text):
            entry["details"].append(f"  at {m.group(1)} ({m.group(2)})")
        elif m := SUMMARY.match(text):
            entry["summary"].append(m.group(1))
    return OrderedDict((j, e) for j, e in jobs.items() if e["tests"] or e["summary"])


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("run_id", help="GitHub Actions run id (gh run list --json databaseId)")
    parser.add_argument("-R", "--repo", help="owner/name; default: the current checkout's repo")
    args = parser.parse_args()
    if not args.run_id.isdigit():
        parser.error(f"run id must be numeric, got {args.run_id!r}")
    jobs = summarize(log_lines(args.run_id, args.repo))
    if not jobs:
        print(f"run {args.run_id}: no failing test found in the failed-job logs (a setup or lint step may have failed)")
        return 1
    for job, entry in jobs.items():
        print(f"== {job}")
        for summary in entry["summary"]:
            print(f"  summary: {summary}")
        for test in entry["tests"]:
            print(f"  failed: {test}")
        for detail in entry["details"]:
            print(f"  {detail}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
