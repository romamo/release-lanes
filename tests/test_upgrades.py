"""Spec 014: the config upgrade catalogue, `shipmill upgrade [--apply <id>]`, and
`[autonomy] upgrade`"""

import json
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

from shipmill.autonomy import Autonomy, AutonomyPolicy, Hold
from shipmill.cli import main
from shipmill.config import CONFIG_PATH, Table
from shipmill.errors import ReleaseError
from shipmill.upgrades import CATALOGUE, FRAGMENTS_README, UpgradeId, find

from .conftest import POLICY, Repo

AGENTS = '\n[agents]\nprompt = "triage {repo}"\n'
HELD = Hold(("#7 Investigating",))


def listed(root: Path, capsys: pytest.CaptureFixture[str]) -> list[str]:
    assert main(["--repo", str(root), "upgrade", "--json"]) == 0
    return [u["id"] for u in json.loads(capsys.readouterr().out)]


def write_config(root: Path, text: str) -> Path:
    path = root / CONFIG_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))
    return path


def shipmill(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """The installed CLI, as a person runs it: its exit code and stderr"""
    command = [sys.executable, "-m", "shipmill", "--repo", str(root), *args]
    return subprocess.run(command, capture_output=True, text=True, check=False)


# -- the catalogue ---------------------------------------------------------------------------


def test_catalogue_ids_are_unique_and_found() -> None:
    ids = [u.id for u in CATALOGUE]
    assert [str(i) for i in ids] == ["changelog-fragments", "plugin-update"]
    assert len(set(ids)) == len(ids)
    assert find(UpgradeId("plugin-update")).line == "plugin_update = true"
    assert find(UpgradeId("changelog-fragments")).line == 'fragments = "changelog.d"'


@pytest.mark.parametrize("text", ["", "Plugin-Update", "plugin_update", "-x", "a--b"])
def test_upgrade_id_is_strict(text: str) -> None:
    with pytest.raises(ReleaseError, match="an upgrade id is lowercase words joined by '-'"):
        UpgradeId(text)


# -- listing (S-014-1) -----------------------------------------------------------------------


def test_s014_1_lists_fragments_until_the_config_sets_it(repo: Repo, capsys: pytest.CaptureFixture[str]) -> None:
    assert listed(repo.root, capsys) == ["changelog-fragments"]
    for value in ('"changelog.d"', '"docs/news"', '""', "false"):
        write_config(repo.root, POLICY.replace("[changelog]\n", f"[changelog]\nfragments = {value}\n"))
        assert listed(repo.root, capsys) == [], value


def test_s014_1_lists_plugin_update_only_with_agents_and_no_key(repo: Repo, capsys: pytest.CaptureFixture[str]) -> None:
    fragments = POLICY.replace("[changelog]\n", '[changelog]\nfragments = "changelog.d"\n')
    assert listed(repo.root, capsys) == ["changelog-fragments"]  # no [agents]: plugin-update doesn't apply
    write_config(repo.root, fragments + AGENTS)
    assert listed(repo.root, capsys) == ["plugin-update"]
    for value in ("true", "false"):  # a key written by hand, either way, is a decision
        write_config(repo.root, fragments + AGENTS + f"plugin_update = {value}\n")
        assert listed(repo.root, capsys) == []


def test_s014_1_lists_for_people_with_the_apply_command(repo: Repo, capsys: pytest.CaptureFixture[str]) -> None:
    write_config(repo.root, POLICY + AGENTS)
    assert main(["--repo", str(repo.root), "upgrade"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("changelog-fragments (shipmill ")
    assert '  adds: [changelog] fragments = "changelog.d"\n' in out
    assert "upgrade --apply changelog-fragments`" in out
    assert "plugin-update (shipmill " in out and "upgrade --apply plugin-update`" in out
    write_config(repo.root, POLICY.replace("[changelog]\n", '[changelog]\nfragments = "x"\n'))
    assert main(["--repo", str(repo.root), "upgrade"]) == 0
    assert capsys.readouterr().out == "no pending upgrades\n"


def test_listing_json_carries_the_proposal_text(repo: Repo, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--repo", str(repo.root), "upgrade", "--json"]) == 0
    (record,) = json.loads(capsys.readouterr().out)
    assert record["id"] == "changelog-fragments"
    assert record["table"] == "changelog" and record["line"] == 'fragments = "changelog.d"'
    assert record["files"] == ["changelog.d/README.md"]
    assert record["changes"] and record["why"] and record["off"] and record["version"]


def test_listing_refuses_a_table_that_is_not_one(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    write_config(tmp_path, 'agents = "yes"\n')
    with pytest.raises(ReleaseError, match=r"agents must be a table"):
        listed(tmp_path, capsys)


# -- applying (S-014-2) ----------------------------------------------------------------------

COMMENTED = """\
# the release policy  (two spaces, kept)
name = "demo"

[changelog]   # where the entries live
path = "CHANGELOG.md"  # the file
style = "keep-a-changelog"
# a comment about [bump], left above it

[bump]
from = "headings"
"""


def test_s014_2_apply_adds_the_line_and_the_readme_and_commits_nothing(repo: Repo) -> None:
    path = write_config(
        repo.root,
        POLICY.replace(
            '[changelog]\nstyle = "keep-a-changelog"\n',
            '[changelog]   # where the entries live\nstyle = "keep-a-changelog"  # the style\n# about [bump]\n',
        ),
    )
    repo.git.run("commit", "-q", "-am", "commented config")
    head = repo.git.sha()
    before = path.read_bytes()
    assert main(["--repo", str(repo.root), "upgrade", "--apply", "changelog-fragments"]) == 0
    after = path.read_bytes()
    marker = b'style = "keep-a-changelog"  # the style\n'
    assert after == before.replace(marker, marker + b'fragments = "changelog.d"\n')
    assert tomllib.loads(after.decode())["changelog"]["fragments"] == "changelog.d"
    assert (repo.root / "changelog.d" / "README.md").read_text(encoding="utf-8") == FRAGMENTS_README
    assert repo.git.sha() == head  # no commit
    status = repo.git.run("status", "--porcelain").splitlines()
    assert sorted(status) == sorted([f" M {CONFIG_PATH.as_posix()}", "?? changelog.d/"])


def test_s014_2_apply_keeps_every_other_byte(tmp_path: Path) -> None:
    path = write_config(tmp_path, COMMENTED)
    assert main(["--repo", str(tmp_path), "upgrade", "--apply", "changelog-fragments"]) == 0
    expected = COMMENTED.replace(
        'style = "keep-a-changelog"\n', 'style = "keep-a-changelog"\nfragments = "changelog.d"\n'
    )
    assert path.read_text(encoding="utf-8") == expected


def test_s014_2_apply_keeps_crlf_and_a_missing_last_newline(tmp_path: Path) -> None:
    text = 'name = "demo"\r\n\r\n[agents]\r\nprompt = "triage {repo}"'
    path = write_config(tmp_path, text)
    assert main(["--repo", str(tmp_path), "upgrade", "--apply", "plugin-update"]) == 0
    assert path.read_bytes() == (text + "\r\nplugin_update = true\r\n").encode()


@pytest.mark.parametrize("separator", [" ", " ", "\x85"])
def test_s014_2_apply_splits_lines_only_on_toml_newlines(tmp_path: Path, separator: str) -> None:
    # str.splitlines also breaks on these; TOML doesn't, so a comment holding one is one line
    text = f'[changelog]\npath = "CHANGELOG.md"  # a{separator}# b\n'
    path = write_config(tmp_path, text)
    assert main(["--repo", str(tmp_path), "upgrade", "--apply", "changelog-fragments"]) == 0
    assert path.read_text(encoding="utf-8") == text + 'fragments = "changelog.d"\n'


def test_apply_reads_past_a_multiline_string_that_looks_like_a_table(tmp_path: Path) -> None:
    text = '[agents]\nprompt = """\ntriage\n[changelog]\n"""\n\n[changelog]\npath = "CHANGELOG.md"\n'
    path = write_config(tmp_path, text)
    assert main(["--repo", str(tmp_path), "upgrade", "--apply", "plugin-update"]) == 0
    assert main(["--repo", str(tmp_path), "upgrade", "--apply", "changelog-fragments"]) == 0
    assert path.read_text(encoding="utf-8") == (
        '[agents]\nprompt = """\ntriage\n[changelog]\n"""\nplugin_update = true\n\n'
        '[changelog]\npath = "CHANGELOG.md"\nfragments = "changelog.d"\n'
    )


def test_apply_under_an_empty_table_and_keeps_an_existing_readme(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = write_config(tmp_path, "[changelog]\n\n[bump]\n")
    readme = tmp_path / "changelog.d" / "README.md"
    readme.parent.mkdir()
    readme.write_text("ours\n", encoding="utf-8")
    assert main(["--repo", str(tmp_path), "upgrade", "--apply", "changelog-fragments", "--json"]) == 0
    record = json.loads(capsys.readouterr().out)
    assert record["created"] == [] and record["kept"] == ["changelog.d/README.md"]
    assert path.read_text(encoding="utf-8") == '[changelog]\nfragments = "changelog.d"\n\n[bump]\n'
    assert readme.read_text(encoding="utf-8") == "ours\n"


@pytest.mark.parametrize(
    "text",
    ['changelog = { path = "CHANGELOG.md" }\n', 'changelog.path = "CHANGELOG.md"\n', '["changelog"]\npath = "x"\n'],
)
def test_apply_refuses_a_table_without_a_header_line_it_can_edit(tmp_path: Path, text: str) -> None:
    path = write_config(tmp_path, text)
    with pytest.raises(ReleaseError, match=r"changelog-fragments.*\[changelog\]"):
        main(["--repo", str(tmp_path), "upgrade", "--apply", "changelog-fragments"])
    assert path.read_text(encoding="utf-8") == text
    assert not (tmp_path / "changelog.d").exists()


# -- refusing (S-014-3) ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("config", "upgrade", "error"),
    [
        (POLICY, "nightly-builds", "unknown upgrade nightly-builds; known: changelog-fragments, plugin-update"),
        (POLICY, "plugin-update", "upgrade plugin-update doesn't apply: .github/shipmill.toml has no [agents]"),
        (
            POLICY.replace("[changelog]\n", '[changelog]\nfragments = "changelog.d"\n'),
            "changelog-fragments",
            "upgrade changelog-fragments is already decided: [changelog] fragments is set",
        ),
        (
            POLICY + AGENTS + "plugin_update = false\n",
            "plugin-update",
            "upgrade plugin-update is already decided: [agents] plugin_update is set",
        ),
        ('name = "demo"\n', "changelog-fragments", "upgrade changelog-fragments: .github/shipmill.toml has no"),
    ],
)
def test_s014_3_apply_exits_2_naming_the_id(tmp_path: Path, config: str, upgrade: str, error: str) -> None:
    path = write_config(tmp_path, config)
    done = shipmill(tmp_path, "upgrade", "--apply", upgrade)
    assert done.returncode == 2
    assert error in done.stderr
    assert path.read_text(encoding="utf-8") == config
    assert not (tmp_path / "changelog.d").exists()


# -- [autonomy] upgrade (S-014-4) ------------------------------------------------------------


def upgrade_level(text: str) -> AutonomyPolicy:
    return AutonomyPolicy.parse(Table(tomllib.loads(text), "shipmill.toml [autonomy]"))


def test_s014_4_upgrade_autonomy_defaults_to_propose_and_takes_each_level() -> None:
    assert upgrade_level("").upgrade is Autonomy.PROPOSE
    for level in Autonomy:
        assert upgrade_level(f'upgrade = "{level}"\n').upgrade is level


@pytest.mark.parametrize("value", ['"auto"', '"Act"', "true", "1"])
def test_s014_4_upgrade_autonomy_refuses_other_values_naming_the_allowed(value: str) -> None:
    allowed = r"upgrade must be one of \['observe', 'propose', 'act'\], got "
    with pytest.raises(ReleaseError, match=allowed):
        upgrade_level(f"upgrade = {value}\n")


def test_s014_4_a_hold_turns_upgrade_act_into_propose() -> None:
    assert upgrade_level('upgrade = "act"\n').upgrades(Hold()) is Autonomy.ACT
    assert upgrade_level('upgrade = "act"\n').upgrades(HELD) is Autonomy.PROPOSE
    assert upgrade_level('upgrade = "propose"\n').upgrades(HELD) is Autonomy.PROPOSE
    assert upgrade_level('upgrade = "observe"\n').upgrades(HELD) is Autonomy.OBSERVE
    # not a stage: doctor's stage list is unchanged
    assert [str(s) for s in upgrade_level('upgrade = "act"\n').stages()] == ["release", "rollback"]
