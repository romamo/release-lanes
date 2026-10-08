"""shipmill: release lanes driven by a hand-written CHANGELOG.

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
  status          say whether the factory works or is stuck: a verdict, then a short summary with
                  a line per reason and a direct link per item; --rows for github-ship-watch's
                  table, --json for its JSON lines
  init            write a starting policy and the calling workflow (--operate: the operate one)
  gate            start a Claude Code session for the repo only when its state needs one; each tick,
                  held or not, prunes the worktrees that landed, as `worktrees --prune` does
  worktrees       list the repository's worktrees, each REMOVABLE once its work landed, or KEPT and why;
                  --prune removes the REMOVABLE ones and their local branches
  app-token       print a GitHub App installation token limited to one repo, cached while it has
                  10 minutes left; --git-credential answers as git's credential helper
  app-create      create the gate's GitHub App in one click: owner and visibility planned from the
                  repos holding .github/shipmill.toml, the key saved with mode 0600
  app-install     guide installing the App on more repos: the App's Install App page and what to pick

Exit codes: 0 done (a plan may skip); 1 doctor found a failure, status found a row that needs
action, or app-create or app-install left a repo without the App; 2 bad input or a refused state.
"""

import argparse
import dataclasses
import datetime as dt
import importlib.metadata
import json
import os
import re
import shutil
import sys
import time
import webbrowser
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import TextIO

