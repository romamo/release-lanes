"""Spec 005, What Claude Code provides: the design doc records the headless probe"""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


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
