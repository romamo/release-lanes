"""Write a release into a checkout: the stamp a release commit carries (version files,
version lines, and for a stable release its CHANGELOG section), and the sync commit that
brings a stable release made off main back into main's CHANGELOG"""

import datetime as dt
import re
import subprocess
import tomllib
from collections.abc import Sequence
from pathlib import Path

from shipmill import fragments
from shipmill.changelog import Changelog, Entry
from shipmill.errors import ReleaseError
from shipmill.gitrepo import Git
from shipmill.policy import Lane, Policy, VersionFiles
from shipmill.version import Version


def project_version(root: Path) -> tuple[str, Version]:
    table = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8")).get("project", {})
    if "version" not in table:
        raise ReleaseError("pyproject.toml has no static [project] version for version_files = 'pyproject'")
    return str(table["name"]), Version.parse(str(table["version"]))


def replace_once(text: str, pattern: re.Pattern[str], new: str, where: str) -> str:
    hits = len(pattern.findall(text))
    if hits != 1:
        raise ReleaseError(f"{where}: {pattern.pattern!r} matches {hits} times, want exactly 1")
    return pattern.sub(lambda _: new, text)


def _edit(path: Path, pattern: re.Pattern[str], new: str, where: str) -> None:
    path.write_text(replace_once(path.read_text(encoding="utf-8"), pattern, new, where), encoding="utf-8")


def write_version(root: Path, policy: Policy, version: Version, date: dt.date) -> list[str]:
    """Point the version files and version lines at version"""
    changed = []
    if policy.version_files is VersionFiles.PYPROJECT:
        name, current = project_version(root)
        if current != version:
            _edit(
                root / "pyproject.toml",
                re.compile(rf'^version = "{re.escape(str(current))}"$', re.MULTILINE),
                f'version = "{version}"',
                "pyproject.toml",
            )
            changed.append("pyproject.toml")
            lock = root / "uv.lock"
            if lock.is_file():
                _edit(
                    lock,
                    re.compile(rf'(?<=name = "{re.escape(name)}"\nversion = ")[^"]+(?=")'),
                    str(version),
                    "uv.lock",
                )
                changed.append("uv.lock")
    values = {
        "name": policy.name,
        "version": str(version),
        "semver": version.semver,
        "minor": version.series,
        "date": date.isoformat(),
    }
    for line in policy.version_lines:
        target = root / line.file
        if not target.is_file():
            raise ReleaseError(f"version_lines names {line.file}, which doesn't exist")
        _edit(target, line.pattern, line.replace.format(**values), line.file)
        changed.append(line.file)
    return changed


def run_after_stamp(root: Path, policy: Policy) -> None:
    for argv in policy.after_stamp:
        proc = subprocess.run(argv, cwd=root, capture_output=True, text=True)
        if proc.returncode != 0:
            raise ReleaseError(
                f"after_stamp {' '.join(argv)!r} exited {proc.returncode}: {(proc.stderr or proc.stdout).strip()}"
            )


def hotfix_entries(git: Git, policy: Policy, merges: Sequence[str]) -> list[Entry]:
    """The entries each merge added under main's Unreleased, then those it added to the
    fragments it changed (spec 013): a fragment it only renamed adds nothing"""
    entries: list[Entry] = []
    for merge in merges:
        after = _changelog(git, policy, merge).pending(fragments.entries(fragments.added_by(git, policy, merge)))
        gone = fragments.entries(fragments.removed_by(git, policy, merge))
        before = set(_changelog(git, policy, f"{merge}^1").pending(gone))
        added = [e for e in after if e not in before]
        if not added:
            also = "" if policy.fragments is None else f" or fragment in {policy.fragments}"
            raise ReleaseError(f"{merge[:12]} adds no CHANGELOG entry{also}: a hotfix ships entries it can name")
        entries.extend(e for e in added if e not in entries)
    return entries


