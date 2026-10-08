"""Check that a repository is ready for the bot: its policy, CHANGELOG, version files, and the
workflows the bot calls"""

import importlib.util
import re
import sys
import tomllib
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from shipmill import cli_command, fragments
from shipmill.agent_as_person import NAME as AGENT_AS_PERSON
from shipmill.agent_as_person import Flagged
from shipmill.autonomy import HOLD_LABEL, Autonomy, Hold
from shipmill.changelog import Changelog
from shipmill.config import config_path
from shipmill.errors import ReleaseError
from shipmill.gate import skills_dir
from shipmill.github import GitHub
from shipmill.gitrepo import REMOTE, Git
from shipmill.land import BLOCKING_BRANCH, WORK_PREFIX, blocked
from shipmill.policy import BumpFrom, Lane, Policy, VersionFiles
from shipmill.stamp import project_version

CALLER = Path(".github") / "workflows" / "release.yml"
OPERATE_CALLER = Path(".github") / "workflows" / "operate.yml"  # calls shipmill's operate.yml on a schedule
_BOT_WORKFLOWS = ("prepare.yml", "land.yml")
_LOCAL_USES = re.compile(r"uses:\s*\./\.github/workflows/(?P<file>[\w.-]+\.ya?ml)")


@dataclass(frozen=True, slots=True)
class Check:
    status: str  # PASS, WARN, or FAIL
    name: str
    detail: str


@dataclass(frozen=True, slots=True)
class PluginRow:
    """One of github-ship-watch's SHIPMILL_VERSION or SHIPMILL_OUTDATED rows"""

    state: str
    subject: str
    detail: str


PluginRows = Callable[[], list[PluginRow]]  # the rows, or ReleaseError when they can't be read
PeopleReader = Callable[[], Flagged]  # spec 012's flagged items, or ReleaseError when they can't be read


