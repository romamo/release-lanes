"""Release lanes driven by a hand-written CHANGELOG"""

import shutil
import sys
from collections.abc import Callable
from pathlib import Path

# The CLI from git with nothing installed: what a hint names when no `shipmill` on PATH is the
# running one (#223), and what a file init writes names, since anyone may read it
UVX = "uvx --from git+https://github.com/shipmill/shipmill@v0 shipmill"


def cli_command(which: Callable[[str], str | None] = shutil.which, prefix: str = sys.prefix) -> str:
    """How a hint tells a person to run the CLI (#236).

    A bare `shipmill` when the `shipmill` that PATH finds resolves into the running
    interpreter's environment (a `uv tool install shipmill`, or a virtualenv on PATH); the
    uvx form otherwise, as for a uvx run, whose environment is a cache folder off PATH.
    """
    found = which("shipmill")
    if found is not None and Path(found).resolve().is_relative_to(Path(prefix).resolve()):
        return "shipmill"
    return UVX
