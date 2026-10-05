"""Release windows such as "Mon-Fri 07:00 Europe/Kyiv": a lane's schedule is due once a
window has opened and the lane has not released since"""

import datetime as dt
import re
from dataclasses import dataclass
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from shipmill.errors import ReleaseError

_DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
_WINDOW = re.compile(r"(?P<days>\S+)\s+(?P<hour>\d\d):(?P<minute>\d\d)\s+(?P<zone>\S+)")


@dataclass(frozen=True, slots=True)
class Window:
    text: str
    weekdays: frozenset[int]  # 0 = Monday
    time: dt.time
    zone: ZoneInfo

    @classmethod
    def parse(cls, text: str) -> Window:
        m = _WINDOW.fullmatch(text.strip())
        if m is None:
            raise ReleaseError(f"a window is '<days> HH:MM <zone>', such as 'Mon-Fri 07:00 UTC', got {text!r}")
        hour, minute = int(m["hour"]), int(m["minute"])
        if hour > 23 or minute > 59:
            raise ReleaseError(f"window {text!r}: {hour:02}:{minute:02} is not a time of day")
        try:
            zone = ZoneInfo(m["zone"])
        except ZoneInfoNotFoundError:
            raise ReleaseError(f"window {text!r}: unknown time zone {m['zone']!r}") from None
        return cls(text, _weekdays(m["days"], text), dt.time(hour, minute), zone)

    def latest_start(self, now: dt.datetime) -> dt.datetime | None:
        """The newest window start at or before now, within the last week"""
        local = now.astimezone(self.zone)
        for back in range(8):
            day = local.date() - dt.timedelta(days=back)
            if day.weekday() not in self.weekdays:
                continue
            start = dt.datetime.combine(day, self.time, tzinfo=self.zone)
            if start <= local:
                return start.astimezone(dt.UTC)
        return None


def _weekdays(spec: str, text: str) -> frozenset[int]:
    if spec.lower() == "daily":
        return frozenset(range(7))
    days: set[int] = set()
    for part in spec.lower().split(","):
        ends = part.split("-")
        if len(ends) > 2 or not all(e in _DAYS for e in ends):
            raise ReleaseError(f"window {text!r}: days are 'daily', 'Mon', 'Mon-Fri', or 'Mon,Wed', got {spec!r}")
        first, last = _DAYS.index(ends[0]), _DAYS.index(ends[-1])
        if last < first:
            raise ReleaseError(f"window {text!r}: day range {part!r} runs backwards")
        days.update(range(first, last + 1))
    return frozenset(days)


@dataclass(frozen=True, slots=True)
class Freeze:
    """An inclusive range of UTC dates, 'YYYY-MM-DD..YYYY-MM-DD', when a lane holds"""

    first: dt.date
    last: dt.date

    @classmethod
    def parse(cls, text: str) -> Freeze:
        ends = text.split("..")
        if len(ends) != 2:
            raise ReleaseError(f"a freeze is 'YYYY-MM-DD..YYYY-MM-DD', got {text!r}")
        try:
            first, last = (dt.date.fromisoformat(e.strip()) for e in ends)
        except ValueError:
            raise ReleaseError(f"freeze {text!r}: not YYYY-MM-DD dates") from None
        if last < first:
            raise ReleaseError(f"freeze {text!r} ends before it starts")
        return cls(first, last)

    def holds(self, now: dt.datetime) -> bool:
        return self.first <= now.astimezone(dt.UTC).date() <= self.last
