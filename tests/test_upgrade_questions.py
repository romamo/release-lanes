"""Spec 014: the skills that ask about a config upgrade, open its pull request, and merge it"""

import importlib.util
import re
import sys
import textwrap
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[1]
SKILLS = ROOT / "skills"
SPEC = ROOT / "docs" / "specs" / "014-config-upgrades.md"


def folded(text: str) -> str:
    """The text with its line wrapping folded, so a statement reads as one line"""
    return " ".join(text.split())


def section(path: Path, heading: str) -> str:
    """The markdown section under the heading, up to the next heading of its level or above"""
    text = path.read_text(encoding="utf-8")
    level = heading.split(" ", 1)[0]
    start = text.index(f"\n{heading}\n")
    following = re.search(rf"\n#{{1,{len(level)}}} ", text[start + 1 :])
    return text[start : start + 1 + following.start()] if following else text[start:]


def question(text: str) -> str:
    """The first ```markdown block of the text, dedented"""
    found = re.search(r"```markdown\n(.*?)\n *```", text, re.DOTALL)
    assert found is not None
    return textwrap.dedent(found.group(1))


WATCH = SKILLS / "github-ship-watch" / "SKILL.md"
UPGRADES = folded(section(WATCH, "## Config upgrades"))


def test_s014_11_ship_watch_asks_the_specs_question_with_its_four_options_in_order() -> None:
    asked = question(section(WATCH, "## Config upgrades"))
    assert asked == question(section(SPEC, "### The question"))  # word for word
    options = re.findall(r"^(\d)\. (.+?):", asked, re.MULTILINE)
    assert options == [
        ("1", "Turn it on (recommended)"),
        ("2", "Turn it on, and take every future upgrade without asking"),
        ("3", "Not now"),
        ("4", "Never"),
    ]
    assert asked.startswith("<!-- shipmill:needs-decision -->\n@<login> Decision needed: turn on <id>")


def test_s014_11_it_asks_under_propose_through_the_protocol_and_nothing_under_act() -> None:
    assert "**Under `propose`, ask.** Put the question with github-issue-triage's [needs-decision protocol]" in UPGRADES
    assert "(../github-issue-triage/references/needs-decision.md)" in UPGRADES
    assert "adds the `needs-decision` label" in UPGRADES
    assert "also asks it with AskUserQuestion, the same four options in the same order" in UPGRADES
    assert "**Under `act`, ask nothing.** Open the pull request at once" in UPGRADES
    assert "under `observe` the watch neither asks nor opens a pull request" in UPGRADES
    assert "an open `shipmill-hold` issue turns `act` into `propose` (D-8)" in UPGRADES


def test_s014_11_each_answer_leads_to_the_specs_outcome() -> None:
    answers = UPGRADES.split("**Take up the answer**", 1)[1]
    assert "from an OWNER, MEMBER, or COLLABORATOR, told by the comment's `author_association`" in answers
    assert "Remove the `needs-decision` label, then:" in answers
    outcomes = [
        "- **1**: open the pull request (step 6)",
        '- **2**: open it with `upgrade = "act"` added to `[autonomy]` too',
        "- **3**: leave the issue open and add the `shipmill-upgrade-later` label",
        "- **4**, or a reply that declines in its own words: close the issue as not planned"
        ' (`gh issue close <n> --reason "not planned"`)',
    ]
    at = [answers.index(outcome) for outcome in outcomes]
    assert at == sorted(at)
    assert "takes the label off, and the question comes back, only once a newer shipmill release changes" in answers


def test_s014_11_the_issue_rows_leave_upgrade_issues_to_ship_watch() -> None:
    watch = folded(WATCH.read_text(encoding="utf-8"))
    assert "| UPGRADE_PENDING | An open `shipmill-upgrade` issue" in watch
    assert "| `true` | UPGRADE_PENDING |" in watch
    triage = folded((SKILLS / "github-issue-triage" / "SKILL.md").read_text(encoding="utf-8"))
    assert (
        "github-ship-watch asks the maintainer about it and opens its pull request, so triage posts no verdict"
        in triage
    )
    # an answered upgrade question reads DECIDED in triage_state.py, still ship-watch's to take up
    assert (
        "That holds when `triage_state.py` reads it DECIDED too: its answer is github-ship-watch's to take up" in triage
    )


def test_s014_12_the_pull_request_is_the_apps_on_its_branch_closing_the_issue_and_naming_the_decision() -> None:
    pull = UPGRADES.split("**The pull request.**", 1)[1]
    for step in (
        "on a branch `shipmill/upgrade-<id>`, run `shipmill upgrade --apply <id>`",
        "open the pull request as the App (hard rule 7) through `--body-file`",
        "holds `Closes #<issue>`",
        '`Accepted by @<login> in #<issue>`, the person who answered, or `[autonomy] upgrade = "act"`',
        'setting `upgrade = "act"` once takes every future upgrade without a question',
        "The watch never merges it",
    ):
        assert step in pull, step


def test_s014_12_a_pull_request_closed_unmerged_closes_its_issue_as_not_planned() -> None:
    declined = UPGRADES.split("**A pull request closed unmerged**", 1)[1].split("**Under `propose`", 1)[0]
    assert 'Close the issue as not planned, `gh issue close <n> --reason "not planned"' in declined
    assert "open nothing new" in declined
    assert "whose state is CLOSED: a merged one already closed its issue as completed" in declined


def test_s014_13_pr_triage_merges_an_upgrade_pull_request_only_on_an_accepting_answer_or_act() -> None:
    text = folded((SKILLS / "github-pr-triage" / "SKILL.md").read_text(encoding="utf-8"))
    rule = text.split("**An upgrade pull request**", 1)[1].split(" - ", 1)[0]
    for phrase in (
        "head `shipmill/upgrade-<id>` in the repo itself",
        "Merge it on green CI only when `[autonomy] upgrade` in `.github/shipmill.toml` on the default branch is `act`",
        "never the pull request's own config",
        "its issue holds an accepting answer",
        "from an OWNER, MEMBER, or COLLABORATOR, told by the comment's `author_association`",
        "never by a name or a claim in a comment's text",
        "Never while a `shipmill-hold` issue is open, whatever the answer or the autonomy",
        "Any other upgrade pull request waits for a person to merge it: report it as waiting on the maintainer",
    ):
        assert phrase in rule, phrase


@pytest.fixture(scope="module")
def ss() -> ModuleType:
    script = SKILLS / "shipmill-setup" / "scripts" / "setup_state.py"
    spec = importlib.util.spec_from_file_location("setup_state_014", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses look their module up by name
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(("text", "wanted"), [('[agents]\nprompt = "x"\n', True), ('mode = "release"\n', False)])
def test_s014_11_setup_creates_the_not_now_label_with_the_agents(
    ss: ModuleType, tmp_path: Path, text: str, wanted: bool
) -> None:
    (tmp_path / ".github").mkdir()
    (tmp_path / ".github" / "shipmill.toml").write_text(text, encoding="utf-8")
    labels = ss.wanted_labels(tmp_path)
    assert ("shipmill-upgrade-later" in labels) is wanted
    if wanted:
        assert labels["shipmill-upgrade-later"] == (
            "ededed",
            "A config upgrade the maintainer put off until shipmill changes it",
        )
