"""Spec 004, Docs: the setup docs, the design doc, and github-ship-watch document the App"""

import importlib.util
import re
import sys
import tomllib
from pathlib import Path

from shipmill.app import CACHE, CREDENTIAL_HELPER, GH_HELPER, HELPERS, PERMISSIONS

ROOT = Path(__file__).resolve().parents[1]


def folded(*parts: str) -> str:
    """A doc with its line wrapping folded, so a statement reads as one line"""
    return " ".join(ROOT.joinpath(*parts).read_text(encoding="utf-8").split())


def table(text: str) -> set[tuple[str, str]]:
    """The (permission, access) rows of the permission table in a folded doc"""
    return set(re.findall(r"\| ([A-Z][a-z]+(?: [a-z]+)?) \| (read|write) \|", text))


def agents_block(text: str) -> dict[str, object]:
    block = text.split("[agents]\n", 1)[1].split("```", 1)[0]
    agents: dict[str, object] = tomllib.loads("[agents]\n" + "\n".join(line.strip() for line in block.splitlines()))[
        "agents"
    ]
    return agents


def test_s004_14_the_docs_document_the_app() -> None:
    setup = folded("skills", "shipmill-setup", "SKILL.md")
    gate = setup.split("## The gate", 1)[1].split(" ## ", 1)[0]
    install = folded("docs", "install.md")
    modes = folded("docs", "design", "agent-modes.md")
    watch = folded("skills", "github-ship-watch", "SKILL.md")
    expected = {(p.label, str(p.access)) for p in PERMISSIONS}

    for name, doc in (("shipmill-setup", gate), ("install.md", install)):
        # creating the App: no webhook, the permission table, installed only where it works
        assert "Webhook's Active box" in doc, name
        assert table(doc) == expected, name
        assert "Only select repositories" in doc, name
        # the key path and its mode
        assert "~/.config/shipmill/app-<app_id>.pem" in doc, name
        assert "chmod 600" in doc, name
        assert "readable by group or others" in doc, name
        # app_id, the dry run that proves the setup, launchd, and no App
        assert "app_id = 123456" in doc, name
        assert "would launch as <slug>[bot]" in doc, name
        assert "launchd" in doc and "--app-key <path>" in doc, name
        assert "launch as before, as" in doc, name
        # the token cache and the helpers
        assert f"$(git rev-parse --git-common-dir)/shipmill/{HELPERS}/" in doc, name
        assert f"`{GH_HELPER}` wrapper" in doc and CREDENTIAL_HELPER in doc, name
        assert f"shipmill/{CACHE}` (mode `0600`)" in doc, name
        assert "shipmill app-token" in doc, name

    raw_setup = (ROOT / "skills" / "shipmill-setup" / "SKILL.md").read_text(encoding="utf-8")
    raw_install = (ROOT / "docs" / "install.md").read_text(encoding="utf-8")
    raw_modes = (ROOT / "docs" / "design" / "agent-modes.md").read_text(encoding="utf-8")
    for raw in (raw_install, raw_modes):
        assert "app_id" not in agents_block(raw)  # shown, commented out, so a copy sets no App
    # #204, D-19: the skill sets up every gate with an App, so its example sets app_id, a
    # placeholder step 3 replaces
    assert agents_block(raw_setup.split("\n## The gate\n", 1)[1])["app_id"] == 123456
    assert "never merge `123456` itself" in gate

    # the design doc: the state files and what Claude Code provides
    state = modes.split("### State", 1)[1].split(" ## ", 1)[0]
    assert f"`{CACHE}`, with `app_id` set" in state and "mode `0600`" in state
    assert f"`{HELPERS}/`, with `app_id` set" in state and "mode `0700`" in state and "hold no token" in state
    provides = modes.split("### What Claude Code provides", 1)[1].split(" ### ", 1)[0]
    assert "`claude --bg --settings '<json>'` applies the JSON's `env` block" in provides
    assert "~/.config/shipmill/app-<app_id>.pem" in modes and "`app_id`" in modes

    # the metrics count the App's bot as a bot
    assert "the GitHub App's `<slug>[bot]`, which counts as a bot without `--bot`" in watch


def test_s004_14_the_metrics_count_the_apps_bot_as_a_bot() -> None:
    script = ROOT / "skills" / "github-ship-watch" / "scripts" / "metrics.py"
    spec = importlib.util.spec_from_file_location("metrics_app_docs", script)
    assert spec is not None and spec.loader is not None
    metrics = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = metrics
    spec.loader.exec_module(metrics)
    none: frozenset[str] = frozenset()
    assert not metrics.person({"__typename": "Bot", "login": "shipmill-agent[bot]"}, none)
    assert metrics.person({"__typename": "User", "login": "romamo"}, none)
