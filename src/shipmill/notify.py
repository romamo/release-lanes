"""A desktop notification: `osascript` on macOS, `notify-send` elsewhere. The title and body
are always arguments, never part of a script or a shell string, since a body names a
session id that comes from `claude agents --json`"""

import shutil
import subprocess
import sys
from collections.abc import Callable, Sequence
from typing import Protocol

TIMEOUT = 30.0  # seconds a send may take
NO_NOTIFIER = "no notifier (osascript or notify-send)"
OSASCRIPT = (
    "osascript",
    "-e",
    "on run argv",
    "-e",
    "display notification (item 2 of argv) with title (item 1 of argv)",
    "-e",
    "end run",
)
NOTIFY_SEND = ("notify-send",)


class NotifyFailed(Exception):
    """A send that failed: no notifier, a missing command, a non-zero exit, or a timeout.
    The gate prints it and goes on; it never changes the tick's decision"""


Runner = Callable[[Sequence[str], float], None]


def run(cmd: Sequence[str], timeout: float) -> None:
    """Run a notifier command; raise NotifyFailed when it can't be run, fails, or hangs"""
    try:
        proc = subprocess.run(list(cmd), capture_output=True, text=True, check=False, timeout=timeout)
    except FileNotFoundError:
        raise NotifyFailed(f"{cmd[0]} not found") from None
    except subprocess.TimeoutExpired:
        raise NotifyFailed(f"{cmd[0]} ran longer than {timeout:g}s") from None
    if proc.returncode != 0:
        why = (proc.stderr or proc.stdout).strip()[:300]
        raise NotifyFailed(f"{cmd[0]} exited {proc.returncode}" + (f": {why}" if why else ""))


class Notifier(Protocol):
    def send(self, title: str, body: str) -> None: ...


class Desktop:
    """The platform's notifier, chosen once; None when the platform has none"""

    def __init__(self, command: tuple[str, ...] | None, runner: Runner = run, timeout: float = TIMEOUT) -> None:
        self.command = command
        self.runner = runner
        self.timeout = timeout

    @classmethod
    def detect(
        cls,
        platform: str = sys.platform,
        which: Callable[[str], str | None] = shutil.which,
        runner: Runner = run,
    ) -> Desktop:
        if platform == "darwin":
            return cls(OSASCRIPT, runner)
        if which("notify-send") is not None:
            return cls(NOTIFY_SEND, runner)
        return cls(None, runner)

    def send(self, title: str, body: str) -> None:
        if self.command is None:
            raise NotifyFailed(NO_NOTIFIER)
        self.runner([*self.command, title, body], self.timeout)
