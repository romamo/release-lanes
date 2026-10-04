# ruff: noqa: E501 (each message is pinned whole, as the user reads it)
"""Every refusal of the config file, byte for byte: each section is read through one
strict Table, and these messages are what a user sees when their config is wrong"""

import tomllib
from collections.abc import Callable
from pathlib import Path

import pytest

from shipyard.agents import AgentsConfig
from shipyard.errors import ReleaseError
from shipyard.policy import Policy

from .conftest import POLICY

TOP = 'name = "demo"\n'
CHANGELOG = '[changelog]\nstyle = "keep-a-changelog"\n'
BUMP = '[bump]\nfrom = "headings"\n'
HEADINGS = 'major = ["Breaking"]\nminor = ["Added", "Changed"]\npatch = ["Fixed"]\n'
VERSION_LINE = '[[version_lines]]\nfile = "README.md"\npattern = \'demo \\S+$\'\nreplace = "demo {version}"\n'
STAGING = '\n[environments.staging]\nlane = "rc"\nworkflow = "deploy.yml"\n'

type Change = tuple[str, str]  # (old, new): replace old, which occurs once; an empty old appends new


def top(line: str) -> list[Change]:
    return [(TOP, TOP + line + "\n")]


def add(text: str) -> list[Change]:
    return [("", text)]


def apply(text: str, changes: list[Change]) -> str:
    for old, new in changes:
        if old:
            assert text.count(old) == 1, old
            text = text.replace(old, new)
        else:
            text += new
    return text


