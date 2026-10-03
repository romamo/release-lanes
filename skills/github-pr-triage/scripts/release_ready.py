#!/usr/bin/env python3
"""Check that a release commit is ready to tag.

Usage: release_ready.py <owner/repo> <sha> <version> [--repo-dir PATH] [--tag-prefix v]
                        [--manifest FILE | --no-manifest] [--changelog FILE | --no-changelog]

Checks, each printed as PASS or FAIL:
  ci         every workflow run on <sha> completed with success
  on-branch  <sha> is on origin/<default branch>
  tag        <tag-prefix><version> doesn't exist yet, locally or on origin
  manifest   the manifest at <sha> declares version <version>: pyproject.toml, Cargo.toml,
             package.json, or any TOML/JSON file with a top-level version (default: the
             first of those that exists at <sha>; skipped with --no-manifest)
  changelog  the first released section at <sha> is ## [<version>] and dated
  breaking   if that section has a "### Breaking" subsection, its summary says "not additive"
  links      the compare link for [<version>] exists
  (changelog, breaking, and links assume Keep a Changelog; skip them with --no-changelog)
Then lists open PRs (and when each last changed), for you to judge which belong in the release.

Exit 0 when every check passes, 1 when any fails, 2 on bad input or a git/gh failure.
Needs git and an authenticated gh CLI. Python 3.10+, standard library only.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys


def run(cmd: list[str], cwd: str | None = None, ok_codes: tuple[int, ...] = (0,)) -> str:
    proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, check=False)
    if proc.returncode not in ok_codes:
        sys.stderr.write(f"error: {' '.join(cmd[:3])}...: {proc.stderr.strip()}\n")
        raise SystemExit(2)
    return proc.stdout


MANIFESTS = ("pyproject.toml", "Cargo.toml", "package.json")


def manifest_version(text: str, path: str) -> str | None:
    """The top-level version a TOML or JSON manifest declares"""
    if path.endswith(".json"):
        value = json.loads(text).get("version")
        return value if isinstance(value, str) else None
    m = re.search(r'^version\s*=\s*"([^"]+)"', text, re.MULTILINE)
    return m.group(1) if m else None


def at(sha: str, path: str, d: str) -> str | None:
    proc = subprocess.run(["git", "show", f"{sha}:{path}"], cwd=d, capture_output=True, text=True, check=False)
    return proc.stdout if proc.returncode == 0 else None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("repo")
    parser.add_argument("sha")
    parser.add_argument("version")
    parser.add_argument("--repo-dir", default=".")
    parser.add_argument("--tag-prefix", default="v")
    parser.add_argument("--manifest", help="version file (default: first found of " + ", ".join(MANIFESTS) + ")")
    parser.add_argument("--no-manifest", action="store_true", help="skip the manifest check (e.g. Go)")
    parser.add_argument("--changelog", default="CHANGELOG.md")
    parser.add_argument("--no-changelog", action="store_true", help="skip the Keep a Changelog checks")
    args = parser.parse_args()
    d = args.repo_dir
    tag = f"{args.tag_prefix}{args.version}"

    run(["git", "fetch", "-q", "--tags", "origin"], cwd=d)
    sha = run(["git", "rev-parse", f"{args.sha}^{{commit}}"], cwd=d).strip()
    default = json.loads(run(["gh", "repo", "view", args.repo, "--json", "defaultBranchRef"]))["defaultBranchRef"][
        "name"
    ]
    results: list[tuple[str, bool, str]] = []

    runs = json.loads(
        run(
            [
                "gh",
                "run",
                "list",
                "-R",
                args.repo,
                "--commit",
                sha,
                "--json",
                "workflowName,status,conclusion",
                "--limit",
                "50",
            ]
        )
    )
    bad = [
        f"{r['workflowName']}={r['conclusion'] or r['status']}"
        for r in runs
        if r["status"] != "completed" or r["conclusion"] not in ("success", "skipped", "neutral")
    ]
    results.append(
        (
            "ci",
            bool(runs) and not bad,
            ", ".join(bad) if bad else f"{len(runs)} run(s) green" if runs else "no runs on this commit yet",
        )
    )

    on_branch = subprocess.run(["git", "merge-base", "--is-ancestor", sha, f"origin/{default}"], cwd=d).returncode == 0
    results.append(("on-branch", on_branch, f"origin/{default}"))

    local = run(["git", "tag", "-l", tag], cwd=d).strip()
    remote = run(["git", "ls-remote", "--tags", "origin", f"refs/tags/{tag}"], cwd=d).strip()
    results.append(("tag", not local and not remote, f"{tag} {'exists' if local or remote else 'is free'}"))

    if not args.no_manifest:
        paths = [args.manifest] if args.manifest else list(MANIFESTS)
        found = next(((p, t) for p in paths if (t := at(sha, p, d)) is not None), None)
        if found is None:
            results.append(
                ("manifest", False, f"none of {', '.join(paths)} at {sha[:7]}; pass --manifest or --no-manifest")
            )
        else:
            version = manifest_version(found[1], found[0])
            results.append(("manifest", version == args.version, f"{found[0]}: version = {version or '?'}"))

    if not args.no_changelog:
        log = at(sha, args.changelog, d)
        if log is None:
            results.append(("changelog", False, f"no {args.changelog} at {sha[:7]}; pass --no-changelog"))
        else:
            released = re.search(r"^## \[(?!Unreleased\])([^\]]+)\](.*)$", log, re.MULTILINE)
            heading_ok = bool(
                released and released.group(1) == args.version and re.search(r"\d{4}-\d{2}-\d{2}", released.group(2))
            )
            results.append(("changelog", heading_ok, released.group(0) if released else "no released section"))

            if released:
                start = released.end()
                nxt = re.search(r"^## \[", log[start:], re.MULTILINE)
                section = log[start : start + nxt.start()] if nxt else log[start:]
                summary = section.split("###", 1)[0].lower()
                breaking = "### Breaking" in section
                results.append(
                    (
                        "breaking",
                        not breaking or "not additive" in summary,
                        "has Breaking; summary says not additive"
                        if breaking and "not additive" in summary
                        else "has Breaking but the summary doesn't say not additive"
                        if breaking
                        else "no Breaking section",
                    )
                )
            link = re.search(rf"^\[{re.escape(args.version)}\]: \S+", log, re.MULTILINE)
            results.append(("links", bool(link), link.group(0) if link else f"no [{args.version}]: link"))

    for name, ok, detail in results:
        print(f"{'PASS' if ok else 'FAIL'}  {name:<10} {detail}")
    prs = json.loads(run(["gh", "pr", "list", "-R", args.repo, "--state", "open", "--json", "number,title,updatedAt"]))
    print(f"\nOpen PRs ({len(prs)}), judge which belong in {tag}:")
    for pr in prs:
        print(f"  #{pr['number']:<5} {pr['updatedAt'][:10]}  {pr['title'][:70]}")
    return 0 if all(ok for _, ok, _ in results) else 1


if __name__ == "__main__":
    sys.exit(main())