from shipmill import cli_command, status
from shipmill.agents import AgentsConfig
from shipmill.app import (
    CACHE,
    HELPERS,
    Api,
    Identity,
    Openssl,
    Signer,
    UrllibApi,
    app_check,
    app_token,
    check_key,
    credential,
    default_key,
    prepare_session,
)
from shipmill.app_create import (
    FLOW_SECONDS,
    accounts,
    check_key_dir,
    config_lines,
    convert,
    create,
    free_name,
    gated_repos,
    given_repos,
    host_token,
    owner_menu,
    pick_owner,
    plan,
)
from shipmill.app_install import WAIT_SECONDS, guide
from shipmill.autonomy import Hold
from shipmill.config import CONFIG_PATH, config_path
from shipmill.doctor import CALLER, OPERATE_CALLER, doctor, read_plugin_rows
from shipmill.errors import ReleaseError
from shipmill.gate import (
    ClaudeCli,
    StateRead,
    StateRunner,
    check_checkout,
    gate,
    host_login,
    pruner,
    refresh,
    run_state,
    state_dir,
    state_failed,
    tick_lines,
    tick_record,
    watch,
    watch_command,
)
from shipmill.github import GhCli, GitHub
from shipmill.gitrepo import Git
from shipmill.init import init, init_operate
from shipmill.land import ActionsRun, Prepared, Stop, cleanup, land, prepare, work_branch
from shipmill.launchd import DEFAULT_TOOL, build, install, remove
from shipmill.notify import Desktop
from shipmill.operate import Http, UrllibHttp, approve, approve_rollback, operate, summary
from shipmill.planner import Event, Hotfix, Planner, Proposal
from shipmill.policy import Lane, Policy
from shipmill.propose import close_released, propose, run_url
from shipmill.stamp import notes, sync
from shipmill.version import Version
from shipmill.worktrees import ClaudeSessions, Sessions, judge, prune, table
from shipmill.worktrees import report as worktrees_report


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
        prog="shipmill", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
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
    p.add_argument(
        "--workflow-ref",
        default=os.environ.get("GITHUB_WORKFLOW_REF", ""),
        help="the release workflow, whose other active runs may own a work branch (default: $GITHUB_WORKFLOW_REF)",
    )
    p.add_argument(
        "--run-id",
        default=os.environ.get("GITHUB_RUN_ID", ""),
        help="this run, never an owner of a work branch (default: $GITHUB_RUN_ID)",
    )
    p.add_argument(
        "--step-summary",
        type=Path,
        default=os.environ.get("GITHUB_STEP_SUMMARY") or None,
        help="also report a stop or a recovery here (default: $GITHUB_STEP_SUMMARY)",
    )

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

    p = sub.add_parser("status", help="say whether the factory works or is stuck (specs 008, 009)")
    p.add_argument(
        "slug", nargs="?", metavar="owner/name", help="the GitHub repo (default: origin's); --repo is its checkout"
    )
    shown = p.add_mutually_exclusive_group()
    shown.add_argument("--rows", action="store_true", help="print github-ship-watch's table instead")
    shown.add_argument("--json", action="store_true", help="print github-ship-watch's JSON lines instead")

    p = sub.add_parser("init", help=f"write {CONFIG_PATH} and {CALLER}")
    p.add_argument("--ci", default="ci.yml", help="the CI workflow a release commit must pass (default: ci.yml)")
    p.add_argument("--force", action="store_true", help="overwrite existing files")
    p.add_argument(
        "--operate",
        action="store_true",
        help=f"write only {OPERATE_CALLER}, which runs shipmill operate every 10 minutes",
    )

    p = sub.add_parser(
        "gate",
        help=(
            f"start a Claude Code session only when the repo's state needs one ([agents] in {CONFIG_PATH}); "
            "every tick prunes the worktrees that landed"
        ),
    )
    p.add_argument("slug", metavar="owner/name", help="the GitHub repo; --repo is its checkout")
    p.add_argument("--claude-arg", action="append", default=[], help="extra flag for the session (repeatable)")
    p.add_argument(
        "--refresh",
        action="store_true",
        help="move this detached, clean gate checkout to origin's default branch before reading it",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="decide and print; start, stop, move, or prune nothing (lists the worktrees it would prune)",
    )
    p.add_argument("--json", action="store_true", help="print the decision, with the pruned worktrees, as JSON")
    p.add_argument(
        "--app-key",
        type=Path,
        help="the private key of the App in [agents] app_id (default: ~/.config/shipmill/app-<app_id>.pem)",
    )

    p = sub.add_parser(
        "worktrees", help="list every worktree of the repository as REMOVABLE once its work landed, or KEPT and why"
    )
    p.add_argument(
        "--prune",
        action="store_true",
        help="remove each REMOVABLE worktree (never forced) and the local branch it held; print them REMOVED",
    )
    p.add_argument(
        "--dry-run", action="store_true", help="with --prune: print the REMOVABLE ones as WOULD_REMOVE, remove nothing"
    )
    p.add_argument("--json", action="store_true", help="print the worktrees as one JSON object")

    p = sub.add_parser(
        "app-token",
        help="print an installation token of a GitHub App, limited to one repo and cached in the checkout",
    )
    p.add_argument("slug", metavar="owner/name", help="the GitHub repo the token is limited to; --repo is its checkout")
    p.add_argument("--app-id", type=int, required=True, help="the App's id, as [agents] app_id names it")
    p.add_argument("--app-key", type=Path, help="the App's private key (default: ~/.config/shipmill/app-<app_id>.pem)")
    p.add_argument(
        "--git-credential",
        metavar="OPERATION",
        help=(
            "answer git's credential protocol on stdin: get prints username=x-access-token and the token "
            "as password for https://github.com; store, erase, and other operations print nothing"
        ),
    )

    p = sub.add_parser("app-create", help="create the gate's GitHub App in one click (spec 006)")
    p.add_argument(
        "--owner", help="the personal account or org that owns the App (default: asked, else your personal account)"
    )
    visibility = p.add_mutually_exclusive_group()
    visibility.add_argument(
        "--public", dest="public", action="store_const", const=True, help="installable on any account"
    )
    visibility.add_argument(
        "--private", dest="public", action="store_const", const=False, help="installable on the owner only"
    )
    p.add_argument("--name", help="the App's name, unique on GitHub (default: shipmill-<owner>, then shipmill-<login>)")
    p.add_argument("--repos", help="the gated repos as owner/name,...; skips looking for them")
    p.add_argument("--dry-run", action="store_true", help="print the plan; create nothing")
    p.add_argument("--json", action="store_true", help="print the plan and the result as one JSON object")
    p.add_argument("--no-browser", action="store_true", help="print the URLs instead of opening them")

    p = sub.add_parser("app-install", help="guide installing the gate's GitHub App on more repos (spec 007)")
    p.add_argument("repos", nargs="*", metavar="owner/name", help="the repos the App should cover (default: origin's)")
    p.add_argument("--app-id", type=int, help="the App's id (default: [agents] app_id)")
    p.add_argument("--app-key", type=Path, help="the App's private key (default: ~/.config/shipmill/app-<app_id>.pem)")
    p.add_argument("--no-browser", action="store_true", help="print the page instead of opening it")
    p.add_argument("--json", action="store_true", help="print the result as one JSON object")

    p = sub.add_parser("launchd", help="run the gate for a dedicated checkout every few minutes (macOS)")
    p.add_argument("slug", metavar="owner/name", help="the GitHub repo; --repo is its gate checkout")
    p.add_argument("--every", type=int, default=15, help="minutes between runs (default: 15)")
    p.add_argument("--tool", default=DEFAULT_TOOL, help=f"where uvx gets shipmill (default: {DEFAULT_TOOL})")
    p.add_argument("--claude-arg", action="append", default=[], help="extra flag for the session (repeatable)")
    p.add_argument(
        "--app-key",
        type=Path,
        help="pass the private key of the App in [agents] app_id to the job's gate (made absolute)",
    )
    p.add_argument("--print", action="store_true", help="print the job's plist; install nothing")
    p.add_argument("--remove", action="store_true", help="unload and delete the repo's job")
    return parser