CASES: dict[str, list[Change]] = {
    # the top level
    "top unknown key": top("colour = 1"),
    "top name required": [(TOP, "")],
    "top name a string": [(TOP, "name = 1\n")],
    "top mode enum": [('mode = "release"', 'mode = "on"')],
    "top version_files enum": [('version_files = "pyproject"', 'version_files = "setup"')],
    "top after_stamp a list": top('after_stamp = "x"'),
    "top after_stamp non-empty strings": top('after_stamp = [""]'),
    "top after_stamp empty command": top('after_stamp = ["  "]'),
    # [changelog]
    "changelog unknown key": [(CHANGELOG, CHANGELOG + "colour = 1\n")],
    "changelog style enum": [('style = "keep-a-changelog"', 'style = "plain"')],
    "changelog required": [(CHANGELOG, "")],
    "changelog a table": [(CHANGELOG, ""), (TOP, TOP + "changelog = 1\n")],
    "changelog path a string": [(CHANGELOG, CHANGELOG + "path = 1\n")],
    # [bump]
    "bump unknown key": [(BUMP, BUMP + "colour = 1\n")],
    "bump from enum": [('from = "headings"', 'from = "titles"')],
    "bump heading twice": [('patch = ["Fixed"]', 'patch = ["Added"]')],
    "bump no headings": [(HEADINGS, "")],
    "bump headings a list": [('patch = ["Fixed"]', 'patch = "Fixed"')],
    "bump paths unknown key": [('from = "headings"', 'from = "paths"\nminor_paths = ["src"]')],
    "bump paths required": [('from = "headings"', 'from = "paths"'), (HEADINGS, "")],
    "bump paths empty": [('from = "headings"', 'from = "paths"\nminor_paths = []'), (HEADINGS, "")],
    # [gates]
    "gates unknown key": add("\n[gates]\ncolour = 1\n"),
    "gates blocker_lanes enums": add('\n[gates]\nblocker_lanes = ["beta"]\n'),
    "gates freeze strings": add("\n[gates]\nfreeze = 1\n"),
    "gates a table": top("gates = 1"),
    # [publish], [bot]
    "publish unknown key": add("\n[publish]\ncolour = 1\n"),
    "publish tag_message a string": add("\n[publish]\ntag_message = 1\n"),
    "bot unknown key": add("\n[bot]\ncolour = 1\n"),
    "bot email a string": add("\n[bot]\nemail = 1\n"),
    # [lanes]
    "lanes unknown lane": [("[lanes.dev]", "[lanes.beta]")],
    "lanes stable promotes without rc": [('[lanes.rc]\nschedule = ["daily 07:00 UTC"]\ngithub_release = true\n', "")],
    "lanes none": [(POLICY[POLICY.index("[lanes.dev]") : POLICY.index("[[version_lines]]")], "[lanes]\n\n")],
    "lane hotfix unknown key": [("[lanes.hotfix]\n", "[lanes.hotfix]\nschedule = ['Mon 07:00 UTC']\n")],
    "lane dev unknown key": [("quiet_minutes = 30", "quiet_minutes = 30\nmarker = 'a'")],
    "lane quiet range": [("quiet_minutes = 30", "quiet_minutes = 301")],
    "lane quiet not a bool": [("quiet_minutes = 30", "quiet_minutes = true")],
    "lane promote_from": [('promote_from = "rc"', 'promote_from = "dev"')],
    "lane soak without promote": [('promote_from = "rc"\n', "")],
    "lane marker": [("[lanes.rc]\n", "[lanes.rc]\nmarker = 'c'\n")],
    "lane dispatch": [('dispatch = ["publish.yml"]', 'dispatch = ["../x.yml"]')],
    "lane milestone a bool": [("quiet_minutes = 30", "quiet_minutes = 30\nmilestone = 1")],
    "lane a table": [("[lanes.dev]\nquiet_minutes = 30\n", ""), ("[lanes.rc]\n", "[lanes]\ndev = 1\n\n[lanes.rc]\n")],
    # [[version_lines]]
    "version_lines unknown key": [(VERSION_LINE, VERSION_LINE + "colour = 1\n")],
    "version_lines pattern": [("pattern = 'demo \\S+$'", "pattern = 'demo ('")],
    "version_lines array of tables": [(VERSION_LINE, ""), (TOP, TOP + "version_lines = [1]\n")],
    # [environments]
    "environments unknown key": add(STAGING + 'region = "eu"\n'),
    "environments a table": add("\n[environments]\nstaging = 1\n"),
    "environments the section a table": top("environments = 1"),
    # [operate]
    "operate unknown key": add(STAGING + "\n[operate]\nrollback = 3\n"),
    "operate range": add(STAGING + "\n[operate]\nrollback_after = 0\n"),
    "operate integer": add(STAGING + '\n[operate]\nrollback_after = "3"\n'),
    "operate empty label": add(STAGING + '\n[operate]\nincident_label = " "\n'),
    "operate a table": top("operate = 1"),
    # [autonomy]
    "autonomy unknown key": add('\n[autonomy]\nreleases = "act"\n'),
    "autonomy level": add('\n[autonomy]\nrelease = "auto"\n'),
    "autonomy level a string": add("\n[autonomy]\nrollback = true\n"),
    "autonomy deploy a table": add('\n[autonomy]\ndeploy = "propose"\n'),
    "autonomy deploy level": add(STAGING + '\n[autonomy]\ndeploy.staging = "yes"\n'),
    "autonomy deploy environment name": add('\n[autonomy]\ndeploy."pro duction" = "act"\n'),
    "autonomy deploy unknown environment": add(STAGING + '\n[autonomy]\ndeploy.qa = "act"\n'),
    "autonomy a table": top("autonomy = 1"),
    # [agents]
    "agents unknown key": add('\n[agents]\nprompt = "/t"\nwhen = 1\n'),
    "agents prompt required": add("\n[agents]\nprs = true\n"),
    "agents prompt empty": add('\n[agents]\nprompt = "  "\n'),
    "agents retry range": add('\n[agents]\nprompt = "/t"\nretry_hours = 0\n'),
    "agents prs a bool": add('\n[agents]\nprompt = "/t"\nprs = "yes"\n'),
    "agents a table": top("agents = 1"),
}


