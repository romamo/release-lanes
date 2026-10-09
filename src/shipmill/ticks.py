"""The scheduled ticks the Release workflow needs, read from the policy (spec 015): hourly
when a trigger needs polling, one cron per window time when the lanes are schedule-only,
and none when every run starts from a push or by hand"""

import datetime as dt
import re
from collections.abc import Iterable
from dataclasses import dataclass

from shipmill import UVX
from shipmill.policy import Lane, Policy

DAYS = 366  # a year from the given day, so a zone with daylight saving time shows both offsets
HOURLY = "7 * * * *"
_HOURLY_BLOCK = f'  schedule:\n    - cron: "{HOURLY}" # hourly: the policy decides whether a lane\'s window is open\n'
# the form `cron:` lines take in a workflow, quoted or not, with an optional comment
_CRON_LINE = re.compile(r"""^\s*-?\s*cron:\s*(?P<q>["']?)(?P<cron>[^"'#\n]+?)(?P=q)\s*(?:#.*)?$""", re.MULTILINE)
_EVERY_HOUR = re.compile(r"[0-9*/,-]+ \* \* \* \*")


@dataclass(frozen=True, slots=True)
class Tick:
    """A cron at one UTC minute and hour on some weekdays"""

    minute: int
    hour: int
    weekdays: tuple[int, ...]  # cron weekdays in ascending order, 0 = Sunday

    def __str__(self) -> str:
        return f"{self.minute} {self.hour} * * {','.join(str(d) for d in self.weekdays)}"


@dataclass(frozen=True, slots=True)
class Ticks:
    """What the Release workflow's schedule must hold: hourly, or the windows' ticks (none
    at all when both are empty)"""

    hourly: bool
    windows: tuple[Tick, ...] = ()  # sorted by hour, then minute; empty when hourly

    def lines(self) -> tuple[str, ...]:
        """The cron lines, in the order release.yml lists them"""
        return (HOURLY,) if self.hourly else tuple(str(t) for t in self.windows)

    def yaml(self) -> str:
        """release.yml's `schedule:` block under `on:`, or nothing when no tick is needed"""
        if self.hourly:
            return _HOURLY_BLOCK
        if not self.windows:
            return ""
        crons = "".join(f'    - cron: "{t}"\n' for t in self.windows)
        return (
            "  schedule:\n"
            "    # shipmill derived these from the policy's windows in .github/shipmill.toml, in UTC; after\n"
            "    # changing the windows, rewrite this file with the same --ci and --runs-on:\n"
            f"    # {UVX} init --caller --force\n"
            f"{crons}"
        )


HOURLY_TICKS = Ticks(hourly=True)


def needed(policy: Policy, today: dt.date) -> Ticks:
    """The ticks the policy's triggers need: hourly when a lane sets milestone, or the stable
    lane promotes with quiet_minutes, since neither ends with a push; else one per UTC minute
    and hour the windows open at over DAYS days from today. The hotfix lane is cut by hand"""
    rules = [rule for lane, rule in policy.lanes.items() if lane is not Lane.HOTFIX]
    if any(r.milestone or (r.lane is Lane.STABLE and r.promote and r.quiet_minutes is not None) for r in rules):
        return HOURLY_TICKS
    days: dict[tuple[int, int], set[int]] = {}
    for window in (w for r in rules for w in r.schedule):
        for offset in range(DAYS):
            start = window.start_on(today + dt.timedelta(days=offset))
            if start is not None:
                days.setdefault((start.hour, start.minute), set()).add((start.weekday() + 1) % 7)
    ticks = tuple(Tick(minute, hour, tuple(sorted(d))) for (hour, minute), d in sorted(days.items()))
    return Ticks(hourly=False, windows=ticks)


def crons(text: str) -> tuple[str, ...]:
    """The `cron:` lines of a workflow, their fields joined by one space"""
    return tuple(" ".join(m["cron"].split()) for m in _CRON_LINE.finditer(text))


def missing(needs: Ticks, present: Iterable[str]) -> tuple[str, ...]:
    """The needed cron lines the present ones don't cover: a cron of the form `M * * * *`
    covers every need; a window's tick is covered by crons at its minute and hour whose
    weekdays together hold its own. Extra crons are never a lack (D-9)"""
    found = list(present)
    if any(_EVERY_HOUR.fullmatch(c) for c in found):
        return ()
    if needs.hourly:
        return (HOURLY,)
    at: dict[tuple[int, int], set[int]] = {}
    for cron in found:
        if (parsed := _parse(cron)) is not None:
            minute, hour, weekdays = parsed
            at.setdefault((minute, hour), set()).update(weekdays)
    return tuple(str(t) for t in needs.windows if not set(t.weekdays) <= at.get((t.minute, t.hour), set()))


def _parse(cron: str) -> tuple[int, int, set[int]] | None:
    """A cron at one minute and hour on every day of the month and year: its minute, hour,
    and cron weekdays (7 read as 0, Sunday); None for any other form"""
    fields = cron.split()
    if len(fields) != 5 or fields[2:4] != ["*", "*"]:
        return None
    minute, hour, weekdays = fields[0], fields[1], fields[4]
    if not (minute.isdigit() and hour.isdigit() and int(minute) < 60 and int(hour) < 24):
        return None
    if weekdays == "*":
        return int(minute), int(hour), set(range(7))
    days: set[int] = set()
    for part in weekdays.split(","):
        m = re.fullmatch(r"([0-7])(?:-([0-7]))?", part)
        if m is None:
            return None
        first, last = int(m[1]), int(m[2] if m[2] else m[1])
        if last < first:
            return None
        days.update(d % 7 for d in range(first, last + 1))
    return int(minute), int(hour), days
