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

    def files(self, folder: str) -> list[str]:
        tree = f"{self.rev}:{folder}"
        if not self.git.ok("cat-file", "-e", tree) or self.git.run("cat-file", "-t", tree).strip() != "tree":
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
class AddedBy:
    """The files a commit added under the folder, against its first parent: a hotfix's
    merge ships the fragments it added (spec 013). Reading only the diff never needs the
    folder at the first parent."""

    git: Git
    rev: str

    def files(self, folder: str) -> list[str]:
        out = self.git.run(
            "diff",
            "--name-only",
            "-z",
            "--no-renames",
            "--diff-filter=A",
            f"{self.rev}^1",
            self.rev,
            "--",
            f"{folder}/",
        )
        return [p[len(folder) + 1 :] for p in out.split("\0") if p]

    def text(self, path: str) -> str:
        return AtRevision(self.git, self.rev).text(path)


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
    """The fragments at rev; none when the policy sets no fragments folder"""
    return [] if policy.fragments is None else read(AtRevision(git, rev), policy.fragments, policy.style)


def added_by(git: Git, policy: Policy, merge: str) -> list[Fragment]:
    """The fragments merge added against its first parent; none when the policy sets no
    fragments folder"""
    return [] if policy.fragments is None else read(AddedBy(git, merge), policy.fragments, policy.style)


def in_checkout(root: Path, policy: Policy) -> list[Fragment]:
    """The fragments in the checkout; none when the policy sets no fragments folder"""
    return [] if policy.fragments is None else read(InCheckout(root), policy.fragments, policy.style)


def entries(fragments: Sequence[Fragment]) -> list[Entry]:
    return [e for f in fragments for e in f.entries]


def where(policy: Policy) -> str:
    """Where pending entries come from, for a message: 'Unreleased', or 'Unreleased or in <folder>'"""
    return "Unreleased" if policy.fragments is None else f"Unreleased or in {policy.fragments}"
