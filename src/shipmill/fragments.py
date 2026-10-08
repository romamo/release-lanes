"""Changelog fragments: one file per pull request under the policy's [changelog] fragments
folder, each holding what the PR would have written under Unreleased (spec 013).

A stable release writes them into its CHANGELOG section and deletes them; until then every
command that reads what is pending reads Unreleased plus the fragments, in file-name order.
The planner reads them at a revision through git; prepare and doctor read the checkout; a
hotfix reads the ones each of its merges added, and sync deletes them on main.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from shipmill.changelog import Entry, fragment_entries
from shipmill.config import CONFIG_PATH, loads
from shipmill.errors import ReleaseError
from shipmill.gitrepo import Git
from shipmill.policy import Policy, Style

README = "README.md"  # the file that keeps the folder in git; never a fragment
SUFFIX = ".md"


@dataclass(frozen=True, slots=True)
class Fragment:
    path: str  # relative to the repo root, as <folder>/<name>.md
    entries: tuple[Entry, ...]


class _Source(Protocol):
    """Where the folder's files are read from: a revision, or the checkout"""

    def files(self, folder: str) -> list[str]:
        """Every file under folder, as paths relative to it; ReleaseError naming the folder
        when it doesn't exist"""
        ...

    def text(self, path: str) -> str: ...


@dataclass(frozen=True, slots=True)
class AtRevision:
    git: Git
    rev: str

    def has(self, folder: str) -> bool:
        tree = f"{self.rev}:{folder}"
        return self.git.ok("cat-file", "-e", tree) and self.git.run("cat-file", "-t", tree).strip() == "tree"

    def files(self, folder: str) -> list[str]:
        tree = f"{self.rev}:{folder}"
        if not self.has(folder):
            raise ReleaseError(f"[changelog] fragments names {folder}, which is not a folder at {self.rev[:12]}")
        found = []
        for line in self.git.run("ls-tree", "-r", "-z", tree).split("\0"):
            if not line:
                continue
            meta, name = line.split("\t", 1)
            kind = meta.split()[1]
            if kind != "blob":
                raise ReleaseError(f"{folder}/{name}: a {kind} in the fragments folder; a fragment is a file")
            found.append(name)
        return found

    def text(self, path: str) -> str:
        found = self.git.show(self.rev, path)
        if found is None:
            raise ReleaseError(f"no {path} at {self.rev[:12]}")
        return found


@dataclass(frozen=True, slots=True)
class ChangedBy:
    """The files a commit changed under the folder against its first parent, on one side of
    the diff: after it, the ones it added or modified; before it, the ones it deleted or
    modified. A hotfix's merge ships the entries the first holds and the second lacks (spec
    013), so a renamed or edited fragment ships only what the merge added. Reading only the
    diff never needs the folder at the first parent."""

    git: Git
    rev: str
    before: bool

    def files(self, folder: str) -> list[str]:
        out = self.git.run(
            "diff",
            "--name-only",
            "-z",
            "--no-renames",
            f"--diff-filter={'DM' if self.before else 'AM'}",
            f"{self.rev}^1",
            self.rev,
            "--",
            f"{folder}/",
        )
        return [p[len(folder) + 1 :] for p in out.split("\0") if p]

    def text(self, path: str) -> str:
        return AtRevision(self.git, f"{self.rev}^1" if self.before else self.rev).text(path)


@dataclass(frozen=True, slots=True)
class InCheckout:
    root: Path

    def files(self, folder: str) -> list[str]:
        base = self.root / folder
        if not base.is_dir():
            raise ReleaseError(f"[changelog] fragments names {folder}, which is not a folder in {self.root}")
        return [p.relative_to(base).as_posix() for p in base.rglob("*") if not p.is_dir()]

    def text(self, path: str) -> str:
        return (self.root / path).read_text(encoding="utf-8")


def read(source: _Source, folder: str, style: Style) -> list[Fragment]:
    """The folder's fragments in file-name order; ReleaseError naming the file on one that
    sits in a subfolder, isn't .md, or doesn't read as entries"""
    fragments = []
    for name in sorted(source.files(folder)):
        path = f"{folder}/{name}"
        if "/" in name:
            raise ReleaseError(f"{path}: a fragment sits right in {folder}, not in a subfolder")
        if name == README:
            continue
        if not name.endswith(SUFFIX) or name == SUFFIX:
            raise ReleaseError(f"{path}: not a fragment; a file in {folder} is a <name>{SUFFIX} or {README}")
        fragments.append(Fragment(path, tuple(fragment_entries(source.text(path), style, path))))
    return fragments


def at_revision(git: Git, policy: Policy, rev: str) -> list[Fragment]:
    """The fragments at rev; none when the policy sets no fragments folder, or when rev
    predates the folder (D-27)"""
    if policy.fragments is None:
        return []
    source = AtRevision(git, rev)
    if not source.has(policy.fragments) and _predates_fragments(git, rev):
        return []
    return read(source, policy.fragments, policy.style)


def _predates_fragments(git: Git, rev: str) -> bool:
    """Whether rev, whose tree lacks the folder, is older than fragments: it isn't HEAD, the
    revision shipmill runs on, and its own config, read through git, doesn't set [changelog]
    fragments or doesn't exist. A config there that fails to read fails here (D-27)"""
    if git.sha(rev) == git.sha("HEAD"):
        return False
    path = CONFIG_PATH.as_posix()
    text = git.show(rev, path)
    if text is None:
        return True
    where = f"{path} at {rev[:12]}"
    return Policy.parse(loads(text, where), where).fragments is None


def added_by(git: Git, policy: Policy, merge: str) -> list[Fragment]:
    """The fragments merge added or modified against its first parent, as merge has them;
    none when the policy sets no fragments folder"""
    return [] if policy.fragments is None else read(ChangedBy(git, merge, False), policy.fragments, policy.style)


def removed_by(git: Git, policy: Policy, merge: str) -> list[Fragment]:
    """The fragments merge deleted or modified against its first parent, as the parent has
    them; none when the policy sets no fragments folder"""
    return [] if policy.fragments is None else read(ChangedBy(git, merge, True), policy.fragments, policy.style)


def in_checkout(root: Path, policy: Policy) -> list[Fragment]:
    """The fragments in the checkout; none when the policy sets no fragments folder"""
    return [] if policy.fragments is None else read(InCheckout(root), policy.fragments, policy.style)


def entries(fragments: Sequence[Fragment]) -> list[Entry]:
    return [e for f in fragments for e in f.entries]


def where(policy: Policy) -> str:
    """Where pending entries come from, for a message: 'Unreleased', or 'Unreleased or in <folder>'"""
    return "Unreleased" if policy.fragments is None else f"Unreleased or in {policy.fragments}"
