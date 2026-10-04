"""shipyard: release lanes driven by a hand-written CHANGELOG.

  settle-minutes  how long to wait after a push for more merges
  plan            decide whether a lane releases now; JSON on stdout
  prepare         stamp a planned release into the checkout, optionally commit and push it
  land            push, tag, sync main, and publish a release commit that passed CI
  cleanup         delete a release commit's work branch
  sync            bring a stable release made off main into main's CHANGELOG (recovery)
  notes           print a release's notes
  doctor          check that the repository is ready for the bot
  init            write a starting policy and the calling workflow

Exit codes: 0 done (a plan may skip); 1 doctor found a failure; 2 bad input or a refused state.
"""

import argparse
import datetime as dt
import json
import os
import sys
from collections.abc import Mapping
from pathlib import Path

from shipyard.doctor import CALLER, doctor
from shipyard.errors import ReleaseError
from shipyard.github import GhCli
from shipyard.gitrepo import Git
from shipyard.init import init
from shipyard.land import cleanup, land, prepare
from shipyard.planner import Event, Hotfix, Planner
from shipyard.policy import ALIAS_PATH, CONFIG_PATH, Lane, Policy, config_path
from shipyard.stamp import notes, sync
from shipyard.version import Version


def _outputs(path: Path | None, values: Mapping[str, str]) -> None:
    """Append key=value lines for $GITHUB_OUTPUT; newlines are flattened so a value (which
    may quote an issue title) can't set another output"""
    if path is None:
        return
    with path.open("a", encoding="utf-8") as out:
        out.writelines(f"{k}={' '.join(v.splitlines())}\n" for k, v in values.items())


def _ints(text: str) -> tuple[int, ...]:
    parts = [p for p in text.replace(",", " ").split() if p]
    try:
        return tuple(int(p.lstrip("#")) for p in parts)
    except ValueError:
        raise ReleaseError(f"pull request numbers are integers, such as 12,15; got {text!r}") from None


def _now(text: str | None) -> dt.datetime:
    if text is None:
        return dt.datetime.now(dt.UTC)
    now = dt.datetime.fromisoformat(text)
    if now.tzinfo is None:
        raise ReleaseError(f"--now needs a UTC offset, such as 2026-10-05T07:10:00+00:00; got {text!r}")
    return now


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="shipyard", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--repo", type=Path, default=Path.cwd(), help="the repository checkout (default: .)")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("settle-minutes", help="print the longest quiet_minutes in the policy")

    p = sub.add_parser("plan", help="decide whether a lane releases now")
    p.add_argument("--event", choices=[e.value for e in Event], help="default: $GITHUB_EVENT_NAME, else manual")
    p.add_argument("--lane", choices=[ln.value for ln in Lane], help="release this lane by hand")
    p.add_argument("--dry-run", action="store_true", help="plan as dry-run whatever the policy's mode")
    p.add_argument("--hotfix-prs", default="", help="hotfix: merged pull requests, such as 12,15")
    p.add_argument("--hotfix-from", default="", help="hotfix: the stable tag to fix (default: the latest)")
    p.add_argument("--now", help="ISO time with offset (default: now)")
    p.add_argument("--github-output", type=Path)

    p = sub.add_parser("prepare", help="stamp a planned release into the checkout")
    _release_args(p)
    p.add_argument("--base", required=True)
    p.add_argument("--merges", default="", help="hotfix: merge commits, space-separated")
    p.add_argument("--prs", default="", help="hotfix: pull request numbers, for the commit message")
    p.add_argument("--date", type=dt.date.fromisoformat, help="YYYY-MM-DD (default: today, UTC)")
    p.add_argument("--commit", action="store_true", help="commit the stamp as the release commit")
    p.add_argument("--push", action="store_true", help="also push the commit to its work branch")
    p.add_argument("--github-output", type=Path)

    p = sub.add_parser("land", help="push, tag, sync, and publish a release commit that passed CI")
    _release_args(p)
    p.add_argument("--sha", required=True)
    p.add_argument("--base", required=True)
    p.add_argument("--date", type=dt.date.fromisoformat, help="YYYY-MM-DD (default: today, UTC)")

    p = sub.add_parser("cleanup", help="delete a release commit's work branch")
    p.add_argument("--version", required=True, type=Version.parse)

    p = sub.add_parser("sync", help="bring a stable release made off main into the checkout of main")
    p.add_argument("--version", required=True, type=Version.parse)
    p.add_argument("--date", type=dt.date.fromisoformat, help="YYYY-MM-DD (default: today, UTC)")

    p = sub.add_parser("notes", help="print a release's notes, from its tag")
    p.add_argument("--version", required=True, type=Version.parse)

    sub.add_parser("doctor", help="check that the repository is ready for the bot")

    p = sub.add_parser("init", help=f"write {CONFIG_PATH} and {CALLER}")
    p.add_argument("--ci", default="ci.yml", help="the CI workflow a release commit must pass (default: ci.yml)")
    p.add_argument(
        "--force",
        action="store_true",
        help=f"overwrite existing files; an existing {ALIAS_PATH} is replaced by {CONFIG_PATH}",
    )
    return parser


