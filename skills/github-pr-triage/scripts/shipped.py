#!/usr/bin/env python3
"""Find the issues a release fixed, and optionally tell each one it shipped.

Usage: shipped.py <owner/repo> <prev-tag> <tag> [--repo-dir PATH] [--install TEXT] [--post]

Walks `git log <prev-tag>..<tag>` in the local checkout and collects issue numbers from:
  - closing trailers in commit messages ("Fixes #N" at the start of a line)
  - the bodies of the PRs those commits belong to (merge, squash, or rebase merges)
Keeps only issues that are closed, and skips any that already carry a
"Released in <tag>" comment, so a second run posts nothing.

Without --post it prints the plan (one line per issue) and posts nothing.
With --post it comments "Released in <tag>." plus --install text, if given.

Exit 0 on success (including nothing to do), 2 on bad input or a git/gh failure.
Needs git and an authenticated gh CLI. Python 3.10+, standard library only.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys

KEYWORDS = r"(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)"
TRAILER = re.compile(rf"^\s*(?:[-*]\s*)?{KEYWORDS}:?\s+#(\d+)\b", re.IGNORECASE | re.MULTILINE)
ANYWHERE = re.compile(rf"\b{KEYWORDS}:?\s+#(\d+)\b", re.IGNORECASE)


def run(cmd: list[str], cwd: str | None = None) -> str:
    proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        sys.stderr.write(f"error: {' '.join(cmd[:3])}...: {proc.stderr.strip()}\n")
        raise SystemExit(2)
    return proc.stdout


def commits(repo_dir: str, prev: str, tag: str) -> list[tuple[str, str]]:
    out = run(["git", "log", "--format=%H%x00%B%x1e", f"{prev}..{tag}"], cwd=repo_dir)
    pairs = []
    for record in out.split("\x1e"):
        if record.strip():
            sha, _, body = record.strip().partition("\x00")
            pairs.append((sha, body))
    return pairs


def prs_for(repo: str, sha: str) -> list[dict[str, object]]:
    out = run(["gh", "api", f"repos/{repo}/commits/{sha}/pulls"])
    prs: list[dict[str, object]] = json.loads(out)
    return prs


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("repo", help="owner/name")
    parser.add_argument("prev_tag")
    parser.add_argument("tag")
    parser.add_argument("--repo-dir", default=".", help="local checkout (default: cwd)")
    parser.add_argument("--install", default="", help='appended to the comment, e.g. "`uv add pkg==X`"')
    parser.add_argument("--post", action="store_true", help="post the comments (default: print the plan)")
    args = parser.parse_args()
    if "/" not in args.repo:
        parser.error("repo must be owner/name")

    run(["git", "fetch", "-q", "--force", "--tags", "origin"], cwd=args.repo_dir)  # a bot moves major tags such as v0
    issues: dict[int, set[str]] = {}
    seen_prs: set[int] = set()
    for sha, body in commits(args.repo_dir, args.prev_tag, args.tag):
        for n in TRAILER.findall(body):
            issues.setdefault(int(n), set()).add(sha[:7])
        for pr in prs_for(args.repo, sha):
            number = int(str(pr["number"]))
            if number in seen_prs or not pr.get("merged_at"):
                continue
            seen_prs.add(number)
            text = f"{pr.get('title') or ''}\n{pr.get('body') or ''}"
            for n in ANYWHERE.findall(text):
                issues.setdefault(int(n), set()).add(f"PR #{number}")

    marker = f"Released in {args.tag}"
    for number in sorted(issues):
        view = json.loads(
            run(["gh", "issue", "view", str(number), "-R", args.repo, "--json", "state,title,comments,url"])
        )
        if "/pull/" in view["url"]:
            continue
        source = ", ".join(sorted(issues[number]))
        if view["state"] != "CLOSED":
            print(f"#{number:<5} SKIP open         {view['title'][:60]}  [{source}]")
            continue
        if any(c["body"].startswith(marker) for c in view["comments"]):
            print(f"#{number:<5} SKIP notified     {view['title'][:60]}")
            continue
        body = f"{marker}.{(' ' + args.install) if args.install else ''}"
        if args.post:
            run(["gh", "issue", "comment", str(number), "-R", args.repo, "--body", body])
            print(f"#{number:<5} POSTED            {view['title'][:60]}  [{source}]")
        else:
            print(f"#{number:<5} WOULD POST        {view['title'][:60]}  [{source}]")
    return 0


if __name__ == "__main__":
    sys.exit(main())