def read_plugin_rows(repo: str, root: Path) -> list[PluginRow]:
    """watch_state.py's shipmill_rows for the checkout (D-22): the same installs, latest
    release (`gh release view`), and fix text as `shipmill status`, read in this process"""
    script = skills_dir() / "github-ship-watch" / "scripts" / "watch_state.py"
    spec = importlib.util.spec_from_file_location("shipmill_watch_state", script)
    if spec is None or spec.loader is None:
        raise ReleaseError(f"can't load {script}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # its dataclasses look their module up by name
    spec.loader.exec_module(module)
    try:
        rows = module.shipmill_rows(repo, root)
    except SystemExit as exc:  # watch_state's way to refuse input or report a failed command
        raise ReleaseError(f"watch_state.py stopped: {exc if str(exc) != '2' else 'see the error above'}") from None
    except OSError as exc:  # gh or git missing
        raise ReleaseError(str(exc)) from None
    return [PluginRow(r.state, r.subject, r.detail) for r in rows]


def plugin_checks(read: PluginRows) -> list[Check]:
    """The shipmill@shipmill installs on this host that apply to the repo, as `status` reads
    them: each one behind the latest release is a WARN with the fix to run in its folder (the
    repo's, the gate checkout's, or user scope); a failed read is a WARN, never a crash"""
    try:
        rows = read()
    except ReleaseError as exc:
        return [Check("WARN", "plugin", f"can't read the shipmill plugin's installs: {exc}")]
    checks = []
    for row in rows:
        if row.state == "SHIPMILL_VERSION":
            checks.append(Check("PASS", "plugin", row.detail))
        elif row.state == "SHIPMILL_OUTDATED":
            checks.append(Check("WARN", "plugin", f"{row.subject} {row.detail}"))
        else:
            raise ReleaseError(f"watch_state.py's shipmill_rows returned a {row.state} row")
    return checks


def agent_as_person_check(read: PeopleReader) -> Check:
    """S-012-7: a WARN naming the agent-marked items a person wrote, with the fix; a failed
    read is a WARN that says so, never a crash or a silent PASS"""
    try:
        found = read()
    except ReleaseError as exc:
        return Check("WARN", AGENT_AS_PERSON, f"can't read the last 7 days' issues, pull requests, and comments: {exc}")
    if found.urls:
        return Check("WARN", AGENT_AS_PERSON, found.detail())
    return Check("PASS", AGENT_AS_PERSON, "no agent-marked item of the last 7 days was written by a person")


def doctor(
    root: Path,
    github: GitHub | None = None,
    plugins: PluginRows | None = None,
    people: PeopleReader | None = None,
) -> list[Check]:
    """github reads the open hold; None (no gh) reports that it can't. plugins reads the
    shipmill plugin's installs on this host; None checks none. people reads spec 012's
    AGENT_AS_PERSON items, only when [agents] app_id is set; None checks none"""
    checks: list[Check] = []

    def add(ok: bool, name: str, detail: str, warn: bool = False) -> None:
        checks.append(Check("PASS" if ok else ("WARN" if warn else "FAIL"), name, detail))

    try:
        found = config_path(root)
        policy = Policy.load(found)
    except ReleaseError as exc:
        add(False, "policy", str(exc))
        return checks
    read = found.relative_to(root)
    add(True, "policy", f"{read}, mode {policy.mode}, lanes {', '.join(policy.lanes)}")
    if policy.roadmap is not None:
        add(True, "roadmap", f"{policy.roadmap}; read by the product-intake skill")
    autonomy, hold = _autonomy(policy, github, root / CALLER)
    checks.extend(autonomy)
    if (operated := _operate(policy, hold, root, _operate_caller(root))) is not None:
        checks.append(operated)

    git = Git(root)
    add(git.ok("remote", "get-url", "origin"), "remote", "an 'origin' remote to push releases to")
    checks.append(_work_branch(git))
    tags = git.tags()
    stable = [t.version for t in tags if t.version.is_stable]
    add(
        bool(stable),
        "tags",
        f"latest stable {max(stable).tag}" if stable else "no stable vX.Y.Z tag yet",
        warn=True,
    )
    if policy.bump_from is BumpFrom.PATHS and not stable:
        add(False, "bump", "bump from = 'paths' diffs against the last stable tag; tag one first")

    path = root / policy.changelog
    if policy.fragments is not None and not (root / policy.fragments).is_dir():
        # S-013-2: a policy naming a folder that isn't there is broken, not a check to report
        raise ReleaseError(f"[changelog] fragments names {policy.fragments}, which is not a folder in {root}")
    if not path.is_file():
        add(False, "changelog", f"no {policy.changelog}")
    else:
        try:
            changelog = Changelog(path.read_text(encoding="utf-8"), policy.style)
            pieces = fragments.in_checkout(root, policy)
            pending = changelog.pending(fragments.entries(pieces))
            unknown = (
                sorted({str(e.heading) for e in pending if e.heading not in policy.bump_headings})
                if policy.bump_from is BumpFrom.HEADINGS
                else []
            )
            held = (
                ""
                if policy.fragments is None
                else f" (Unreleased and {len(pieces)} fragment{'' if len(pieces) == 1 else 's'} in {policy.fragments})"
            )
            add(
                not unknown,
                "changelog",
                f"{len(pending)} pending entries{held}"
                + (f"; headings missing from [bump]: {unknown}" if unknown else ""),
            )
            released = changelog.released()
            for fragment in pieces:
                if all(e in released for e in fragment.entries):
                    add(
                        False,
                        "fragments",
                        f"{fragment.path}: a released section holds every entry; delete it: git rm {fragment.path}",
                        warn=True,
                    )
        except ReleaseError as exc:
            add(False, "changelog", str(exc))

    if policy.version_files is VersionFiles.PYPROJECT:
        try:
            _, version = project_version(root)
            tagged = {max(t.version for t in tags), max(stable)} if stable else set()
            add(
                not tagged or version in tagged,
                "version",
                f"pyproject.toml declares {version}; tags: {', '.join(sorted(v.tag for v in tagged)) or 'none'}",
                warn=True,
            )
        except (ReleaseError, OSError) as exc:
            add(False, "version", str(exc))

    pyproject = root / "pyproject.toml"
    if pyproject.is_file():
        try:
            local = _local_sources(pyproject)
            add(
                not local,
                "sources",
                f"[tool.uv.sources] points at a local checkout, which CI and users don't have: {', '.join(local)}"
                if local
                else "no path or editable [tool.uv.sources]",
            )
        except tomllib.TOMLDecodeError as exc:
            add(False, "sources", f"pyproject.toml: {exc}")

    for line in policy.version_lines:
        target = root / line.file
        hits = len(line.pattern.findall(target.read_text(encoding="utf-8"))) if target.is_file() else 0
        add(hits == 1, "version_lines", f"{line.file}: {line.pattern.pattern!r} matches {hits} time(s)")

    caller = root / CALLER
    if not caller.is_file():
        add(False, "workflow", f"no {CALLER}; `{cli_command()} init` writes one")
    else:
        text = caller.read_text(encoding="utf-8")
        uses_bot = all(re.search(rf"uses:\s*\S*/\.github/workflows/{name}\b", text) for name in _BOT_WORKFLOWS)
        add(uses_bot, "workflow", f"{CALLER} calls prepare.yml and land.yml")
        for m in _LOCAL_USES.finditer(text):
            if m["file"] in _BOT_WORKFLOWS:  # the bot's own, when a repository hosts it
                continue
            ci = root / ".github" / "workflows" / m["file"]
            ok = ci.is_file() and _takes_input(ci.read_text(encoding="utf-8"), "workflow_call", "ref")
            add(ok, "ci", f"{m['file']} runs on workflow_call with a 'ref' input to check out")
    for rule in policy.lanes.values():
        for workflow in rule.dispatch:
            target = root / ".github" / "workflows" / workflow
            ok = target.is_file() and _takes_input(target.read_text(encoding="utf-8"), "workflow_dispatch", "tag")
            add(ok, "dispatch", f"{workflow} ({rule.lane}) runs on workflow_dispatch with a 'tag' input")
    operate_caller = _operate_caller(root)
    for env in policy.environments.values():
        watched = env.source is not None or bool(env.health)  # D-9: only these need operate
        checks.append(_deploy(root / ".github" / "workflows" / env.workflow, env.name, operate_caller, watched))
    if plugins is not None:
        checks.extend(plugin_checks(plugins))
    if people is not None and policy.agents is not None and policy.agents.app_id is not None:
        checks.append(agent_as_person_check(people))
    return checks


def _deploy(path: Path, environment: str, caller: Path, operated: bool) -> Check:
    """An environment's workflow takes the tag and the environment, and a job names the
    environment, without which GitHub records no deployment. A WARN when no step fails a run
    off the tag it deploys (#80), and when an environment operate watches (D-9) has a workflow
    that doesn't start the operate caller after the deploy, for a first check right away (#84)"""
    name = f"{path.name} ({environment})"
    if not path.is_file():
        return Check("FAIL", "environment", f"{name}: no .github/workflows/{path.name}")
    text = path.read_text(encoding="utf-8")
    missing = [f"the '{i}' input" for i in ("tag", "environment") if not _takes_input(text, "workflow_dispatch", i)]
    if not _sets_environment(path, set()):
        missing.append("a job with environment: (GitHub records a deployment only then)")
    if missing:
        return Check("FAIL", "environment", f"{name} lacks {', '.join(missing)}")
    passed = [f"{name} runs on workflow_dispatch with 'tag' and 'environment' inputs"]
    warned = []
    if _REF_GUARD.search(text):
        passed.append("fails a run that isn't on the tag")
    else:
        warned.append(
            "no step fails a run whose github.ref isn't refs/tags/<tag>: a run started without --ref <tag>"
            " deploys the tag under the branch's ref, which names no release tag (the README's deploy workflow"
            " shows the guard)"
        )
    starts = _starts(text, caller.name)
    if starts is True:
        passed.append(f"starts {caller.name} after the deploy")
    elif operated and starts is False:
        warned.append(f"it starts {caller.name} as a dry run: add -f dry-run=false")
    elif operated:
        warned.append(
            f"it doesn't start {caller.name} after the deploy, so the first health check waits for the"
            f" schedule: add a job that needs the deploy job and runs `gh workflow run {caller.name}"
            " -f dry-run=false` with actions: write"
        )
    detail = "; ".join(passed)
    if warned:
        return Check("WARN", "environment", f"{detail}, but {'; and '.join(warned)}")
    return Check("PASS", "environment", detail)


# the guard a deploy workflow runs: a line that compares the run's ref (github.ref or
# GITHUB_REF; ref_name doesn't tell a branch from a tag) with refs/tags/<tag>, in either order
_REF_GUARD = re.compile(r"^(?=.*(?:\bgithub\.ref|\bGITHUB_REF)\b)(?=.*refs/tags/).*$", re.MULTILINE)


def _starts(text: str, workflow: str) -> bool | None:
    """Whether a deploy workflow starts the workflow with -f dry-run=false: False when it
    starts it only as a dry run, None when it doesn't start it. A text check that reads a
    command's backslash-continued lines as one"""
    command = re.compile(rf"\bgh\s+workflow\s+run\s+(?:\S*/)?{re.escape(workflow)}(?![\w.-])")
    lines = [line for line in text.replace("\\\n", " ").splitlines() if command.search(line)]
    if not lines:
        return None
    return any(re.search(r"\bdry-run=false\b", line) for line in lines)


def _sets_environment(path: Path, seen: set[Path]) -> bool:
    """Whether a job sets environment: as one of its own keys (not a step's `with:` or an
    `env:` entry), directly or in a local reusable workflow the job calls; a text check"""
    if path in seen or not path.is_file():
        return False
    seen.add(path)
    # the body runs to the first line that starts at column 0; a blank line doesn't end it
    jobs = re.search(r"^jobs:[ \t]*(?:#.*)?$(?P<body>(?:\n(?:[ \t#].*)?)*)", path.read_text(encoding="utf-8"), re.M)
    job_indent: int | None = None
    key_indent: int | None = None
    for line in jobs["body"].splitlines() if jobs else ():
        key = line.strip()
        if not key or key.startswith("#"):
            continue
        indent = len(line) - len(line.lstrip(" "))
        if job_indent is None or indent <= job_indent:
            job_indent, key_indent = indent, None  # a job's id
            continue
        if key_indent is None:
            key_indent = indent
        if indent != key_indent:
            continue
        if key.startswith("environment:"):
            return True
        if (local := _LOCAL_USES.match(key)) and _sets_environment(path.parent / local["file"], seen):
            return True
    return False


def _autonomy(policy: Policy, github: GitHub | None, caller: Path) -> tuple[list[Check], Hold]:
    """The effective autonomy per stage, and the stop switch: an open hold is a WARN, and so
    is a caller that can't open the issue a hold or propose leads to, when one can"""
    checks = []
    if github is None:
        hold = Hold()
        checks.append(Check("WARN", "hold", f"can't read issues for {HOLD_LABEL}: gh isn't installed"))
    else:
        try:
            hold = Hold.read(github)
        except ReleaseError as exc:
            hold = Hold()
            checks.append(Check("WARN", "hold", f"can't read issues for {HOLD_LABEL}: {exc}"))
        else:
            if hold.on:
                detail = (
                    f"{hold.reason}: no release, deploy, or rollback acts by itself, and only a hotfix can be"
                    " started by hand; close it to resume"
                )
                checks.append(Check("WARN", "hold", detail))
            else:
                checks.append(Check("PASS", "hold", f"no open {HOLD_LABEL} issue"))
    levels = [f"{stage} {policy.autonomy.effective(stage, hold)}" for stage in policy.autonomy.stages()]
    default = Autonomy.PROPOSE if hold.on else Autonomy.ACT
    other = "any other" if policy.autonomy.deploy else "every"
    detail = ", ".join(levels) + f"; {other} deploy environment {default}"
    if hold.on:
        detail += f" ({hold.reason})"
    detail += "; deploy and rollback act in shipmill operate"
    detail += f"; intake {policy.autonomy.intake} in the product-intake skill"
    checks.append(Check("PASS", "autonomy", detail))
    proposes = any(policy.autonomy.configured(stage) is Autonomy.PROPOSE for stage in policy.autonomy.stages())
    if (
        (proposes or hold.on)
        and caller.is_file()
        and _job_grants(caller.read_text("utf-8"), "prepare.yml", "issues") is False
    ):
        why = hold.reason if hold.on else "the config sets a stage to propose"
        detail = (
            f"{why}, so a run opens a proposal issue, but the prepare job in {CALLER} grants no"
            " `issues: write`: change its `issues: read` to `issues: write`"
        )
        checks.append(Check("WARN", "permissions", detail))
    if (
        policy.autonomy.release is Autonomy.PROPOSE
        and caller.is_file()
        and _job_grants(caller.read_text("utf-8"), "land.yml", "issues") is False
    ):
        detail = (
            "release autonomy is propose, so a release closes its lane's proposal issue, but the land job in"
            f" {CALLER} grants no `issues: write`: add it"
        )
        checks.append(Check("WARN", "permissions", detail))
    return checks, hold


def _operate_caller(root: Path) -> Path:
    """The workflow that runs shipmill operate, relative to root: OPERATE_CALLER, except in the
    repository hosting shipmill, whose .github/workflows/operate.yml is the reusable workflow
    itself; there, the first workflow calling it locally (uses: ./.github/workflows/operate.yml),
    as its Release workflow calls prepare.yml and land.yml"""
    hosted = root / OPERATE_CALLER
    if not (hosted.is_file() and _takes_input(hosted.read_text(encoding="utf-8"), "workflow_call", "tool")):
        return OPERATE_CALLER
    for path in sorted(hosted.parent.iterdir()):
        if path != hosted and path.suffix in (".yml", ".yaml") and _LOCAL_OPERATE.search(path.read_text("utf-8")):
            return path.relative_to(root)
    return OPERATE_CALLER


_LOCAL_OPERATE = re.compile(rf"^\s+uses:\s*\./\.github/workflows/{re.escape(OPERATE_CALLER.name)}\b", re.MULTILINE)


def _operate(policy: Policy, hold: Hold, root: Path, caller: Path) -> Check | None:
    """The operate caller (relative to root), only when an environment is promoted or has a
    health URL (D-9): it runs on a schedule, writes deployment statuses, and starts deploy
    workflows"""
    used = [e.name for e in policy.environments.values() if e.source is not None or e.health]
    if not used:
        return None
    why = f"{', '.join(used)} {'uses' if len(used) == 1 else 'use'} from or health"
    if not (root / caller).is_file():
        detail = f"{why}, but no {caller} runs shipmill operate: `{cli_command()} init --operate` writes it"
        return Check("WARN", "operate", detail)
    text = (root / caller).read_text(encoding="utf-8")
    if _job_grants(text, "operate.yml", "deployments") is None:
        return Check("WARN", "operate", f"{why}, but no job in {caller} calls shipmill's operate.yml")
    needed = ["deployments", "actions"]
    proposes = any(policy.autonomy.configured(s) is Autonomy.PROPOSE for s in policy.autonomy.stages())
    if proposes or hold.on or policy.incident_label:
        # the proposal issue a deploy under propose or the hold opens, and the incident a
        # failing environment with a health URL opens
        needed.append("issues")
    missing = [f"{name}: write" for name in needed if not _job_grants(text, "operate.yml", name)]
    if missing:
        return Check("WARN", "operate", f"the job in {caller} that calls operate.yml lacks {', '.join(missing)}")
    detail = f"{caller} runs shipmill operate with {', '.join(needed)}: write"
    if policy.incident_label:
        held = ", ".join(lane for lane in Lane if lane in policy.blocker_lanes)
        detail += (
            f"; rolls back after {policy.operate.rollback_after} failed checks in a row and opens an"
            f" incident labelled {policy.incident_label!r}, which holds {held}"
        )
    return Check("PASS", "operate", detail)


_JOB = re.compile(r"^  (?P<name>[\w-]+):[ \t]*(?:#.*)?$", re.MULTILINE)


def _job_grants(text: str, workflow: str, permission: str) -> bool | None:
    """Whether the caller's job that calls shipmill's workflow grants `<permission>: write`;
    None when no job calls it. A text check, not a YAML parse: a job runs to the next
    two-space key"""
    starts = [m.start() for m in _JOB.finditer(text)] + [len(text)]
    for start, end in zip(starts, starts[1:], strict=False):
        job = text[start:end]
        if re.search(rf"^\s+uses:\s*\S*\.github/workflows/{re.escape(workflow)}\b", job, re.MULTILINE):
            grant = rf"^\s+(?:{permission}:\s*write|permissions:\s*write-all)\b"
            return re.search(grant, job, re.MULTILINE) is not None
    return None


def _work_branch(git: Git) -> Check:
    """Asks origin itself, as prepare does: a shallow, single-branch, or stale clone's
    refs/remotes don't show every branch on origin"""
    work = f"{WORK_PREFIX}<tag>"
    if not git.ok("remote", "get-url", REMOTE):
        return Check("WARN", "work branch", f"no '{REMOTE}' remote to ask for a branch '{BLOCKING_BRANCH}'")
    try:
        on_origin = git.remote_branch(BLOCKING_BRANCH) is not None
    except ReleaseError as exc:
        return Check("WARN", "work branch", f"can't ask {REMOTE} for a branch '{BLOCKING_BRANCH}': {exc}")
    if on_origin:
        return Check("FAIL", "work branch", blocked(work))
    if git.ok("show-ref", "--verify", "-q", f"refs/heads/{BLOCKING_BRANCH}"):
        detail = f"a local branch '{BLOCKING_BRANCH}' would block the work branch {work} once pushed to {REMOTE}"
        return Check("WARN", "work branch", detail)
    return Check("PASS", "work branch", f"no branch '{BLOCKING_BRANCH}' on {REMOTE} to block {work}")


def _local_sources(pyproject: Path) -> list[str]:
    """The [tool.uv.sources] packages taken from a local path, editable or not"""
    sources = tomllib.loads(pyproject.read_text(encoding="utf-8")).get("tool", {}).get("uv", {}).get("sources", {})
    local = []
    for name, source in sources.items():
        # a package may list several sources, one per marker
        entries = source if isinstance(source, list) else [source]
        if any(isinstance(e, dict) and ("path" in e or e.get("editable")) for e in entries):
            local.append(name)
    return sorted(local)


def _takes_input(text: str, event: str, name: str) -> bool:
    """Whether a workflow's event block declares the input; a text check, not a YAML parse"""
    # the block runs to the first line indented no deeper than the event; a blank line doesn't end it
    m = re.search(
        rf"^(?P<indent>[ \t]*){event}:[ \t]*(?:#.*)?$(?P<body>(?:\n(?:(?P=indent)[ \t]+.*|[ \t]*$))*)", text, re.M
    )
    return m is not None and re.search(rf"^\s+{name}:", m["body"], re.MULTILINE) is not None