def _release_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--lane", required=True, choices=[ln.value for ln in Lane])
    p.add_argument("--version", required=True, type=Version.parse)


def main(argv: list[str]) -> int:
    args = _parser().parse_args(argv)
    root: Path = args.repo.resolve()
    if args.command == "init":
        initialized = init(root, args.ci, args.force)
        for path in initialized.written:
            print(f"wrote {path.relative_to(root)}")
        if initialized.removed is not None:
            print(f"removed {initialized.removed.relative_to(root)}, which {CONFIG_PATH} replaces")
        print("next: review the policy, then run `shipyard doctor`")
        return 0
    if args.command == "doctor":
        checks = doctor(root)
        for check in checks:
            print(f"{check.status} {check.name}: {check.detail}")
        return 1 if any(c.status == "FAIL" for c in checks) else 0

    policy = Policy.load(config_path(root))
    git = Git(root, policy.bot_name, policy.bot_email)
    today = dt.datetime.now(dt.UTC).date()
    if args.command == "settle-minutes":
        print(policy.quiet_minutes)
    elif args.command == "plan":
        event = Event(args.event or os.environ.get("GITHUB_EVENT_NAME") or Event.MANUAL.value)
        lane = Lane(args.lane) if args.lane else None
        hotfix = Hotfix(_ints(args.hotfix_prs), args.hotfix_from or None) if lane is Lane.HOTFIX else None
        if hotfix is None and (args.hotfix_prs or args.hotfix_from):
            raise ReleaseError("--hotfix-prs and --hotfix-from go with --lane hotfix")
        decision = Planner(git, policy, GhCli(root), _now(args.now)).plan(event, lane, args.dry_run, hotfix)
        outputs = decision.outputs()
        print(json.dumps(outputs, indent=2))
        _outputs(args.github_output, outputs)
    elif args.command == "prepare":
        prepared = prepare(
            git,
            policy,
            Lane(args.lane),
            args.version,
            args.base,
            args.date or today,
            tuple(args.merges.split()),
            _ints(args.prs),
            commit=args.commit or args.push,
            push=args.push,
        )
        for changed in prepared.changed:
            print(f"stamped {changed}")
        if args.push and not prepared.pushed:
            print(f"another run holds the work branch for {args.version}; this run stops", file=sys.stderr)
        sha = prepared.sha if (prepared.pushed or not args.push) else ""
        _outputs(args.github_output, {"sha": sha})
    elif args.command == "land":
        landed = land(git, policy, GhCli(root), Lane(args.lane), args.version, args.sha, args.base, args.date or today)
        print(
            json.dumps(
                {
                    "tag": landed.tag,
                    "placed": landed.placed,
                    "synced": landed.synced,
                    "published": list(landed.published),
                },
                indent=2,
            )
        )
    elif args.command == "cleanup":
        print("deleted the work branch" if cleanup(git, args.version) else "no work branch to delete")
    elif args.command == "sync":
        released = git.show(args.version.tag, policy.changelog)
        if released is None:
            raise ReleaseError(f"no {policy.changelog} at {args.version.tag}")
        newest = all(t.version <= args.version for t in git.tags() if t.version.is_stable)
        for changed in sync(git, policy, args.version, released, args.date or today, newest):
            print(f"synced {changed}")
    elif args.command == "notes":
        text = git.show(args.version.tag, policy.changelog)
        if text is None:
            raise ReleaseError(f"no {policy.changelog} at {args.version.tag}")
        stable = [t.version for t in git.tags() if t.version.is_stable and t.version < args.version]
        sys.stdout.write(notes(policy, text, args.version, max(stable) if stable else None))
    return 0


def run() -> None:
    try:
        code = main(sys.argv[1:])
    except ReleaseError as exc:
        print(f"shipyard: {exc}", file=sys.stderr)
        code = 2
    sys.exit(code)