def apply_merges(git: Git, policy: Policy, merges: Sequence[str]) -> None:
    """Apply each merge's change, except to the CHANGELOG and the fragments folder, to the
    checkout's index"""
    kept_off = [f":(exclude){policy.changelog}"]
    if policy.fragments is not None:
        kept_off.append(f":(exclude){policy.fragments}")
    for merge in merges:
        patch = git.run_bytes("diff", "--binary", f"{merge}^1", merge, "--", ".", *kept_off)
        if not patch.strip():
            continue
        problem = git.apply(patch)
        if problem:
            raise ReleaseError(f"{merge[:12]} does not apply to the release branch: {problem}")


def _changelog(git: Git, policy: Policy, rev: str) -> Changelog:
    text = git.show(rev, policy.changelog)
    if text is None:
        raise ReleaseError(f"no {policy.changelog} at {rev}")
    return Changelog(text, policy.style)


def stamp(
    git: Git, policy: Policy, lane: Lane, version: Version, date: dt.date, merges: Sequence[str] = ()
) -> list[str]:
    """Stamp the checkout (at the release's base) as version; returns the files changed"""
    root = git.root
    changed: list[str] = []
    entries: list[Entry] = []
    if lane is Lane.HOTFIX:
        entries = hotfix_entries(git, policy, merges)
        apply_merges(git, policy, merges)
        changed.append("(merged changes)")
    path = root / policy.changelog
    if lane in (Lane.STABLE, Lane.HOTFIX):
        changelog = Changelog(path.read_text(encoding="utf-8"), policy.style)
        found: list[fragments.Fragment] = []
        if lane is Lane.STABLE:
            found = fragments.in_stamped_checkout(git, policy)  # none at a base older than the folder (D-27)
            # Unreleased, the fragments, and the version's rc sections (D-25, spec 013)
            entries = changelog.promoted(version, fragments.entries(found))
        path.write_text(
            changelog.release(
                version, date, entries, from_unreleased=lane is Lane.STABLE, fragments=fragments.entries(found)
            ),
            encoding="utf-8",
        )
        changed.append(policy.changelog)
        released = set(entries)
        for fragment in found:
            if any(e in released for e in fragment.entries):
                (root / fragment.path).unlink()
                changed.append(fragment.path)
    changed += write_version(root, policy, version, date)
    run_after_stamp(root, policy)
    return changed


def sync(git: Git, policy: Policy, version: Version, released: str, date: dt.date, newest: bool) -> list[str]:
    """Bring a stable release cut off main into main's checkout: move its entries from
    Unreleased into its section, delete the fragments whose every entry it holds (spec 013),
    and when it is the newest stable, point the version at it. released is the CHANGELOG as
    the release commit has it."""
    root = git.root
    stamped = Changelog(released, policy.style)
    entries = stamped.section_entries(version)
    path = root / policy.changelog
    main = Changelog(path.read_text(encoding="utf-8"), policy.style)
    found = fragments.in_checkout(root, policy)
    path.write_text(
        main.release(version, date, entries, from_unreleased=True, fragments=fragments.entries(found)),
        encoding="utf-8",
    )
    changed = [policy.changelog]
    held = set(entries)
    for fragment in found:
        # a fragment with an entry the section lacks stays: that entry is still pending
        if all(e in held for e in fragment.entries):
            (root / fragment.path).unlink()
            changed.append(fragment.path)
    if newest:
        changed += write_version(root, policy, version, date)
    run_after_stamp(root, policy)
    return changed


def notes(
    policy: Policy,
    changelog_text: str,
    version: Version,
    since: Version | None,
    fragment_entries: Sequence[Entry] = (),
) -> str:
    """A release's notes: a stable release's CHANGELOG section, or what a pre-release holds
    beyond the last stable release: its pending Unreleased and fragment entries"""
    changelog = Changelog(changelog_text, policy.style)
    if version.is_stable:
        return changelog.section(version)
    pending = changelog.pending(fragment_entries)
    head = f"Changes since {since.tag}:" if since else "Changes:"
    if not pending:
        return f"{head} no CHANGELOG entries.\n"
    return f"{head}\n\n{changelog.render(pending)}\n"
