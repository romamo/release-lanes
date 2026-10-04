"""shipyard: release lanes driven by a hand-written CHANGELOG.

  settle-minutes  how long to wait after a push for more merges
  plan            decide whether a lane releases now; JSON on stdout
  propose         open or update the issue for each release a plan proposed
  prepare         stamp a planned release into the checkout, optionally commit and push it
  land            push, tag, sync main, and publish a release commit that passed CI
  close-proposal  close the lane's proposal issue once land released it
  cleanup         delete a release commit's work branch
  sync            bring a stable release made off main into main's CHANGELOG (recovery)
  notes           print a release's notes
  operate         check environment health, promote after the bake, roll back; approve a proposal
  doctor          check that the repository is ready for the bot
  init            write a starting policy and the calling workflow (--operate: the operate one)
  gate            start a Claude Code session for the repo only when its state needs one

Exit codes: 0 done (a plan may skip); 1 doctor found a failure; 2 bad input or a refused state.
"""

import argparse
import datetime as dt
import json
import os
import shutil
import sys
from collections.abc import Mapping
from pathlib import Path

from shipyard.agents import AgentsConfig
from shipyard.config import ALIAS_PATH, CONFIG_PATH, config_path
from shipyard.doctor import CALLER, OPERATE_CALLER, doctor
from shipyard.errors import ReleaseError
from shipyard.gate import ClaudeCli, check_checkout, gate, refresh, watch
from shipyard.github import GhCli, GitHub
from shipyard.gitrepo import Git
from shipyard.init import init, init_operate
from shipyard.land import cleanup, land, prepare
from shipyard.launchd import DEFAULT_TOOL, build, install, remove
from shipyard.operate import Http, UrllibHttp, approve, approve_rollback, operate, summary
from shipyard.planner import Event, Hotfix, Planner, Proposal
from shipyard.policy import Lane, Policy
from shipyard.propose import close_released, propose, run_url
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


def _proposals(text: str) -> tuple[Proposal, ...]:
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ReleaseError(f"--proposals is the plan's proposals output, a JSON list: {exc}") from None
    if not isinstance(raw, list) or not raw:
        raise ReleaseError(f"--proposals is a non-empty JSON list; got {text!r}")
    return tuple(Proposal.from_dict(item) for item in raw)


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

    p = sub.add_parser("propose", help="open or update the issue for each release a plan proposed")
    p.add_argument("--proposals", required=True, help="the plan's proposals output, a JSON list")

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

    p = sub.add_parser("close-proposal", help="close the lane's proposal issue once land released it")
    _release_args(p)

    p = sub.add_parser("cleanup", help="delete a release commit's work branch")
    p.add_argument("--version", required=True, type=Version.parse)

    p = sub.add_parser("sync", help="bring a stable release made off main into the checkout of main")
    p.add_argument("--version", required=True, type=Version.parse)
    p.add_argument("--date", type=dt.date.fromisoformat, help="YYYY-MM-DD (default: today, UTC)")

    p = sub.add_parser("notes", help="print a release's notes, from its tag")
    p.add_argument("--version", required=True, type=Version.parse)

    p = sub.add_parser("operate", help="check environment health, promote after the bake")
    p.add_argument("--dry-run", action="store_true", help="report what it would do; write and start nothing")
    approving = p.add_mutually_exclusive_group()
    approving.add_argument(
        "--approve", metavar="ENVIRONMENT", help="deploy the tag proposed for this environment, once"
    )
    approving.add_argument(
        "--approve-rollback", metavar="ENVIRONMENT", help="start the rollback an incident proposes for it, once"
    )
    p.add_argument("--now", help="ISO time with offset (default: now)")
    p.add_argument(
        "--step-summary",
        type=Path,
        default=os.environ.get("GITHUB_STEP_SUMMARY") or None,
        help="also append the report here (default: $GITHUB_STEP_SUMMARY)",
    )

    sub.add_parser("doctor", help="check that the repository is ready for the bot")

    p = sub.add_parser("init", help=f"write {CONFIG_PATH} and {CALLER}")
    p.add_argument("--ci", default="ci.yml", help="the CI workflow a release commit must pass (default: ci.yml)")
    p.add_argument(
        "--force",
        action="store_true",
        help=f"overwrite existing files; an existing {ALIAS_PATH} is replaced by {CONFIG_PATH}",
    )
    p.add_argument(
        "--operate",
        action="store_true",
        help=f"write only {OPERATE_CALLER}, which runs shipyard operate every 10 minutes",
    )

    p = sub.add_parser(
        "gate", help=f"start a Claude Code session only when the repo's state needs one ([agents] in {CONFIG_PATH})"
    )
    p.add_argument("slug", metavar="owner/name", help="the GitHub repo; --repo is its checkout")
    p.add_argument("--claude-arg", action="append", default=[], help="extra flag for the session (repeatable)")
    p.add_argument(
        "--refresh",
        action="store_true",
        help="move this detached, clean gate checkout to origin's default branch before reading it",
    )
    p.add_argument("--dry-run", action="store_true", help="decide and print; start, stop, or move nothing")
    p.add_argument("--json", action="store_true", help="print the decision as JSON")

    p = sub.add_parser("launchd", help="run the gate for a dedicated checkout every few minutes (macOS)")
    p.add_argument("slug", metavar="owner/name", help="the GitHub repo; --repo is its gate checkout")
    p.add_argument("--every", type=int, default=15, help="minutes between runs (default: 15)")
    p.add_argument("--tool", default=DEFAULT_TOOL, help=f"where uvx gets shipyard (default: {DEFAULT_TOOL})")
    p.add_argument("--claude-arg", action="append", default=[], help="extra flag for the session (repeatable)")
    p.add_argument("--print", action="store_true", help="print the job's plist; install nothing")
    p.add_argument("--remove", action="store_true", help="unload and delete the repo's job")
    return parser


