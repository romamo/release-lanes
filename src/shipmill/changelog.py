"""A hand-written CHANGELOG: its Unreleased entries, its release sections, and the edit a
stable release makes, which moves entries out of Unreleased into a dated section.

Two styles:

- keep-a-changelog: "## [Unreleased]", "## [1.2.0] - 2026-10-01", "### Added" category
  headings, one top-level bullet per entry (continuation lines indented), and compare
  links at the end
- dash: "## Unreleased", "## 1.2.0 — 2026-10-01", one "### Title" block per entry

Sections the bot doesn't touch are kept byte for byte.
"""

import datetime as dt
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from shipmill.errors import ReleaseError
from shipmill.policy import Style
from shipmill.version import Version

_HEADINGS = {
    Style.KEEP_A_CHANGELOG: (
        "## [Unreleased]",
        re.compile(r"^## \[(?P<version>[^\]]+)\](?: - (?P<date>\S+))?\s*$"),
    ),
    Style.DASH: ("## Unreleased", re.compile(r"^## (?P<version>\d\S*)(?: — (?P<date>\S+))?\s*$")),
}
_RELEASE_LIKE = re.compile(r"^## (\[|\d)")  # a heading that must parse as a release
_LINK = re.compile(r"^\[(?P<name>[^\]]+)\]: (?P<url>\S+)\s*$")
_COMPARE = re.compile(r"^(?P<base>\S+)/compare/(?P<range>\S+)$")
# where an rc section's intro prose ends: its first heading, or a list item (of any marker,
# indented or not) before one
_NOT_PROSE = re.compile(r"^(#|\s*([-*+]|\d+[.)])\s)")


@dataclass(frozen=True, slots=True)
class Entry:
    heading: str | None  # the category heading in keep-a-changelog style; None in dash style
    text: str  # the entry's lines, without trailing blank lines


@dataclass(frozen=True, slots=True)
class _Segment:
    heading: str  # the "## " line, or "" for the text before the first one
    version: Version | None  # a release section's version
    unreleased: bool
    lines: tuple[str, ...]  # the section's lines after its heading, verbatim


