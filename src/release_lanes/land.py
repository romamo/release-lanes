"""Land a release commit that passed CI: put it where its lane keeps releases, tag it, bring
a stable release made off main back into main, and publish it"""

import datetime as dt
from dataclasses import dataclass

from release_lanes.errors import ReleaseError
from release_lanes.github import GitHub
from release_lanes.gitrepo import Git
from release_lanes.policy import Lane, Policy
from release_lanes.stamp import notes, stamp, sync
from release_lanes.version import Version

WORK_PREFIX = "release-lanes/"  # the branch a release commit waits on while CI runs
_SYNC_ATTEMPTS = 5


def work_branch(version: Version) -> str:
    return f"{WORK_PREFIX}{version.tag}"


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
    if not git.push(f"{sha}:refs/heads/{work_branch(version)}"):
        raise ReleaseError(f"pushing {work_branch(version)} was rejected")
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
        if not git.push(f"{sha}:refs/heads/{policy.branch}"):
            raise ReleaseError(f"pushing {sha[:12]} to {policy.branch} was rejected")
        placed, on_main = policy.branch, True
    elif lane is Lane.HOTFIX:
        branch = f"release/{version.series}"
        current = git.remote_branch(branch)
        if current is not None and current != base:
            raise ReleaseError(f"{branch} moved to {current[:12]} since the hotfix was planned on {base[:12]}")
        if not git.push(f"{sha}:refs/heads/{branch}"):
            raise ReleaseError(f"pushing {sha[:12]} to {branch} was rejected")
        placed = branch

    message = policy.tag_message.format(name=policy.name, version=version)
    git.run("tag", "-a", version.tag, "-m", message, sha)
    if not git.push(f"refs/tags/{version.tag}"):
        raise ReleaseError(f"pushing tag {version.tag} was rejected")

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
    return Landed(version.tag, placed, synced, tuple(published))


def _sync_main(git: Git, policy: Policy, version: Version, released: str, today: dt.date, newest: bool) -> None:
    """Commit the release's CHANGELOG section (and version, when newest) onto main, retrying
    when a merge lands on main between the fetch and the push"""
    for _ in range(_SYNC_ATTEMPTS):
        git.fetch(f"+refs/heads/{policy.branch}:refs/remotes/origin/{policy.branch}")
        git.run("checkout", "-q", "--detach", f"origin/{policy.branch}")
        sync(git, policy, version, released, today, newest)
        git.run("add", "-A")
        git.run("commit", "-q", "-m", f"Sync {version} into {policy.branch}")
        if git.push(f"HEAD:refs/heads/{policy.branch}"):
            return
    raise ReleaseError(f"{policy.branch} kept moving: the sync of {version} failed {_SYNC_ATTEMPTS} times")


def cleanup(git: Git, version: Version) -> bool:
    """Delete the release commit's work branch; whether there was one"""
    if git.remote_branch(work_branch(version)) is None:
        return False
    if not git.push(f":refs/heads/{work_branch(version)}"):
        raise ReleaseError(f"deleting {work_branch(version)} was rejected")
    return True
