"""Check that a repository is ready for the bot: its policy, CHANGELOG, version files, and the
workflows the bot calls"""

import re
import tomllib
from dataclasses import dataclass
from pathlib import Path

from shipyard.changelog import Changelog
from shipyard.errors import ReleaseError
from shipyard.gitrepo import REMOTE, Git
from shipyard.land import BLOCKING_BRANCH, WORK_PREFIX, blocked
from shipyard.policy import ALIAS_PATH, CONFIG_PATH, BumpFrom, Policy, VersionFiles, config_path
from shipyard.stamp import project_version

CALLER = Path(".github") / "workflows" / "release.yml"
_BOT_WORKFLOWS = ("prepare.yml", "land.yml")
_LOCAL_USES = re.compile(r"uses:\s*\./\.github/workflows/(?P<file>[\w.-]+\.ya?ml)")


@dataclass(frozen=True, slots=True)
class Check:
    status: str  # PASS, WARN, or FAIL
    name: str
    detail: str


def doctor(root: Path) -> list[Check]:
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
    if read == ALIAS_PATH:
        detail = (
            f"{ALIAS_PATH} is the alias; rename it to {CONFIG_PATH} (git mv) once your Release workflow's"
            " `tool` is this release or newer"
        )
        add(False, "config", detail, warn=True)

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
    if not path.is_file():
        add(False, "changelog", f"no {policy.changelog}")
    else:
        try:
            changelog = Changelog(path.read_text(encoding="utf-8"), policy.style)
            pending = changelog.pending()
            unknown = (
                sorted({str(e.heading) for e in pending if e.heading not in policy.bump_headings})
                if policy.bump_from is BumpFrom.HEADINGS
                else []
            )
            add(
                not unknown,
                "changelog",
                f"{len(pending)} pending entries" + (f"; headings missing from [bump]: {unknown}" if unknown else ""),
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
        add(False, "workflow", f"no {CALLER}; `shipyard init` writes one")
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
    for env in policy.environments.values():
        checks.append(_deploy(root / ".github" / "workflows" / env.workflow, env.name))
    return checks


def _deploy(path: Path, environment: str) -> Check:
    """An environment's workflow takes the tag and the environment, and a job names the
    environment, without which GitHub records no deployment"""
    name = f"{path.name} ({environment})"
    if not path.is_file():
        return Check("FAIL", "environment", f"{name}: no .github/workflows/{path.name}")
    text = path.read_text(encoding="utf-8")
    missing = [f"the '{i}' input" for i in ("tag", "environment") if not _takes_input(text, "workflow_dispatch", i)]
    if not _sets_environment(path, set()):
        missing.append("a job with environment: (GitHub records a deployment only then)")
    if missing:
        return Check("FAIL", "environment", f"{name} lacks {', '.join(missing)}")
    return Check("PASS", "environment", f"{name} runs on workflow_dispatch with 'tag' and 'environment' inputs")


def _sets_environment(path: Path, seen: set[Path]) -> bool:
    """Whether a job sets environment: as one of its own keys (not a step's `with:` or an
    `env:` entry), directly or in a local reusable workflow the job calls; a text check"""
    if path in seen or not path.is_file():
        return False
    seen.add(path)
    jobs = re.search(r"^jobs:\s*(?:#.*)?$(?P<body>(?:\n[ \t#].*|\n\s*)*)", path.read_text(encoding="utf-8"), re.M)
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
    m = re.search(rf"^(?P<indent>\s*){event}:\s*(?:#.*)?$(?P<body>(?:\n(?P=indent)\s+.*|\n\s*)*)", text, re.MULTILINE)
    return m is not None and re.search(rf"^\s+{name}:", m["body"], re.MULTILINE) is not None
