"""Versions the bot reads and cuts: X.Y.Z with an optional a, b, or rc pre-release and an
optional .devN, the PEP 440 subset that package installers order correctly"""

import functools
import re
from dataclasses import dataclass, replace
from enum import StrEnum

from release_lanes.errors import ReleaseError

PATTERN = re.compile(r"(\d+)\.(\d+)\.(\d+)(?:(a|b|rc)(\d+))?(?:\.dev(\d+))?")
_SEMVER_PRE = {"a": "alpha", "b": "beta", "rc": "rc"}
_PRE_RANK = {"a": 1, "b": 2, "rc": 3}
TAG_PREFIX = "v"


class Part(StrEnum):
    PATCH = "patch"
    MINOR = "minor"
    MAJOR = "major"

    @property
    def rank(self) -> int:
        return list(Part).index(self)


@functools.total_ordering
@dataclass(frozen=True, slots=True)
class Version:
    major: int
    minor: int
    patch: int
    pre: str | None = None  # "a", "b", or "rc"
    pre_n: int | None = None
    dev: int | None = None

    def __post_init__(self) -> None:
        if (self.pre is None) != (self.pre_n is None):
            raise ReleaseError(f"a pre-release needs both its marker and number: {self.pre!r} {self.pre_n!r}")
        if self.pre is not None and self.pre not in _PRE_RANK:
            raise ReleaseError(f"a pre-release marker is a, b, or rc, got {self.pre!r}")

    @classmethod
    def parse(cls, text: str) -> Version:
        m = PATTERN.fullmatch(text)
        if m is None:
            raise ReleaseError(f"not an X.Y.Z[{{a,b,rc}}N][.devN] version: {text!r}")
        pre_n = None if m[5] is None else int(m[5])
        dev = None if m[6] is None else int(m[6])
        return cls(int(m[1]), int(m[2]), int(m[3]), m[4], pre_n, dev)

    @classmethod
    def of_tag(cls, tag: str) -> Version:
        if not tag.startswith(TAG_PREFIX):
            raise ReleaseError(f"a release tag starts with {TAG_PREFIX!r}, got {tag!r}")
        return cls.parse(tag[len(TAG_PREFIX) :])

    def __str__(self) -> str:
        text = f"{self.major}.{self.minor}.{self.patch}"
        if self.pre is not None:
            text += f"{self.pre}{self.pre_n}"
        if self.dev is not None:
            text += f".dev{self.dev}"
        return text

    def _key(self) -> tuple[int, int, int, int, int, int]:
        # PEP 440: X.Y.Z.devN < X.Y.ZaN < X.Y.ZbN < X.Y.ZrcN < X.Y.Z, and a .devN sorts
        # before the release it is attached to
        if self.pre is None:
            phase, n = (0, 0) if self.dev is not None else (4, 0)
        else:
            assert self.pre_n is not None
            phase, n = _PRE_RANK[self.pre], self.pre_n
        dev = self.dev if self.dev is not None else 2**62
        return (self.major, self.minor, self.patch, phase, n, dev)

    def __lt__(self, other: object) -> bool:
        if not isinstance(other, Version):
            return NotImplemented
        return self._key() < other._key()

    @property
    def tag(self) -> str:
        return f"{TAG_PREFIX}{self}"

    @property
    def is_stable(self) -> bool:
        return self.pre is None and self.dev is None

    @property
    def release(self) -> Version:
        """The stable version this one leads to: 1.4.0 for 1.4.0rc2.dev7"""
        return Version(self.major, self.minor, self.patch)

    @property
    def series(self) -> str:
        return f"{self.major}.{self.minor}"

    @property
    def semver(self) -> str:
        text = f"{self.major}.{self.minor}.{self.patch}"
        if self.pre is not None:
            text += f"-{_SEMVER_PRE[self.pre]}.{self.pre_n}"
        if self.dev is not None:
            text += f"{'.' if self.pre is not None else '-'}dev.{self.dev}"
        return text

    def bump(self, part: Part) -> Version:
        """The next stable version; before 1.0 a major bump moves the minor"""
        if not self.is_stable:
            raise ReleaseError(f"only a stable version bumps, got {self}")
        if part is Part.MAJOR and self.major > 0:
            return Version(self.major + 1, 0, 0)
        if part in (Part.MAJOR, Part.MINOR):
            return Version(self.major, self.minor + 1, 0)
        return Version(self.major, self.minor, self.patch + 1)

    def with_pre(self, marker: str, n: int) -> Version:
        return replace(self.release, pre=marker, pre_n=n)

    def with_dev(self, n: int) -> Version:
        return replace(self, dev=n)


ZERO = Version(0, 0, 0)
