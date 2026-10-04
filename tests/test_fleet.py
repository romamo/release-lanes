"""github-ship-watch's fleet report (spec S-001): the fleet file, from fixtures"""

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "skills" / "github-ship-watch" / "scripts"


def load_script(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses look their module up by name
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def fl() -> ModuleType:
    return load_script("fleet")


@pytest.fixture(scope="module")
def ws() -> ModuleType:
    return load_script("watch_state")


FLEET = """\
# the fleet
[[repos]]
repo = "romamo/shipyard"

[[repos]]
repo = 'owner/other'
incident_label = "sev"   # that repo's incidents
"""


def write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "fleet.toml"
    path.write_text(text, encoding="utf-8")
    return path


def refusal(fl: ModuleType, path: Path) -> str:
    with pytest.raises(SystemExit) as refused:
        fl.load(path)
    assert refused.value.code == 2
    return str(refused.value)


def test_the_fleet_file_lists_the_repos_in_order(fl: ModuleType, tmp_path: Path) -> None:
    assert fl.load(write(tmp_path, FLEET)) == [
        fl.Entry("romamo/shipyard", None),
        fl.Entry("owner/other", "sev"),
    ]


def test_the_plain_form_reads_as_tomllib_reads_it(fl: ModuleType, tmp_path: Path) -> None:
    path = tmp_path / "fleet.toml"
    want = {"repos": [{"repo": "romamo/shipyard"}, {"repo": "owner/other", "incident_label": "sev"}]}
    assert fl.parse_plain(FLEET, path) == want
    if fl.tomllib is not None:  # Python 3.11+
        assert fl.tomllib.loads(FLEET) == want


@pytest.mark.parametrize(
    ("text", "problem"),
    [
        ('[[repos]]\nrepo = "o/r"\n[repos.extra]\nx = 1\n', "can't read"),
        ('[[repos]]\nrepo = """o/r"""\n', "can't read"),
        ('[[repos]]\nrepo = "o/r"\nrepo = "o/s"\n', "set twice"),
    ],
)
def test_the_plain_form_refuses_anything_else(fl: ModuleType, tmp_path: Path, text: str, problem: str) -> None:
    path = tmp_path / "fleet.toml"
    with pytest.raises(SystemExit) as refused:
        fl.parse_plain(text, path)
    assert refused.value.code == 2
    assert str(path) in str(refused.value) and problem in str(refused.value)


@pytest.mark.parametrize(
    ("text", "problem"),
    [
        ('[[repos]\nrepo = "o/r"\n', "not TOML" if sys.version_info >= (3, 11) else "can't read"),
        ("# nothing yet\n", "no [[repos]]"),
        ('owner = "o"\n', "unknown key 'owner'"),
        ('[[repos]]\nincident_label = "sev"\n', "repos[0] has no repo"),
        ('[[repos]]\nrepo = "o/r"\npath = "~/src/r"\n', "repos[0] has an unknown key 'path'"),
        ('[[repos]]\nrepo = "o/r"\n\n[[repos]]\nrepo = "shipyard"\n', "repos[1]: repo 'shipyard' is not in owner/name"),
        ('[[repos]]\nrepo = "o/r/x"\n', "not in owner/name form"),
        ('[[repos]]\nrepo = "o/.."\n', "not in owner/name form"),
        ('[[repos]]\nrepo = "o/r"\nincident_label = ""\n', "incident_label must be a non-empty string"),
    ],
)
def test_s001_3_a_malformed_fleet_file_is_refused_naming_the_file_and_the_problem(
    fl: ModuleType, tmp_path: Path, text: str, problem: str
) -> None:
    path = write(tmp_path, text)
    message = refusal(fl, path)
    assert str(path) in message
    assert problem in message


def test_s001_3_a_missing_fleet_file_is_refused(fl: ModuleType, tmp_path: Path) -> None:
    path = tmp_path / "absent.toml"
    assert refusal(fl, path) == f"error: {path}: no such file"


def test_s001_4_a_repo_listed_twice_is_refused(fl: ModuleType, tmp_path: Path) -> None:
    path = write(tmp_path, FLEET + '\n[[repos]]\nrepo = "Romamo/Shipyard"\n')
    assert refusal(fl, path) == f"error: {path}: lists Romamo/Shipyard twice"


def test_s001_8_the_fleet_files_incident_label_is_passed_to_the_watch(fl: ModuleType, tmp_path: Path) -> None:
    shipyard, other = fl.load(write(tmp_path, FLEET))
    checkout = tmp_path / "checkout"
    assert fl.watch_command(other, checkout) == [
        sys.executable,
        str(fl.WATCH_STATE),
        "owner/other",
        "--repo-dir",
        str(checkout),
        "--json",
        "--incident-label",
        "sev",
    ]
    assert "--incident-label" not in fl.watch_command(shipyard, checkout)


def test_s001_8_the_watch_uses_the_given_incident_label_over_the_configs(ws: ModuleType) -> None:
    policy = Path(".github/shipyard.toml")
    text = 'mode = "release"\n\n[operate]\nincident_label = "sev1"\n'
    assert ws.config(text, policy).incident_label == "sev1"
    assert ws.config(text, policy, "sev").incident_label == "sev"
    assert ws.config('mode = "release"\n', policy, "sev").incident_label == "sev"
    args = ws.arguments().parse_args(["owner/other", "--incident-label", "sev"])
    assert args.incident_label == "sev"
    assert ws.arguments().parse_args(["owner/other"]).incident_label is None
