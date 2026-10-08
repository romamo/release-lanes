#!/usr/bin/env python3
"""Find the issues a release fixed, and optionally tell each one it shipped.

Usage: shipped.py <owner/repo> <prev-tag> <tag> [--repo-dir PATH] [--install TEXT] [--post]

Walks `git log <prev-tag>..<tag>` in the local checkout and collects issue numbers from:
  - closing trailers in commit messages ("Fixes #N" at the start of a line)
  - the bodies of the PRs those commits belong to (merge, squash, or rebase merges)
Keeps only issues that are closed, and skips any that already carry a
"Released in <tag>" comment, so a second run posts nothing.

Without --post it prints the plan (one line per issue) and posts nothing.
With --post it comments "Released in <tag>." plus --install text, if given. When the
checkout's .github/shipmill.toml sets [agents] app_id, each comment posts through
`uvx --from git+https://github.com/shipmill/shipmill@v0 shipmill gh`, as the App (spec 012);
without it, plain gh posts as the host's login. Reads always use plain gh.

Exit 0 on success (including nothing to do), 2 on bad input, a config github-ship-watch's
watch_state.py refuses, or a git/gh failure (a `shipmill gh` exit 2 among them: the comment
is never retried with plain gh). Needs git and an authenticated gh CLI, and uvx with app_id
set. Python 3.10+, standard library only.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import re
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from types import ModuleType

# github-ship-watch's watch_state.py: one reading of the config's [agents] for every skill script
WATCH_STATE = Path(__file__).resolve().parents[2] / "github-ship-watch" / "scripts" / "watch_state.py"
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


def watch_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("shipmill_watch_state", WATCH_STATE)
    if spec is None or spec.loader is None:
        sys.stderr.write(f"error: can't load {WATCH_STATE}\n")
        raise SystemExit(2)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # its dataclasses look their module up by name
    spec.loader.exec_module(module)
    return module


def writer(repo_dir: Path, ws: ModuleType) -> list[str]:
    """The comment's gh: `shipmill gh` with [agents] app_id set, else plain gh (spec 012)"""
    try:
        found: list[str] = ws.gh_writer(repo_dir)
    except ws.Refused as refused:
        sys.stderr.write(f"{refused}\n")
        raise SystemExit(2) from None
    return found


def tell(
    repo: str,
    issues: dict[int, set[str]],
    tag: str,
    install: str,
    write: list[str] | None,
    gh: Callable[[list[str]], str] = run,
) -> None:
    """One line per issue; with write (the --post gh), comment on each closed one not yet told.
    Reads are plain gh; a failed write stops the run, never retried another way"""
    marker = f"Released in {tag}"
    for number in sorted(issues):
        view = json.loads(gh(["gh", "issue", "view", str(number), "-R", repo, "--json", "state,title,comments,url"]))
        if "/pull/" in view["url"]:
            continue
        source = ", ".join(sorted(issues[number]))
        if view["state"] != "CLOSED":
            print(f"#{number:<5} SKIP open         {view['title'][:60]}  [{source}]")
            continue
        if any(c["body"].startswith(marker) for c in view["comments"]):
            print(f"#{number:<5} SKIP notified     {view['title'][:60]}")
            continue
        body = f"{marker}.{(' ' + install) if install else ''}"
        if write is not None:
            gh([*write, "issue", "comment", str(number), "-R", repo, "--body", body])
            print(f"#{number:<5} POSTED            {view['title'][:60]}  [{source}]")
        else:
            print(f"#{number:<5} WOULD POST        {view['title'][:60]}  [{source}]")


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

    # before any gh call, so a config that can't be read stops the run with nothing posted
    write = writer(Path(args.repo_dir), watch_module()) if args.post else None
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

    tell(args.repo, issues, args.tag, args.install, write)
    return 0


if __name__ == "__main__":
    sys.exit(main())