def _release_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--lane", required=True, choices=[ln.value for ln in Lane])
    p.add_argument("--version", required=True, type=Version.parse)


def main(
    argv: list[str],
    github: GitHub | None = None,
    http: Http | None = None,
    sessions: Sessions | None = None,
    api: Api | None = None,
    signer: Signer | None = None,
    stdin: TextIO | None = None,
    state: StateRunner | None = None,
) -> int:
    """github stands in for gh, http for the health checks, sessions for `claude agents`, api
    for GitHub's REST API, signer for openssl, stdin for git's credential request, and state
    for watch_state.py, as tests pass fakes"""
    args = _parser().parse_args(argv)
    root: Path = args.repo.resolve()
    if args.command == "app-token":
        return _app_token(root, args, api or UrllibApi(), signer or Openssl(), stdin or sys.stdin)
    if args.command == "app-install":
        return _app_install(root, args, api or UrllibApi(), signer or Openssl())
    if args.command == "app-create":
        ask = input if sys.stdin.isatty() and not args.owner else None
        return _app_create(root, args, api or UrllibApi(), signer or Openssl(), ask=ask)
    if args.command == "status":
        return _status(root, args, state or run_state, api or UrllibApi(), signer or Openssl())
    hub = github or GhCli(root)
    if args.command == "init" and args.operate:
        print(f"wrote {init_operate(root, args.force).relative_to(root)}")
        print(f"next: run `{cli_command()} doctor`")
        return 0
    if args.command == "init":
        initialized = init(root, args.ci, args.force)
        for path in initialized.written:
            print(f"wrote {path.relative_to(root)}")
        print(f"next: review the policy, then run `{cli_command()} doctor`")
        return 0
    if args.command == "doctor":
        slug = _doctor_repo(root)
        plugins = None if slug is None else (lambda: read_plugin_rows(slug, root))
        checks = doctor(root, github or (GhCli(root) if shutil.which("gh") else None), plugins)
        for check in checks:
            print(f"{check.status} {check.name}: {check.detail}")
        return 1 if any(c.status == "FAIL" for c in checks) else 0
    if args.command == "gate":
        return _gate(root, args, hub, sessions or ClaudeSessions(root))
    if args.command == "launchd":
        return _launchd(root, args)
    if args.command == "worktrees":
        if args.dry_run and not args.prune:
            raise ReleaseError("--dry-run goes with --prune")
        git = Git(root)
        judged = judge(git, hub, sessions or ClaudeSessions(root), dt.datetime.now(dt.UTC))
        if args.prune:
            judged = prune(git, judged, args.dry_run)
        sys.stdout.write(json.dumps(worktrees_report(judged), indent=2) + "\n" if args.json else table(judged))
        return 0

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
            github=hub,
            run=ActionsRun.parse(args.workflow_ref, args.run_id) if args.push else None,
        )
        for changed in prepared.changed:
            print(f"stamped {changed}")
        if args.push:
            _report_work_branch(prepared, work_branch(args.version), args.workflow_ref, args.step_summary)
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


