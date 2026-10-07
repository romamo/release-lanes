"""The [agents] section of the config: what `shipmill gate` starts a Claude Code session
for, and with which prompt. The prompt carries the scope the user grants ("merge when
green" or not), so it is a reviewed, committed decision like [autonomy]. Where the session
runs (which machine, which scheduler) is not here: that belongs to the host"""

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from shipmill.config import CONFIG_PATH, Table, config_path, read
from shipmill.errors import ReleaseError

_MAX_HOURS = 7 * 24  # a week
_REMIND_HOURS = 4
_MAX_WAIT_MINUTES = 15


class Mode(StrEnum):
    """How a gate session meets a decision for the user (spec 005)"""

    INTERACTIVE = "interactive"  # `claude --bg`: it asks you and waits
    HEADLESS = "headless"  # `claude -p`: it asks on GitHub and ends (D-17)


@dataclass(frozen=True, slots=True)
class AgentsConfig:
    prompt: str  # the session's prompt; {repo} becomes owner/name
    prs: bool  # open pull requests count as work, for a prompt that lands them
    retry_hours: int  # unchanged findings start a new session after this
    notify: bool = True  # a desktop notification when a session waits on you
    remind_hours: int = _REMIND_HOURS  # repeat it while the session still waits
    max_wait_minutes: int = _MAX_WAIT_MINUTES  # stop a session that waited this long; 0: never
    app_id: int | None = None  # sessions write as this GitHub App (spec 004, D-14); None: the host's gh login
    mode: Mode = Mode.INTERACTIVE  # headless: `claude -p` (spec 005); either mode asks via needs-decision (D-21)
    plugin_update: bool = False  # update the gate checkout's plugin install, once a day (D-22); False: never

    @classmethod
    def parse(cls, table: Table) -> AgentsConfig:
        table.allow(
            "prompt",
            "prs",
            "retry_hours",
            "notify",
            "remind_hours",
            "max_wait_minutes",
            "app_id",
            "mode",
            "plugin_update",
        )
        prompt = table.string("prompt").strip()
        if not prompt:
            raise ReleaseError(f"{table.where}: prompt must not be empty")
        retry = table.integer("retry_hours", default=24, low=1, high=_MAX_HOURS)
        remind = table.integer("remind_hours", default=_REMIND_HOURS, low=1, high=_MAX_HOURS)
        max_wait = table.integer("max_wait_minutes", default=_MAX_WAIT_MINUTES, low=0, high=_MAX_HOURS * 60)
        assert retry is not None and remind is not None and max_wait is not None  # defaults were given
        return cls(
            prompt=prompt,
            prs=table.boolean("prs", default=False),
            retry_hours=retry,
            notify=table.boolean("notify", default=True),
            remind_hours=remind,
            max_wait_minutes=max_wait,
            app_id=table.integer("app_id", default=None, low=1, high=None),
            mode=table.enum("mode", Mode, default=Mode.INTERACTIVE),
            plugin_update=table.boolean("plugin_update", default=False),
        )

    @classmethod
    def load(cls, root: Path) -> AgentsConfig:
        """The [agents] section alone: a repo may use the gate without the release keys"""
        if not (root / CONFIG_PATH).is_file():
            raise ReleaseError(f"no {CONFIG_PATH} in {root}; add an [agents] section with a prompt to use the gate")
        path = config_path(root)
        raw = read(path)
        if "agents" not in raw:
            raise ReleaseError(f"{path} has no [agents] section; add one with a prompt to use the gate")
        return cls.parse(Table(raw, path.name).table("agents"))