MESSAGES: dict[str, str] = {
    "top unknown key": "shipyard.toml: unknown keys ['colour']; allowed: ['after_stamp', 'agents', 'autonomy', 'bot', 'branch', 'bump', 'changelog', 'environments', 'gates', 'lanes', 'mode', 'name', 'operate', 'publish', 'version_files', 'version_lines']",
    "top name required": "shipyard.toml: name is required",
    "top name a string": "shipyard.toml: name must be a string, got 1",
    "top mode enum": "shipyard.toml: mode must be one of ['off', 'dry-run', 'release'], got 'on'",
    "top version_files enum": "shipyard.toml: version_files must be one of ['pyproject', 'none'], got 'setup'",
    "top after_stamp a list": "shipyard.toml: after_stamp must be a list of strings, got 'x'",
    "top after_stamp non-empty strings": "shipyard.toml: after_stamp must be a list of non-empty strings",
    "top after_stamp empty command": "shipyard.toml: after_stamp holds an empty command",
    "changelog unknown key": "shipyard.toml [changelog]: unknown keys ['colour']; allowed: ['path', 'style']",
    "changelog style enum": "shipyard.toml [changelog]: style must be one of ['keep-a-changelog', 'dash'], got 'plain'",
    "changelog required": "shipyard.toml: changelog is required",
    "changelog a table": "shipyard.toml: changelog must be a table, got 1",
    "changelog path a string": "shipyard.toml [changelog]: path must be a string, got 1",
    "bump unknown key": "shipyard.toml [bump]: unknown keys ['colour']; allowed: ['from', 'major', 'minor', 'patch']",
    "bump from enum": "shipyard.toml [bump]: from must be one of ['headings', 'paths'], got 'titles'",
    "bump heading twice": "shipyard.toml: [bump] lists heading 'Added' twice",
    "bump no headings": "shipyard.toml: [bump] from = 'headings' needs major, minor, or patch lists",
    "bump headings a list": "shipyard.toml [bump]: patch must be a list of strings, got 'Fixed'",
    "bump paths unknown key": "shipyard.toml [bump]: unknown keys ['major', 'minor', 'patch']; allowed: ['from', 'minor_paths']",
    "bump paths required": "shipyard.toml [bump]: minor_paths is required",
    "bump paths empty": "shipyard.toml: [bump] from = 'paths' needs a non-empty minor_paths",
    "gates unknown key": "shipyard.toml [gates]: unknown keys ['colour']; allowed: ['blocker_label', 'blocker_lanes', 'freeze', 'freeze_lanes']",
    "gates blocker_lanes enums": "shipyard.toml [gates]: blocker_lanes holds values outside ['dev', 'rc', 'stable', 'hotfix']",
    "gates freeze strings": "shipyard.toml [gates]: freeze must be a list of strings, got 1",
    "gates a table": "shipyard.toml: gates must be a table, got 1",
    "publish unknown key": "shipyard.toml [publish]: unknown keys ['colour']; allowed: ['release_title', 'tag_message']",
    "publish tag_message a string": "shipyard.toml [publish]: tag_message must be a string, got 1",
    "bot unknown key": "shipyard.toml [bot]: unknown keys ['colour']; allowed: ['email', 'name']",
    "bot email a string": "shipyard.toml [bot]: email must be a string, got 1",
    "lanes unknown lane": "shipyard.toml [lanes]: unknown keys ['beta']; allowed: ['dev', 'hotfix', 'rc', 'stable']",
    "lanes stable promotes without rc": "shipyard.toml: lanes.stable promotes from rc, but lanes.rc is not enabled",
    "lanes none": "shipyard.toml: [lanes] enables no lane",
    "lane hotfix unknown key": "shipyard.toml [lanes] [hotfix]: unknown keys ['schedule']; allowed: ['dispatch', 'github_release']",
    "lane dev unknown key": "shipyard.toml [lanes] [dev]: unknown keys ['marker']; allowed: ['dispatch', 'github_release', 'milestone', 'quiet_minutes', 'schedule']",
    "lane quiet range": "shipyard.toml [lanes] [dev]: quiet_minutes must be in 0..300, got 301",
    "lane quiet not a bool": "shipyard.toml [lanes] [dev]: quiet_minutes must be an integer, got True",
    "lane promote_from": "shipyard.toml [lanes] [stable]: promote_from is 'rc' or absent, got 'dev'",
    "lane soak without promote": "shipyard.toml [lanes] [stable]: min_soak_days needs promote_from = 'rc'",
    "lane marker": "shipyard.toml [lanes] [rc]: marker is a, b, or rc, got 'c'",
    "lane dispatch": "shipyard.toml [lanes] [stable]: dispatch names a file in .github/workflows, got '../x.yml'",
    "lane milestone a bool": "shipyard.toml [lanes] [dev]: milestone must be true or false, got 1",
    "lane a table": "shipyard.toml [lanes]: dev must be a table, got 1",
    "version_lines unknown key": "shipyard.toml [[version_lines]][0]: unknown keys ['colour']; allowed: ['file', 'pattern', 'replace']",
    "version_lines pattern": "shipyard.toml [[version_lines]][0].pattern: missing ), unterminated subpattern at position 5",
    "version_lines array of tables": "shipyard.toml: version_lines must be an array of tables, [[version_lines]]",
    "environments unknown key": "shipyard.toml [environments] [staging]: unknown keys ['region']; allowed: ['bake_minutes', 'from', 'health', 'lane', 'workflow']",
    "environments a table": "shipyard.toml [environments]: staging must be a table, got 1",
    "environments the section a table": "shipyard.toml: environments must be a table, got 1",
    "operate unknown key": "shipyard.toml [operate]: unknown keys ['rollback']; allowed: ['incident_label', 'rollback_after']",
    "operate range": "shipyard.toml [operate]: rollback_after must be in 1..20, got 0",
    "operate integer": "shipyard.toml [operate]: rollback_after must be an integer, got '3'",
    "operate empty label": "shipyard.toml [operate]: incident_label must not be empty",
    "operate a table": "shipyard.toml: operate must be a table, got 1",
    "autonomy unknown key": "shipyard.toml [autonomy]: unknown keys ['releases']; allowed: ['deploy', 'release', 'rollback']",
    "autonomy level": "shipyard.toml [autonomy]: release must be one of ['observe', 'propose', 'act'], got 'auto'",
    "autonomy level a string": "shipyard.toml [autonomy]: rollback must be one of ['observe', 'propose', 'act'], got True",
    "autonomy deploy a table": "shipyard.toml [autonomy]: deploy is a table of environments, such as deploy.production = 'propose'",
    "autonomy deploy level": "shipyard.toml [autonomy] [deploy]: staging must be one of ['observe', 'propose', 'act'], got 'yes'",
    "autonomy deploy environment name": "an environment name is letters, digits, '.', '_', or '-'; got 'pro duction'",
    "autonomy deploy unknown environment": "shipyard.toml [autonomy]: deploy.qa names no environment in [environments]; known: staging",
    "autonomy a table": "shipyard.toml: autonomy must be a table, got 1",
    "agents unknown key": "shipyard.toml [agents]: unknown keys ['when']; allowed: ['prompt', 'prs', 'retry_hours']",
    "agents prompt required": "shipyard.toml [agents]: prompt is required",
    "agents prompt empty": "shipyard.toml [agents]: prompt must not be empty",
    "agents retry range": "shipyard.toml [agents]: retry_hours must be in 1..168, got 0",
    "agents prs a bool": "shipyard.toml [agents]: prs must be true or false, got 'yes'",
    "agents a table": "shipyard.toml: agents must be a table, got 1",
}


