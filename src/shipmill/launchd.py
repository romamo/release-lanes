"""A launchd job that runs `shipmill gate` for one repo on a Mac: the host side of the
gate. The repo side, what the session does, is the config's [agents] section.

launchd starts jobs with a minimal PATH, so the job carries one built from where claude,
gh, git, and uvx live now, skipping folders under the temp directory: a terminal app's
shims there (cmux puts claude in one) vanish when the app restarts."""

import os
import plistlib
import re
import subprocess
import sys
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from shipmill.errors import ReleaseError

TOOLS = ("claude", "gh", "git", "uvx")
BASE_PATH = ("/usr/bin", "/bin", "/usr/sbin", "/sbin")
DEFAULT_TOOL = "git+https://github.com/shipmill/shipmill@v0"
_TEMP_ROOTS = ("/tmp/", "/private/tmp/", "/var/folders/", "/private/var/folders/")


@dataclass(frozen=True, slots=True)
class Job:
    label: str
    plist: Path
    log: Path
    document: bytes


def label(repo: str) -> str:
    owner, _, name = repo.partition("/")
    if not owner or not name or "/" in name:
        raise ReleaseError(f"repo must be owner/name, got {repo!r}")
    return "dev.shipmill.gate." + re.sub(r"[^a-z0-9.-]", "-", f"{owner}.{name}".lower())


def is_temporary(folder: str) -> bool:
    resolved = os.path.realpath(folder) + "/"
    temp = os.path.realpath(tempfile.gettempdir()) + "/"
    return resolved.startswith((temp, *_TEMP_ROOTS)) or folder.startswith(_TEMP_ROOTS)


def find_tool(name: str, path: str, skip: Callable[[str], bool] = is_temporary) -> Path:
    """The first executable `name` on `path` outside the folders `skip` rejects"""
    for folder in path.split(os.pathsep):
        candidate = Path(folder) / name
        if folder and not skip(folder) and candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate
    raise ReleaseError(f"{name} is not on PATH outside temporary folders; install it, then run this again")


def job_path(tools: Sequence[Path]) -> str:
    folders: list[str] = []
    for folder in [str(t.parent) for t in tools] + list(BASE_PATH):
        if folder not in folders:
            folders.append(folder)
    return os.pathsep.join(folders)


def build(
    repo: str,
    checkout: Path,
    interval_minutes: int,
    tool: str,
    claude_args: Sequence[str],
    home: Path,
    path: str,
    skip: Callable[[str], bool] = is_temporary,
) -> Job:
    if not 5 <= interval_minutes <= 24 * 60:
        raise ReleaseError(f"the interval must be 5..1440 minutes, got {interval_minutes}")
    found = [find_tool(t, path, skip) for t in TOOLS]
    name = label(repo)
    log = home / "Library" / "Logs" / "shipmill" / f"{name}.log"
    args = [str(found[TOOLS.index("uvx")]), "--from", tool, "shipmill", "--repo", str(checkout), "gate", repo]
    args += ["--refresh"]
    for extra in claude_args:
        args += ["--claude-arg", extra]
    document = plistlib.dumps(
        {
            "Label": name,
            "ProgramArguments": args,
            "WorkingDirectory": str(checkout),
            "StartInterval": interval_minutes * 60,
            "RunAtLoad": True,
            "EnvironmentVariables": {"PATH": job_path(found)},
            "StandardOutPath": str(log),
            "StandardErrorPath": str(log),
        }
    )
    return Job(name, home / "Library" / "LaunchAgents" / f"{name}.plist", log, document)


def _launchctl(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(["launchctl", *args], capture_output=True, text=True, check=False)
    if check and proc.returncode != 0:
        raise ReleaseError(f"launchctl {' '.join(args)} failed: {(proc.stderr or proc.stdout).strip()}")
    return proc


def _require_mac() -> None:
    if sys.platform != "darwin":
        raise ReleaseError("launchd runs on macOS; on Linux, run `shipmill gate` from a systemd timer or cron")


def install(job: Job) -> None:
    """Write the job and (re)load it, so it runs once now and then every interval"""
    _require_mac()
    domain = f"gui/{os.getuid()}"
    if _launchctl("print", f"{domain}/{job.label}", check=False).returncode == 0:
        _launchctl("bootout", f"{domain}/{job.label}")
    job.plist.parent.mkdir(parents=True, exist_ok=True)
    job.log.parent.mkdir(parents=True, exist_ok=True)
    job.plist.write_bytes(job.document)
    _launchctl("bootstrap", domain, str(job.plist))


def remove(repo: str, home: Path) -> Path | None:
    """Unload and delete the repo's job; the plist path, or None when there was none"""
    _require_mac()
    name = label(repo)
    plist = home / "Library" / "LaunchAgents" / f"{name}.plist"
    domain = f"gui/{os.getuid()}"
    if _launchctl("print", f"{domain}/{name}", check=False).returncode == 0:
        _launchctl("bootout", f"{domain}/{name}")
    if not plist.is_file():
        return None
    plist.unlink()
    return plist
