"""Spec 012, Skills, Relayed decisions, and Docs: the skills route every write through
`shipmill gh`, a relayed answer names who decided, and the setup docs document it"""

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
WRITING_SKILLS = (
    "github-issue-triage",
    "github-issue-resolve",
    "github-pr-triage",
    "github-ship-watch",
    "product-intake",
    "shipmill-setup",
)
RELAYED = "Decision by @<login>, relayed by <agent>"


def folded(*parts: str) -> str:
    """A doc with its line wrapping folded, so a statement reads as one line"""
    return " ".join(ROOT.joinpath(*parts).read_text(encoding="utf-8").split())


def test_s012_5_the_six_skills_are_every_skill_that_writes() -> None:
    assert {path.parent.name for path in (ROOT / "skills").glob("*/SKILL.md")} == set(WRITING_SKILLS)


@pytest.mark.parametrize("skill", WRITING_SKILLS)
def test_s012_5_each_skill_writes_through_shipmill_gh_in_every_session(skill: str) -> None:
    text = folded("skills", skill, "SKILL.md")
    assert text.count("**Write as the App when the repo has one") == 1, "said once, in one place"
    rule = text.split("**Write as the App when the repo has one", 1)[1][:900]  # the rule itself, not the rest
    for phrase in (
        "`[agents] app_id` set" if skill == "shipmill-setup" else "sets `[agents] app_id`",
        "every `gh` call that writes",
        "runs as `shipmill gh <the same arguments>`",
        "in every session, gated or started by hand",
        "Reads may keep plain `gh`",
        "A `shipmill gh` that exits 2 is a stop",
        "never retry the write with plain `gh`",
    ):
        assert phrase in rule, (skill, phrase)


@pytest.mark.parametrize("skill", [s for s in WRITING_SKILLS if s != "shipmill-setup"])
def test_s012_5_a_headless_session_gets_a_form_its_allowlist_allows(skill: str) -> None:
    # the headless gate allows Bash(uvx *) and no bare shipmill, so the rule names the uvx form
    text = folded("skills", skill, "SKILL.md")
    assert "`uvx --from git+https://github.com/shipmill/shipmill@v0 shipmill gh ...`" in text


@pytest.mark.parametrize("name", ["needs-decision.md", "comments.md"])
def test_s012_6_a_relayed_answer_names_who_decided(name: str) -> None:
    text = folded("skills", "github-issue-triage", "references", name)
    assert f"`{RELAYED}`" in text or f"```markdown {RELAYED} " in text
    assert "(`Claude Code`)" in text
    assert "`shipmill gh` with `[agents] app_id` set" in text


def test_s012_6_the_relayed_line_is_the_answer_comments_first_line() -> None:
    ref = folded("skills", "github-issue-triage", "references", "needs-decision.md")
    answered = ref.split("## Answered in the session", 1)[1].split(" ## ", 1)[0]
    assert f"```markdown {RELAYED} Answered in the session:" in answered
    comments = folded("skills", "github-issue-triage", "references", "comments.md")
    assert "```markdown Decision by @{login}, relayed by {agent} {The option chosen" in comments


def test_s012_6_no_skill_keeps_its_own_copy_of_the_references() -> None:
    for name in ("needs-decision.md", "comments.md"):
        copies = sorted(p.relative_to(ROOT).as_posix() for p in (ROOT / "skills").rglob(name))
        assert copies == [f"skills/github-issue-triage/references/{name}"]


@pytest.mark.parametrize("doc", [("docs", "install.md"), ("skills", "shipmill-setup", "SKILL.md")])
def test_s012_9_the_setup_docs_document_shipmill_gh(doc: tuple[str, ...]) -> None:
    text = folded(*doc)
    assert "`shipmill gh" in text or "`$CR gh" in text
    assert "runs `gh` as the App" in text or "shipmill gh issue comment" in text
    assert "`$XDG_CACHE_HOME/shipmill/<owner>/<repo>/app-token.json`" in text
    assert "(`~/.cache` when `XDG_CACHE_HOME` is unset)" in text
    assert "The App's identity covers the sessions" in text
    assert "you start yourself too" in text or "a person starts too" in text
    assert "exits 2" in text and "never runs" in text