def _release_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--lane", required=True, choices=[ln.value for ln in Lane])
    p.add_argument("--version", required=True, type=Version.parse)


def main(argv: list[str], github: GitHub | None = None, http: Http | None = None) -> int:
    """github stands in for gh, and http for the health checks, as tests pass fakes"""
    args = _parser().parse_args(argv)
    root: Path = args.repo.resolve()
    hub = github or GhCli(root)
    if args.command == "init" and args.operate:
        print(f"wrote {init_operate(root, args.force).relative_to(root)}")
        print("next: run `shipyard doctor`")
        return 0
    if args.command == "init":
        initialized = init(root, args.ci, args.force)
        for path in initialized.written:
            print(f"wrote {path.relative_to(root)}")
        if initialized.removed is not None:
            print(f"removed {initialized.removed.relative_to(root)}, which {CONFIG_PATH} replaces")
        print("next: review the policy, then run `shipyard doctor`")
        return 0
    if args.command == "doctor":
        checks = doctor(root, github or (GhCli(root) if shutil.which("gh") else None))
        for check in checks:
            print(f"{check.status} {check.name}: {check.detail}")
        return 1 if any(c.status == "FAIL" for c in checks) else 0
    if args.command == "gate":
        return _gate(root, args)
    if args.command == "launchd":
        return _launchd(root, args)

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
        decision = Planner(git, policy, hub, _now(args.now)).plan(event, lane, args.dry_run, hotfix)
        outputs = decision.outputs()
        print(json.dumps(outputs, indent=2))
        _outputs(args.github_output, outputs)
    elif args.command == "propose":
        for done in propose(git, policy, hub, _proposals(args.proposals)):
            print(f"{done.outcome} #{done.issue}: {done.proposal.lane} {done.proposal.version}, {done.proposal.cause}")
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
        landed = land(git, policy, hub, Lane(args.lane), args.version, args.sha, args.base, args.date or today)
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
    elif args.command == "close-proposal":
        print(close_released(policy, hub, Lane(args.lane), args.version, run_url(os.environ)))
    elif args.command == "cleanup":
        print("deleted the work branch" if cleanup(git, args.version) else "no work branch to delete")
    elif args.command == "sync":
        released = git.show(args.version.tag, policy.changelog)
        if released is None:
            raise ReleaseError(f"no {policy.changelog} at {args.version.tag}")
        newest = all(t.version <= args.version for t in git.tags() if t.version.is_stable)
        for changed in sync(git, policy, args.version, released, args.date or today, newest):
            print(f"synced {changed}")
    elif args.command == "operate":
        if args.approve:
            report = approve(policy, hub, args.approve, args.dry_run) + "\n"
        elif args.approve_rollback:
            report = approve_rollback(policy, hub, args.approve_rollback, args.dry_run) + "\n"
        else:
            reports = operate(policy, git.tags(), hub, http or UrllibHttp(), _now(args.now), args.dry_run)
            report = summary(reports, args.dry_run)
        sys.stdout.write(report)
        if args.step_summary is not None:
            with args.step_summary.open("a", encoding="utf-8") as out:
                out.write(report)
    elif args.command == "notes":
        text = git.show(args.version.tag, policy.changelog)
        if text is None:
            raise ReleaseError(f"no {policy.changelog} at {args.version.tag}")
        stable = [t.version for t in git.tags() if t.version.is_stable and t.version < args.version]
        sys.stdout.write(notes(policy, text, args.version, max(stable) if stable else None))
    return 0


def _gate(root: Path, args: argparse.Namespace) -> int:
    decision, launched = gate(
        Git(root),
        args.slug,
        lambda: AgentsConfig.load(root),
        ClaudeCli(args.claude_arg),
        lambda: watch(args.slug, root),
        dt.datetime.now(dt.UTC),
        args.refresh,
        args.dry_run,
    )
    if args.json:
        record = {
            "action": decision.action.value,
            "reason": decision.reason,
            "work": [f.line() for f in decision.work],
            "stopped": [] if args.dry_run else list(decision.stop),
            "launched": launched,
        }
        print(json.dumps(record, indent=2))
        return 0
    print(f"{decision.action.value}: {decision.reason}")
    for finding in decision.work:
        print(f"  {finding.line()}")
    if launched:
        print(f"  launched {launched}: claude attach {launched}")
    return 0


def _launchd(root: Path, args: argparse.Namespace) -> int:
    home = Path.home()
    if args.remove:
        removed = remove(args.slug, home)
        print(f"removed {removed}" if removed else f"no launchd job for {args.slug}")
        return 0
    git = Git(root)
    check_checkout(git, args.slug)
    job = build(args.slug, root, args.every, args.tool, args.claude_arg, home, os.environ.get("PATH", ""))
    if args.print:
        sys.stdout.write(job.document.decode())
        return 0
    refresh(git)  # a dedicated checkout, at the default branch's head
    agents = AgentsConfig.load(root)  # the job would fail on every run without it
    install(job)
    print(f"installed {job.plist}: every {args.every} min, log {job.log}")
    print(f"prompt: {agents.prompt}")
    return 0


def run() -> None:
    try:
        code = main(sys.argv[1:])
    except ReleaseError as exc:
        print(f"shipyard: {exc}", file=sys.stderr)
        code = 2
    sys.exit(code)