def test_each_case_has_its_message() -> None:
    assert CASES.keys() == MESSAGES.keys()


@pytest.mark.parametrize("name", CASES)
def test_the_config_refuses_with_the_same_words(name: str) -> None:
    with pytest.raises(ReleaseError) as caught:
        Policy.parse(tomllib.loads(apply(POLICY, CASES[name])), "shipyard.toml")
    assert str(caught.value) == MESSAGES[name]


def refusal(load: Callable[[], object]) -> str:
    with pytest.raises(ReleaseError) as caught:
        load()
    return str(caught.value)


def test_the_file_is_read_with_the_same_words(tmp_path: Path) -> None:
    path = tmp_path / ".github" / "shipyard.toml"
    assert refusal(lambda: Policy.load(path)) == f"no release policy at {path}"
    path.parent.mkdir()
    path.write_text("name demo\n", encoding="utf-8")
    assert (
        refusal(lambda: Policy.load(path))
        == f"{path}: Expected '=' after a key in a key/value pair (at line 1, column 6)"
    )


def test_the_agents_section_alone_is_read_with_the_same_words(tmp_path: Path) -> None:
    assert refusal(lambda: AgentsConfig.load(tmp_path)) == (
        f"no .github/shipyard.toml in {tmp_path}; add an [agents] section with a prompt to use the gate"
    )
    path = tmp_path / ".github" / "release-policy.toml"
    path.parent.mkdir()
    path.write_text("name demo\n", encoding="utf-8")
    assert refusal(lambda: AgentsConfig.load(tmp_path)) == (
        f"{path}: Expected '=' after a key in a key/value pair (at line 1, column 6)"
    )
    path.write_text('name = "demo"\n', encoding="utf-8")
    assert refusal(lambda: AgentsConfig.load(tmp_path)) == (
        f"{path} has no [agents] section; add one with a prompt to use the gate"
    )
    path.write_text("agents = 1\n", encoding="utf-8")
    assert refusal(lambda: AgentsConfig.load(tmp_path)) == "release-policy.toml: agents must be a table, got 1"
    path.write_text('[agents]\nprompt = "/t"\nwhen = 1\n', encoding="utf-8")
    assert refusal(lambda: AgentsConfig.load(tmp_path)) == (
        "release-policy.toml [agents]: unknown keys ['when']; allowed: ['prompt', 'prs', 'retry_hours']"
    )