def _annotation(text: str) -> str:
    """text as a workflow command's message: GitHub reads %, CR, and LF as escapes"""
    return text.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def _report_work_branch(prepared: Prepared, work: str, workflow_ref: str, step_summary: Path | None) -> None:
    """Make a stop at a work branch on origin, or its recovery, visible: an annotation, a
    step-summary line, and stderr; the run stays green either way (D-18)"""
    if not prepared.found:
        return
    workflow = workflow_ref.partition("@")[0].rsplit("/", 1)[-1] or "the release workflow"
    at = f"{work} is on origin at {prepared.found[:12]}"
    if prepared.stop is None:
        level, title = "notice", "Orphaned work branch replaced"
        text = f"{at}, and no other run of {workflow} is queued or in progress: deleted it and pushed this run's own"
    else:
        level, title = "warning", "Work branch held"
        why = {
            Stop.OWNED: f"another queued or in-progress run of {workflow} may own it",
            Stop.FORBIDDEN: (
                f"this run can't list the runs of {workflow} to tell whether one owns it: grant `actions: read`"
                f" to the prepare job in {CALLER}, so a run deletes a work branch no run owns"
            ),
            Stop.OUTSIDE: "outside a GitHub Actions run nothing tells whether a run owns it",
        }[prepared.stop]
        text = f"{at}; {why}. This run releases nothing; delete the branch if no run uses it"
    print(text, file=sys.stderr)
    print(f"::{level} title={title}::{_annotation(text)}")
    if step_summary is not None:
        with step_summary.open("a", encoding="utf-8") as out:
            out.write(f"{title}: {text}\n\n")


def _gate(root: Path, args: argparse.Namespace, github: GitHub, sessions: Sessions) -> int:
    git = Git(root)
    path = os.environ.get("PATH", "")

    def session_env(identity: Identity) -> dict[str, str]:
        """The helpers go in the checkout's state folder and run this very shipmill"""
        return prepare_session(identity, state_dir(git) / HELPERS, Path(sys.executable), root, args.slug, path)

    decision, launched, waiting, pruned = gate(
        git,
        args.slug,
        lambda: AgentsConfig.load(root),
        ClaudeCli(args.claude_arg),
        lambda read: watch(args.slug, root, read),
        dt.datetime.now(dt.UTC),
        lambda: Hold.read(GhCli(root)),
        Desktop.detect(),
        pruner(git, github, sessions),
        args.refresh,
        args.dry_run,
        app_check(args.slug, args.app_key, Path.home(), Openssl(), UrllibApi()),
        args.app_key is not None,
        session_env,
        lambda: host_login(root),
    )
    if args.json:
        print(json.dumps(tick_record(decision, launched, waiting, pruned, args.dry_run), indent=2))
        return 0
    print("\n".join(tick_lines(decision, launched, waiting, pruned, args.dry_run)))
    return 0


def _app_token(root: Path, args: argparse.Namespace, api: Api, signer: Signer, stdin: TextIO) -> int:
    """Spec 004, Tokens. Never falls back to the host's gh login (D-14): any failure exits 2"""
    app_id: int = args.app_id
    if app_id < 1:
        raise ReleaseError(f"--app-id must be 1 or more, got {app_id}")
    key: Path = args.app_key if args.app_key is not None else default_key(app_id, Path.home())
    cache = state_dir(Git(root)) / CACHE

    def token() -> str:
        return app_token(cache, args.slug, app_id, key, dt.datetime.now(dt.UTC), signer, api)

    if args.git_credential is None:
        print(token())
        return 0
    sys.stdout.write(credential(args.git_credential, stdin.read(), token))
    return 0


