"""Release lanes driven by a hand-written CHANGELOG"""

import os
import shutil
import sys
from pathlib import Path

# The CLI from git with nothing installed: what a hint names when no `shipmill` on PATH is the
# running one (#223), and what a file init writes names, since anyone may read it
UVX = "uvx --from git+https://github.com/shipmill/shipmill@v0 shipmill"


def cli_command(path: str | None = None, prefix: str = sys.prefix) -> str:
    """How a hint tells a person to run the CLI (#236).

    A bare `shipmill` when a folder on PATH (`path`, by default $PATH) outside the running
    interpreter's environment holds a `shipmill` that resolves into it: a `uv tool install
    shipmill` links one into uv's tool folder. The uvx form otherwise. A folder inside the
    environment is skipped, since uvx and `uv run` put the environment's own bin there for the
    run only: the person reading the hint has no such folder on their PATH.
    """
    own = Path(prefix).resolve()
    searched = path if path is not None else os.environ.get("PATH", "")
    outside = [d for d in searched.split(os.pathsep) if d and not Path(d).resolve().is_relative_to(own)]
    found = shutil.which("shipmill", path=os.pathsep.join(outside))
    if found is not None and Path(found).resolve().is_relative_to(own):
        return "shipmill"
    return UVX
