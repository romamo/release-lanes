"""The [agents] section of the config: what `shipyard gate` starts a Claude Code session
for, and with which prompt. The prompt carries the scope the user grants ("merge when
green" or not), so it is a reviewed, committed decision like [autonomy]. Where the session
runs (which machine, which scheduler) is not here: that belongs to the host"""

import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from shipyard.errors import ReleaseError
from shipyard.policy import ALIAS_PATH, CONFIG_PATH, _Table, config_path

_MAX_RETRY_HOURS = 7 * 24  # a week


@dataclass(frozen=True, slots=True)
class AgentsConfig:
    prompt: str  # the session's prompt; {repo} becomes owner/name
    prs: bool  # open pull requests count as work, for a prompt that lands them
    retry_hours: int  # unchanged findings start a new session after this

    @classmethod
    def parse(cls, table: _Table) -> AgentsConfig:
        table.allow("prompt", "prs", "retry_hours")
        prompt = table.string("prompt").strip()
        if not prompt:
            raise ReleaseError(f"{table.where}: prompt must not be empty")
        retry = table.integer("retry_hours", default=24, low=1, high=_MAX_RETRY_HOURS)
        assert retry is not None  # a default was given
        return cls(prompt=prompt, prs=table.boolean("prs", default=False), retry_hours=retry)

    @classmethod
    def load(cls, root: Path) -> AgentsConfig:
        """The [agents] section alone: a repo may use the gate without the release keys"""
        if not any((root / p).is_file() for p in (CONFIG_PATH, ALIAS_PATH)):
            raise ReleaseError(f"no {CONFIG_PATH} in {root}; add an [agents] section with a prompt to use the gate")
        path = config_path(root)
        try:
            raw: Mapping[str, Any] = tomllib.loads(path.read_text(encoding="utf-8"))
        except tomllib.TOMLDecodeError as exc:
            raise ReleaseError(f"{path}: {exc}") from None
        if "agents" not in raw:
            raise ReleaseError(f"{path} has no [agents] section; add one with a prompt to use the gate")
        return cls.parse(_Table(raw, path.name).table("agents"))