def _app_create(
    root: Path,
    args: argparse.Namespace,
    api: Api,
    signer: Signer,
    token: str | None = None,
    browser: Callable[[str], None] | None = None,
    key_dir: Path | None = None,
    install_seconds: float = FLOW_SECONDS,
    out: TextIO | None = None,
    ask: Callable[[str], str] | None = None,
) -> int:
    """Spec 006: discover, plan, create, install. Exit 1 when an installation is still missing"""
    host = token if token is not None else host_token()
    keys = key_dir if key_dir is not None else Path.home() / ".config" / "shipmill"
    stream = out if out is not None else (sys.stderr if args.json else sys.stdout)

    def say(line: str) -> None:
        print(line, file=stream, flush=True)  # S-006-19: each line as it happens, piped or not

    say("looking for the accounts you administer and their repos with .github/shipmill.toml...")
    found = accounts(api, host)
    repos = given_repos(args.repos.split(","), found) if args.repos else gated_repos(api, host, found)
    owner = args.owner
    if owner is None and ask is not None and not args.dry_run and not args.json:
        for line in owner_menu(found, repos):
            say(line)
        owner = pick_owner(found, ask(f"Owner [1-{len(found)}, Enter for {found[0].login}]: "))
    planned = plan(found, repos, owner, args.public, args.name)
    name, note = free_name(api, host, planned, found[0].login, args.name is not None)
    planned = dataclasses.replace(planned, name=name)
    if note is not None:
        say(f"note: {note}")
    record: dict[str, object] = planned.record()
    for line in planned.lines():
        say(line)
    if args.dry_run:
        if args.json:
            print(json.dumps(record, indent=2))
        return 0
    check_key_dir(keys)

    def open_url(url: str) -> None:
        say(f"open {url}")
        if browser is not None:
            browser(url)
        elif not args.no_browser:
            webbrowser.open(url)

    created = convert(create(planned, open_url), api, host, keys)
    say(f"created {created.slug} (App ID {created.app_id}), key in {created.key}")
    targets = planned.installable
    guided = guide(created.app_id, created.key, targets, api, signer, say, open_url, seconds=install_seconds)
    installed = guided.installed
    for line in config_lines(created):
        say(line)
    missing = [r for r in targets if r not in installed]
    for repo in missing:
        say(f"not installed on {repo}; install it at {guided.page}")
    record |= {"app_id": created.app_id, "slug": created.slug, "key": str(created.key), "installed": list(installed)}
    if args.json:
        print(json.dumps(record, indent=2))
    return 1 if missing else 0


def _status(
    root: Path,
    args: argparse.Namespace,
    run: StateRunner,
    api: Api,
    signer: Signer,
    home: Path | None = None,
    platform: str = sys.platform,
    now: dt.datetime | None = None,
) -> int:
    """Specs 008 and 009: the factory's picture for the checkout's repo, or with --rows or
    --json github-ship-watch's report passed through; exit 1 when a row needs action, 2 when
    the script failed rather than reported"""
    git = Git(root)
    if not git.ok("rev-parse", "--show-toplevel"):  # S-008-9: first, before any other git call
        raise ReleaseError(f"{root} is not a git checkout; run status in a checkout of the repo, or pass --repo PATH")
    top = Path(git.run("rev-parse", "--show-toplevel").strip())
    slug: str | None = args.slug
    if slug is None:
        slug = _origin_repo(top)
        if slug is None:
            raise ReleaseError(f"{top}'s origin isn't a GitHub repo; name the repo, such as owner/name")
    else:
        check_checkout(Git(top), slug)
    picture = not (args.rows or args.json)
    home = home if home is not None else Path.home()
    when = now if now is not None else dt.datetime.now(dt.UTC)

    def check(app_id: int, key: Path) -> Identity:
        return app_check(slug, key, home, signer, api)(app_id, when)

    # the picture reads as the gate's App, whose needs-decision questions are its own (spec 005)
    gate = status.read_gate(slug, top, home, platform, run, check, os.getuid()) if picture else None
    login = None if gate is None else gate.login
    proc = run(watch_command(slug, top, StateRead(bot_login=login), json=not args.rows))
    sys.stderr.write(proc.stderr)  # S-008-10: warnings on 0 and 1, the whole traceback on a failure
    if state_failed(proc):
        print(f"shipmill: watch_state.py failed (exit {proc.returncode})", file=sys.stderr)
        return 2
    if gate is None:
        sys.stdout.write(proc.stdout)
        return proc.returncode
    git = Git(top)
    main = status.read_main(git, status.read_branch(slug, run))
    facts = status.Facts(
        repo=slug,
        rows=status.parse_rows(proc.stdout),
        issues=status.read_issues(slug, run, login),
        pulls=status.read_pulls(slug, run),
        main=main,
        version=status.describe(git, main.remote),
        gate=gate,
        cli=importlib.metadata.version("shipmill"),
        now=when,
        checkout=top,
        command=cli_command(),
    )
    sys.stdout.write(status.report(facts).text(slug))
    return proc.returncode  # S-009-14: spec 008's code, whatever the verdict


