"""Land a release commit that passed CI: put it where its lane keeps releases, tag it, bring
a stable release made off main back into main, and publish it"""

import datetime as dt
import time
from dataclasses import dataclass

from shipmill.environments import deployed_from
from shipmill.errors import ReleaseError
from shipmill.github import GitHub
from shipmill.gitrepo import Git
from shipmill.policy import Lane, Policy
from shipmill.stamp import notes, stamp, sync
from shipmill.version import Version

WORK_PREFIX = "shipmill/"  # the branch a release commit waits on while CI runs
# A branch named the prefix itself blocks every work branch: git can't hold both
# refs/heads/X and refs/heads/X/...
BLOCKING_BRANCH = WORK_PREFIX.rstrip("/")
_SYNC_ATTEMPTS = 5
_TAG_RETRY_WAITS = (2, 5, 10, 0)  # seconds after each failed attempt; the last is not waited


def work_branch(version: Version) -> str:
    return f"{WORK_PREFIX}{version.tag}"


def blocked(work: str) -> str:
    """The problem and the fix when origin's BLOCKING_BRANCH stands in the way of a work branch"""
    return f"origin has a branch '{BLOCKING_BRANCH}', which blocks the work branch {work}; delete or rename it"


@dataclass(frozen=True, slots=True)
class Prepared:
    changed: tuple[str, ...]
    sha: str  # empty unless committed
    pushed: bool  # the work branch was pushed; False when another run holds it


def prepare(
    git: Git,
    policy: Policy,
    lane: Lane,
    version: Version,
    base: str,
    today: dt.date,
    merges: tuple[str, ...] = (),
    prs: tuple[int, ...] = (),
    commit: bool = False,
    push: bool = False,
) -> Prepared:
    """Check out base, stamp it as version, and optionally commit and push the work branch"""
    if git.dirty():
        raise ReleaseError("the checkout has uncommitted changes; prepare stamps a clean checkout")
    git.run("checkout", "-q", "--detach", base)
    changed = tuple(stamp(git, policy, lane, version, today, merges))
    if not commit:
        return Prepared(changed, "", False)
    git.run("add", "-A")
    subject = f"Release {version}"
    if prs:
        subject += f" (hotfix: {', '.join(f'#{p}' for p in prs)})"
    git.run("commit", "-q", "-m", subject)
    sha = git.sha()
    if not push:
        return Prepared(changed, sha, False)
    if git.remote_branch(work_branch(version)) is not None:
        return Prepared(changed, sha, False)
    if git.remote_branch(BLOCKING_BRANCH) is not None:
        raise ReleaseError(blocked(work_branch(version)))
    if error := git.push(f"{sha}:refs/heads/{work_branch(version)}"):
        raise ReleaseError(f"pushing {work_branch(version)} was rejected: {error}")
    return Prepared(changed, sha, True)


@dataclass(frozen=True, slots=True)
class Landed:
    tag: str
    placed: str  # where the release commit went: main, release/X.Y, or the tag alone
    synced: bool  # a sync commit brought the release into main
    published: tuple[str, ...]


