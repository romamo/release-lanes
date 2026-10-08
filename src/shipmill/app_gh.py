"""`shipmill gh` (spec 012): run the `gh` on PATH as the repo's GitHub App when its config
sets [agents] app_id, so a session a person starts writes as the App too (D-14).

The arguments reach gh exactly as given. Only the repo gh's `-R`/`--repo` names is read
from them, to limit the token to it. The token goes to gh in GH_TOKEN, never to output.
"""

import re
import shutil
import subprocess
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

from shipmill.errors import ReleaseError

GhRunner = Callable[[Sequence[str], Mapping[str, str]], int]  # argv and env in, gh's exit code out

_REPO_FLAGS = ("-R", "--repo")
_REPO = re.compile(r"(?:github\.com/)?([A-Za-z0-9-]+/[A-Za-z0-9._-]+)", re.IGNORECASE)


def run_gh(argv: Sequence[str], env: Mapping[str, str]) -> int:
    """Run gh with the terminal's stdin, stdout, and stderr; a signal's death is 128 + its number,
    as a shell reports it"""
    code = subprocess.run(list(argv), env=dict(env), check=False).returncode
    return code if code >= 0 else 128 - code


def find_gh(environ: Mapping[str, str]) -> Path:
    found = shutil.which("gh", path=environ.get("PATH", ""))
    if found is None:
        raise ReleaseError("gh not found on PATH; install GitHub's CLI, https://cli.github.com")
    return Path(found)


def named_repo(args: Sequence[str]) -> str | None:
    """The owner/name gh's `-R`/`--repo` names in args (`-R x`, `--repo x`, `--repo=x`, `-Rx`,
    `-R=x`), the last when given twice, as gh takes it; None when no flag names one. Nothing
    after `--` is a flag"""
    found: str | None = None
    index = 0
    while index < len(args):
        arg = args[index]
        if arg == "--":
            break
        if arg in _REPO_FLAGS:
            if index + 1 == len(args):
                raise ReleaseError(f"gh's {arg} needs a repo, such as owner/name")
            found = args[index + 1]
            index += 2
            continue
        if arg.startswith("--repo="):
            found = arg.removeprefix("--repo=")
        elif arg.startswith("-R") and len(arg) > 2:
            found = arg[2:].removeprefix("=")
        index += 1
    if found is None:
        return None
    matched = _REPO.fullmatch(found)
    if matched is None or any(part in {".", ".."} for part in matched.group(1).split("/")):
        raise ReleaseError(f"gh's -R/--repo is {found!r}; the App's token needs owner/name on github.com")
    return matched.group(1)


def as_host(environ: Mapping[str, str]) -> dict[str, str]:
    """gh's env with no App: the caller's, less a GH_TOKEN set but empty, so gh uses the
    host's login as the repo chose rather than by a silent fallback"""
    env = dict(environ)
    if env.get("GH_TOKEN") == "":
        del env["GH_TOKEN"]
    return env


def as_app(environ: Mapping[str, str], token: str) -> dict[str, str]:
    """gh's env as the App: the caller's, with GH_TOKEN replaced by the App's token"""
    return {**environ, "GH_TOKEN": token}