def _doctor_repo(root: Path) -> str | None:
    """The repo doctor reads the plugin's installs for: the origin's, or None without a GitHub
    origin, which doctor's remote check reports"""
    try:
        return _origin_repo(root)
    except ReleaseError:  # no origin remote
        return None


def _origin_repo(root: Path) -> str | None:
    """The checkout's GitHub origin as owner/name, or None outside a GitHub checkout"""
    if not (root / ".git").exists():
        return None
    url = Git(root).run("remote", "get-url", "origin").strip()
    found = re.search(r"github\.com[:/]([^/]+/[^/]+?)(?:\.git)?/?$", url)
    return found.group(1) if found else None


def _app_install(
    root: Path,
    args: argparse.Namespace,
    api: Api,
    signer: Signer,
    browser: Callable[[str], None] | None = None,
    out: TextIO | None = None,
    clock: Callable[[], dt.datetime] = lambda: dt.datetime.now(dt.UTC),
    sleep: Callable[[float], None] = time.sleep,
    seconds: float = WAIT_SECONDS,
) -> int:
    """Spec 007: guide installing the App on the repos; exit 1 when one is still missing"""
    stream = out if out is not None else (sys.stderr if args.json else sys.stdout)

    def say(line: str) -> None:
        print(line, file=stream, flush=True)  # S-007-7

    repos: list[str] = list(args.repos)
    if not repos:
        origin = _origin_repo(root)
        if origin is None:
            raise ReleaseError(f"{root} is not a GitHub checkout; name the repos, such as owner/name")
        repos = [origin]
    app_id: int | None = args.app_id
    if app_id is None:
        app_id = AgentsConfig.load(root).app_id if (root / CONFIG_PATH).is_file() else None
    if app_id is None:
        raise ReleaseError("no [agents] app_id in this checkout's config; pass --app-id")
    key = check_key(args.app_key if args.app_key is not None else default_key(app_id, Path.home()))

    def browse(url: str) -> None:
        if browser is not None:
            browser(url)
        elif not args.no_browser:
            webbrowser.open(url)

    guided = guide(app_id, key, repos, api, signer, say, browse, clock, sleep, seconds)
    missing = [r for r in repos if r not in guided.installed]
    for repo in missing:
        say(f"not installed on {repo}; install it at {guided.page}")
    if args.json:
        print(json.dumps(guided.record(app_id, repos), indent=2))
    return 1 if missing else 0


def _launchd(root: Path, args: argparse.Namespace) -> int:
    home = Path.home()
    if args.remove:
        removed = remove(args.slug, home)
        print(f"removed {removed}" if removed else f"no launchd job for {args.slug}")
        return 0
    git = Git(root)
    check_checkout(git, args.slug)
    key: Path | None = None if args.app_key is None else args.app_key.expanduser().resolve()
    job = build(args.slug, root, args.every, args.tool, args.claude_arg, home, os.environ.get("PATH", ""), app_key=key)
    if args.print:
        sys.stdout.write(job.document.decode())
        return 0
    refresh(git)  # a dedicated checkout, at the default branch's head; strict: one on a branch is refused
    agents = AgentsConfig.load(root)  # the job would fail on every run without it
    if key is not None:  # as would a key for no App, or one the gate refuses
        if agents.app_id is None:
            raise ReleaseError(f"--app-key names an App's key, but [agents] in {CONFIG_PATH} sets no app_id")
        check_key(key)
    install(job)
    print(f"installed {job.plist}: every {args.every} min, log {job.log}")
    print(f"prompt: {agents.prompt}")
    return 0


def run() -> None:
    try:
        code = main(sys.argv[1:])
    except ReleaseError as exc:
        print(f"shipmill: {exc}", file=sys.stderr)
        code = 2
    sys.exit(code)