class Changelog:
    def __init__(self, text: str, style: Style) -> None:
        self.style = style
        self.unreleased_heading, self._release = _HEADINGS[style]
        lines = text.splitlines()
        self._links: list[str] = []
        if style is Style.KEEP_A_CHANGELOG:
            end = len(lines)
            while end and (not lines[end - 1].strip() or _LINK.match(lines[end - 1])):
                end -= 1
            self._links = [line for line in lines[end:] if line.strip()]
            lines = lines[:end]
        self._segments = _split(lines, self.unreleased_heading, self._release)
        found = [s for s in self._segments if s.unreleased]
        if len(found) != 1:
            raise ReleaseError(f"the CHANGELOG needs exactly one '{self.unreleased_heading}' heading")
        self._unreleased = found[0]

    @classmethod
    def read(cls, text: str, style: Style) -> Changelog:
        return cls(text, style)

    def unreleased(self) -> list[Entry]:
        return _entries(self._unreleased.lines, self.style, strict=True, where="Unreleased")

    def released(self) -> set[Entry]:
        found: set[Entry] = set()
        for segment in self._segments:
            if segment.version is not None:
                found.update(_entries(segment.lines, self.style, strict=False, where=str(segment.version)))
        return found

    def pending(self, fragments: Sequence[Entry] = ()) -> list[Entry]:
        """Unreleased entries that no release section already holds, then the fragments'
        entries (spec 013) that neither a release section nor Unreleased holds"""
        released = self.released()
        found = [e for e in self.unreleased() if e not in released]
        seen = set(found)
        for entry in fragments:
            if entry not in released and entry not in seen:
                found.append(entry)
                seen.add(entry)
        return found

    def versions(self) -> list[Version]:
        return [s.version for s in self._segments if s.version is not None]

    def rc_sections(self, version: Version) -> list[Version]:
        """The [X.Y.ZrcN] sections of stable version X.Y.Z newer than the last stable section,
        newest first: a release bot before shipmill wrote them, and a stable release of
        version folds them into its own section (D-25)"""
        if not version.is_stable:
            raise ReleaseError(f"only a stable version has rc sections to fold, not {version}")
        floor = max((v for v in self.versions() if v.is_stable), default=None)
        return sorted(
            (
                v
                for v in self.versions()
                if v.pre == "rc" and v.dev is None and v.release == version and (floor is None or v > floor)
            ),
            reverse=True,
        )

    def promoted(self, version: Version, fragments: Sequence[Entry] = ()) -> list[Entry]:
        """What a stable release of version holds: the pending Unreleased entries, then the
        pending fragments' entries, then the entries of its rc sections, newest section first,
        each entry once (D-25)"""
        return list(dict.fromkeys([*self.pending(fragments), *self._folded(version)]))

    def _folded(self, version: Version) -> list[Entry]:
        """The entries of version's rc sections, newest section first. The prose before a
        section's first '### ' heading, such as a release bot's "The 3rd 1.0 release
        candidate: ..." line, is dropped (D-26). Folding removes the sections, so any other
        text that is not an entry (prose between entries, a '#### ' heading, a list item before
        the first heading) fails here rather than vanish"""
        found: list[Entry] = []
        for rc in self.rc_sections(version):
            lines = self.section(rc).splitlines()
            start = next((i for i, line in enumerate(lines) if _NOT_PROSE.match(line)), len(lines))
            found.extend(_entries(lines[start:], self.style, strict=True, where=f"{rc}, folded into {version}"))
        return found

    def section(self, version: Version) -> str:
        found = [s for s in self._segments if s.version == version]
        if len(found) != 1:
            raise ReleaseError(f"the CHANGELOG needs exactly one section for {version}, found {len(found)}")
        return "\n".join(found[0].lines).strip("\n") + "\n"

    def section_entries(self, version: Version) -> list[Entry]:
        return _entries(self.section(version).splitlines(), self.style, strict=False, where=str(version))

    def render(self, entries: Sequence[Entry]) -> str:
        """Entries as a section body: grouped under their headings in keep-a-changelog style"""
        if self.style is Style.DASH:
            return "\n\n".join(e.text for e in entries)
        groups: dict[str | None, list[str]] = {}
        for entry in entries:
            groups.setdefault(entry.heading, []).append(entry.text)
        parts = []
        for heading, texts in groups.items():
            body = "\n".join(texts)
            parts.append(body if heading is None else f"### {heading}\n\n{body}")
        return "\n\n".join(parts)

    def release(
        self,
        version: Version,
        date: dt.date,
        entries: Sequence[Entry],
        *,
        from_unreleased: bool,
        fragments: Sequence[Entry] = (),
    ) -> str:
        """The CHANGELOG text with entries moved out of Unreleased into a section for version.
        With from_unreleased, every entry must be under Unreleased or in fragments (the
        fragments' entries, spec 013); otherwise (a hotfix's entries on its release branch)
        the ones that are get removed. The rc sections of version are folded in: entries must
        hold every one of their entries, which may come from them as well as from Unreleased,
        and the sections are removed (D-25)."""
        if not version.is_stable:
            raise ReleaseError(f"only a stable release gets a CHANGELOG section, not {version}")
        if not entries:
            raise ReleaseError(f"no entries to release as {version}")
        if version in self.versions():
            raise ReleaseError(f"the CHANGELOG already has a section for {version}")
        rcs = self.rc_sections(version)
        folded = set(rcs)
        held = self._folded(version)
        current = self.unreleased()
        moved = set(entries)
        sources = set(current) | set(held) | set(fragments)
        if from_unreleased and (missing := [e for e in entries if e not in sources]):
            places = [
                "Unreleased",
                *(["a fragment"] if fragments else []),
                *([f"an rc section of {version}"] if folded else []),
            ]
            where = " or ".join(places) if len(places) < 3 else f"{places[0]}, {places[1]}, or {places[2]}"
            raise ReleaseError(
                f"{len(missing)} released entr{'y is' if len(missing) == 1 else 'ies are'} no longer under "
                f"{where} as released (edited after the release was cut?): {missing[0].text.splitlines()[0]!r}"
            )
        if dropped := [e for e in held if e not in moved]:
            raise ReleaseError(
                f"{version} folds its rc sections, but {len(dropped)} of their entries are not in the release:"
                f" {dropped[0].text.splitlines()[0]!r}"
            )
        remaining = [e for e in current if e not in moved]
        new_heading = (
            f"## [{version}] - {date.isoformat()}"
            if self.style is Style.KEEP_A_CHANGELOG
            else f"## {version} — {date.isoformat()}"
        )
        older = [
            s.version
            for s in self._segments
            if s.version is not None and s.version < version and s.version not in folded
        ]
        insert_before = max(older) if older else None
        out: list[str] = []
        placed = False
        for segment in self._segments:
            if segment.version is not None and segment.version == insert_before and not placed:
                out.append(_block(new_heading, self.render(entries)))
                placed = True
            if segment.version in folded:
                continue
            if segment.unreleased:
                out.append(_block(segment.heading, self.render(remaining)))
            elif segment.heading:
                out.append("\n".join((segment.heading, *segment.lines)) + "\n")
            else:
                out.append("\n".join(segment.lines) + "\n" if segment.lines else "")
        if not placed:
            out.append(_block(new_heading, self.render(entries)))
        text = "".join(out).rstrip("\n") + "\n"
        if self.style is Style.KEEP_A_CHANGELOG:
            text += "\n" + "\n".join(self._linked(version, insert_before, folded)) + "\n"
        return text

    def _linked(self, version: Version, previous: Version | None, folded: set[Version]) -> list[str]:
        """The links with version's added and the folded rc sections' removed"""
        gone = tuple(f"[{rc}]: " for rc in folded)
        links = [line for line in self._links if not line.startswith(gone)]
        index = next((i for i, line in enumerate(links) if line.startswith("[Unreleased]: ")), None)
        m = None if index is None else _COMPARE.match(links[index].split(": ", 1)[1])
        if index is None or m is None or not m["range"].endswith("...HEAD"):
            raise ReleaseError("the CHANGELOG needs an '[Unreleased]: <repo>/compare/v<version>...HEAD' link")
        base = m["base"]
        newest = max(self.versions() + [version])
        if newest == version:
            links[index] = f"[Unreleased]: {base}/compare/{version.tag}...HEAD"
        line = (
            f"[{version}]: {base}/compare/{previous.tag}...{version.tag}"
            if previous is not None
            else f"[{version}]: {base}/releases/tag/{version.tag}"
        )
        after = next(
            (i for i, existing in enumerate(links) if previous is not None and existing.startswith(f"[{previous}]: ")),
            None,
        )
        links.insert(after if after is not None else len(links), line)
        return links


