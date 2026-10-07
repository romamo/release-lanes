"""#215: the docs and skills never tell a reader to run a bare `shipmill <command>`, which no install puts on PATH"""

import argparse
import re
from pathlib import Path

from shipmill.cli import _parser

ROOT = Path(__file__).resolve().parents[1]
LONG = "uvx --from git+https://github.com/shipmill/shipmill@v0 shipmill"


def subcommands() -> set[str]:
    actions = [a for a in _parser()._actions if isinstance(a, argparse._SubParsersAction)]
    assert len(actions) == 1
    return set(actions[0].choices)


def scanned() -> list[Path]:
    """What a reader copies commands from: the README, the top-level docs, and the skills.

    docs/decisions.md is a log that names subcommands in prose; docs/specs, docs/design, and
    docs/postmortems are design records, some of them about commands that don't exist yet.
    """
    docs = [p for p in sorted((ROOT / "docs").glob("*.md")) if p.name != "decisions.md"]
    return [ROOT / "README.md", *docs, *sorted((ROOT / "skills").rglob("*.md"))]


def fenced_lines(text: str) -> list[str]:
    lines: list[str] = []
    inside = False
    for line in text.splitlines():
        if line.lstrip().startswith("```"):
            inside = not inside
        elif inside:
            lines.append(line)
    return lines


def bare_commands(text: str, subs: set[str]) -> list[str]:
    """Bare `shipmill <subcommand>` shown as a command to run.

    In a fenced block: `shipmill` (optionally with `--repo <path>`) at a command's start, that
    is at the line's start, after a `$ ` prompt, or after `;`, `&&`, `|`, or `$(`.
    In an inline code span: one that follows an imperative "run", or one that carries a flag,
    unless it follows "runs" (prose about what another part runs, such as the gate).
    """
    names = "|".join(sorted(map(re.escape, subs), key=len, reverse=True))
    start = re.compile(rf"(?:^|[;&|]|\$\()\s*(?:\$\s+)?shipmill(?:\s+--repo\s+\S+)?\s+(?:{names})\b")
    found = [line.strip() for line in fenced_lines(text) if start.search(line)]
    folded = " ".join(text.split())
    for m in re.finditer(rf"`(shipmill (?:{names})\b[^`]*)`", folded):
        before = folded[: m.start()]
        if re.search(r"\bruns\s*\(?$", before):
            continue
        if re.search(r"\b[Rr]un\s*\(?$", before) or " --" in m.group(1):
            found.append(m.group(1))
    return found


def test_the_docs_and_skills_run_shipmill_through_uvx() -> None:
    subs = subcommands()
    offenders = {
        str(path.relative_to(ROOT)): hits
        for path in scanned()
        if (hits := bare_commands(path.read_text(encoding="utf-8"), subs))
    }
    assert offenders == {}


def test_the_scan_flags_a_bare_command_and_spares_prose() -> None:
    subs = subcommands()
    flagged = (
        "```bash\nshipmill status\n```",
        "```bash\nshipmill --repo tmp/gate gate acme/web\n```",
        "```\ncd x && shipmill plan --dry-run\n```",
        "```\n$ shipmill doctor\n```",
        "Then run `shipmill init --operate`.",
        "The summary shows the same; `shipmill operate --dry-run` changes nothing.",
    )
    for text in flagged:
        assert bare_commands(text, subs), text
    spared = (
        f"```bash\n{LONG} status\n$CR app-create --dry-run\n```",
        f"Then run `{LONG} init --operate`.",
        "`shipmill gate` runs `shipmill worktrees --prune` on each tick.",
        "From then on, `shipmill status` answers whether anything is stuck.",
        "```toml\n# shipmill never tags\n```",
    )
    for text in spared:
        assert not bare_commands(text, subs), text


def test_install_md_defines_cr_and_the_alias_before_using_them() -> None:
    install = (ROOT / "docs" / "install.md").read_text(encoding="utf-8")
    defined = install.index(f'CR="{LONG}"')
    assert defined < install.index("$CR ")
    assert f"alias shipmill='{LONG}'" in install
    assert "uv tool install" in install


def test_ship_watch_description_tells_an_agent_how_to_run_the_cli() -> None:
    skill = (ROOT / "skills" / "github-ship-watch" / "SKILL.md").read_text(encoding="utf-8")
    front = skill.split("---\n", 2)[1]
    (line,) = [x for x in front.splitlines() if x.startswith("description: ")]
    description = line.removeprefix("description: ")
    assert "isn't on PATH" in description and f"`{LONG} <command>`" in description
    assert ": " not in description, "a colon and a space would break the plain YAML scalar"
    assert len(description) <= 1024, len(description)
