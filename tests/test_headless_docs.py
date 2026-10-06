"""Spec 005, What Claude Code provides and Docs: the design doc records the headless probe,
and the setup docs document headless mode"""

import re
from pathlib import Path

from shipmill.gate import HEADLESS_REFUSED, HEADLESS_TOOLS

ROOT = Path(__file__).resolve().parents[1]
WIDEN = '--claude-arg=--allowedTools --claude-arg "Bash(npm *)"'
NO_APP = "no app_id: needs-decision comments post as <login>, so GitHub won't notify you"


def folded(*parts: str) -> str:
    """A doc with its line wrapping folded, so a statement reads as one line"""
    return " ".join(ROOT.joinpath(*parts).read_text(encoding="utf-8").split())


def test_s005_4_the_design_doc_records_the_print_probe() -> None:
    doc = folded("docs", "design", "agent-modes.md")
    provides = doc.split("### What Claude Code provides", 1)[1].split("### ", 1)[0]
    assert "Verified on Claude Code 2.1.291 (#160)" in provides
    assert "claude -p --permission-prompts none" in provides and "--disallowedTools AskUserQuestion" in provides
    for observed in (
        "outlived the job that started it",
        "was denied with no prompt",
        "AskUserQuestion was absent",
        "exited on its own",
    ):
        assert observed in provides


def test_s005_18_the_docs_document_headless_mode() -> None:
    modes = folded("docs", "design", "agent-modes.md")
    setup = folded("skills", "shipmill-setup", "SKILL.md").split("## The gate", 1)[1].split(" ## ", 1)[0]
    install = folded("docs", "install.md")
    # mode 2's row is built
    row = modes.split("| 2. Headless |", 1)[1].split(" | 3. ", 1)[0]
    assert 'Built: `mode = "headless"` (spec 005)' in row
    assert "mode 2 built" in modes.split("## Modes", 1)[0]

    for name, doc in (("agent-modes.md", modes), ("shipmill-setup", setup), ("install.md", install)):
        # mode
        assert '# mode = "headless"' in doc and "claude -p" in doc, name
        # HEADLESS_TOOLS, where it lives, and how to widen it
        assert "HEADLESS_TOOLS" in doc and "src/shipmill/gate.py" in doc, name
        assert all(tool in doc for tool in re.findall(r"\w+(?:\([^)]*\))?", HEADLESS_TOOLS)), name
        assert WIDEN in doc, name
        assert "launchd" in doc, name
        # the trust filter
        assert "D-16" in doc and "UNTRUSTED" in doc, name
        assert "OWNER, MEMBER, or COLLABORATOR" in doc or "owner, member, or collaborator" in doc, name
        # the needs-decision label and protocol
        assert "`needs-decision`" in doc and "<!-- shipmill:needs-decision -->" in doc, name
        assert "github-issue-triage/references/needs-decision.md" in doc, name
        # notifications with and without app_id
        assert "<slug>[bot]" in doc and "remind_hours" in doc and "needs-decision.json" in doc, name
        assert NO_APP in doc, name
        assert "`mode`" in doc and "`decisions`" in doc, name

    for name, doc in (("shipmill-setup", setup), ("install.md", install)):
        assert all(flag in doc for flag in HEADLESS_REFUSED), name
        assert "AGENTS_NO_APP" in doc, name


def test_s005_18_the_needs_decision_reference_widens_with_the_working_flag() -> None:
    reference = folded("skills", "github-issue-triage", "references", "needs-decision.md")
    assert "--claude-arg=--allowedTools" in reference
    assert "--claude-arg --allowedTools" not in reference