def _block(heading: str, body: str) -> str:
    return f"{heading}\n\n{body}\n\n" if body else f"{heading}\n\n"


def _split(lines: list[str], unreleased: str, release: re.Pattern[str]) -> list[_Segment]:
    segments: list[_Segment] = []
    heading, start = "", 0
    for i, line in enumerate([*lines, "## <end>"]):
        if not line.startswith("## "):
            continue
        if i > 0 or heading:
            segments.append(_segment(heading, tuple(lines[start:i]), unreleased, release))
        heading, start = line, i + 1
    return segments


def _segment(heading: str, lines: tuple[str, ...], unreleased: str, release: re.Pattern[str]) -> _Segment:
    if heading.rstrip() == unreleased:
        return _Segment(heading, None, True, lines)
    m = release.match(heading)
    if m is None:
        if _RELEASE_LIKE.match(heading):
            raise ReleaseError(
                f"CHANGELOG heading {heading!r} looks like a release but is not '<version> - YYYY-MM-DD'"
            )
        return _Segment(heading, None, False, lines)
    if m["date"] is not None:
        try:
            dt.date.fromisoformat(m["date"])
        except ValueError:
            raise ReleaseError(f"CHANGELOG heading {heading!r}: {m['date']!r} is not a YYYY-MM-DD date") from None
    return _Segment(heading, Version.parse(m["version"]), False, lines)


def fragment_entries(text: str, style: Style, path: str) -> list[Entry]:
    """A changelog fragment's entries (spec 013), read as strictly as Unreleased; errors name
    the file and line"""
    lines = text.splitlines()
    if not any(line.strip() for line in lines):
        raise ReleaseError(f"{path}: an empty fragment; it holds the entries a PR adds, as under Unreleased")
    for n, line in enumerate(lines, 1):
        if line.startswith("## "):
            raise ReleaseError(
                f"{path}:{n}: a '## ' heading; a fragment holds only what goes under Unreleased: {line!r}"
            )
    found = _entries(lines, style, strict=True, where=path, numbered=True)
    if not found:
        raise ReleaseError(f"{path}: the fragment holds no entry")
    return found


def _entries(lines: Iterable[str], style: Style, *, strict: bool, where: str, numbered: bool = False) -> list[Entry]:
    """numbered: errors name the line's number after where, as in 'path:3'"""
    entries: list[Entry] = []
    heading: str | None = None
    current: list[str] = []
    blanks: list[str] = []

    def flush() -> None:
        if current:
            entries.append(Entry(heading if style is Style.KEEP_A_CHANGELOG else None, "\n".join(current)))
            current.clear()
        blanks.clear()

    for n, line in enumerate(lines, 1):
        at = f"{where}:{n}" if numbered else where
        if style is Style.DASH:
            if line.startswith("### "):
                flush()
                current.append(line)
            elif current:
                if line.strip():
                    current.extend(blanks)
                    blanks.clear()
                    current.append(line)
                else:
                    blanks.append(line)
            elif line.strip() and strict:
                raise ReleaseError(f"{at}: text outside a '### ' entry: {line!r}")
            continue
        if line.startswith("### "):
            flush()
            heading = line[4:].strip()
        elif line.startswith(("- ", "* ")):
            flush()
            if heading is None and strict:
                raise ReleaseError(f"{at}: an entry has no '### ' category heading: {line!r}")
            current.append(line)
        elif not line.strip():
            if current:
                blanks.append(line)
        elif current and line[0].isspace():
            current.extend(blanks)
            blanks.clear()
            current.append(line)
        else:
            flush()
            if strict:
                raise ReleaseError(f"{at}: text outside a '- ' entry: {line!r}")
    flush()
    return entries