def land(
    git: Git,
    policy: Policy,
    github: GitHub,
    lane: Lane,
    version: Version,
    sha: str,
    base: str,
    today: dt.date,
    retry_waits: tuple[int, ...] = _TAG_RETRY_WAITS,
) -> Landed:
    rule = policy.rule(lane)
    work = work_branch(version)
    git.fetch(f"+refs/heads/{work}:refs/remotes/origin/{work}")
    if git.sha(f"origin/{work}") != sha:
        raise ReleaseError(f"{work_branch(version)} is not at {sha[:12]}: another run replaced it")
    if git.remote_tag(version.tag):
        raise ReleaseError(f"{version.tag} exists on origin already")
    if git.first_parent(sha) != base:
        raise ReleaseError(f"{sha[:12]} is not a release commit on {base[:12]}")

    placed = "tag only"
    on_main = False
    if lane is Lane.STABLE and git.remote_branch(policy.branch) == base:
        if error := git.push(f"{sha}:refs/heads/{policy.branch}"):
            raise ReleaseError(f"pushing {sha[:12]} to {policy.branch} was rejected: {error}")
        placed, on_main = policy.branch, True
    elif lane is Lane.HOTFIX:
        branch = f"release/{version.series}"
        current = git.remote_branch(branch)
        if current is not None and current != base:
            raise ReleaseError(f"{branch} moved to {current[:12]} since the hotfix was planned on {base[:12]}")
        if error := git.push(f"{sha}:refs/heads/{branch}"):
            raise ReleaseError(f"pushing {sha[:12]} to {branch} was rejected: {error}")
        placed = branch

    message = policy.tag_message.format(name=policy.name, version=version)
    git.run("tag", "-a", version.tag, "-m", message, sha)
    _push_tag(git, version, sha, retry_waits)

    released = git.show(sha, policy.changelog)
    if released is None:
        raise ReleaseError(f"no {policy.changelog} in {sha[:12]}")
    synced = False
    if lane in (Lane.STABLE, Lane.HOTFIX) and not on_main:
        newest = all(t.version <= version for t in git.tags() if t.version.is_stable)
        _sync_main(git, policy, version, released, today, newest)
        synced = True

    stable = [t.version for t in git.tags() if t.version.is_stable and t.version < version]
    since = max(stable) if stable else None
    published = []
    if rule.github_release:
        title = policy.release_title.format(name=policy.name, version=version)
        github.create_release(version.tag, title, notes(policy, released, version, since), lane.is_pre)
        published.append("github-release")
    for workflow in rule.dispatch:
        github.dispatch(workflow, policy.branch, version.tag)
        published.append(workflow)
    for env in deployed_from(policy.environments, lane):
        # on the tag, so the deployment GitHub records names it as its ref: operate reads it there
        github.dispatch(env.workflow, version.tag, version.tag, env.inputs)
        published.append(f"{env.workflow}@{env.name}")
    return Landed(version.tag, placed, synced, tuple(published))


def _push_tag(git: Git, version: Version, sha: str, retry_waits: tuple[int, ...]) -> None:
    """Push the release tag. A rejection is retried: by then the release commit is on its
    branch, so a tag that never arrives leaves a release no later run can finish"""
    errors = []
    for wait in retry_waits:
        if not (error := git.push(f"refs/tags/{version.tag}")):
            return
        errors.append(error)
        if git.remote_tag(version.tag):
            git.fetch(f"+refs/tags/{version.tag}:refs/tags/{version.tag}")
            if git.sha(version.tag) == sha:
                return  # the push landed although git reported an error
            raise ReleaseError(f"{version.tag} appeared on origin at another commit: {error}")
        time.sleep(wait)
    raise ReleaseError(f"pushing tag {version.tag} was rejected {len(errors)} times: {errors[-1]}")


def _sync_main(git: Git, policy: Policy, version: Version, released: str, today: dt.date, newest: bool) -> None:
    """Commit the release's CHANGELOG section (and version, when newest) onto main, retrying
    when a merge lands on main between the fetch and the push"""
    for _ in range(_SYNC_ATTEMPTS):
        git.fetch(f"+refs/heads/{policy.branch}:refs/remotes/origin/{policy.branch}")
        git.run("checkout", "-q", "--detach", f"origin/{policy.branch}")
        sync(git, policy, version, released, today, newest)
        git.run("add", "-A")
        git.run("commit", "-q", "-m", f"Sync {version} into {policy.branch}")
        if not git.push(f"HEAD:refs/heads/{policy.branch}"):
            return
    raise ReleaseError(f"{policy.branch} kept moving: the sync of {version} failed {_SYNC_ATTEMPTS} times")


def cleanup(git: Git, version: Version) -> bool:
    """Delete the release commit's work branch; whether there was one"""
    if git.remote_branch(work_branch(version)) is None:
        return False
    if error := git.push(f":refs/heads/{work_branch(version)}"):
        raise ReleaseError(f"deleting {work_branch(version)} was rejected: {error}")
    return True
