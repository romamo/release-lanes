"""github-ship-watch's fleet report (spec S-001): the fleet file, from fixtures"""

import datetime as dt
import importlib.util
import json
import subprocess
import sys
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType
from typing import Any

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
repo = "romamo/shipmill"

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
        fl.Entry("romamo/shipmill", None),
        fl.Entry("owner/other", "sev"),
    ]


def test_the_plain_form_reads_as_tomllib_reads_it(fl: ModuleType, tmp_path: Path) -> None:
    path = tmp_path / "fleet.toml"
    want = {"repos": [{"repo": "romamo/shipmill"}, {"repo": "owner/other", "incident_label": "sev"}]}
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
        ('[[repos]]\nrepo = "o/r"\n\n[[repos]]\nrepo = "shipmill"\n', "repos[1]: repo 'shipmill' is not in owner/name"),
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
    path = write(tmp_path, FLEET + '\n[[repos]]\nrepo = "Romamo/Shipmill"\n')
    assert refusal(fl, path) == f"error: {path}: lists Romamo/Shipmill twice"


def test_s001_8_the_fleet_files_incident_label_is_passed_to_the_watch(fl: ModuleType, tmp_path: Path) -> None:
    shipmill, other = fl.load(write(tmp_path, FLEET))
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
    assert "--incident-label" not in fl.watch_command(shipmill, checkout)


def test_s001_8_the_watch_uses_the_given_incident_label_over_the_configs(ws: ModuleType) -> None:
    policy = Path(".github/shipmill.toml")
    text = 'mode = "release"\n\n[operate]\nincident_label = "sev1"\n'
    assert ws.config(text, policy).incident_label == "sev1"
    assert ws.config(text, policy, "sev").incident_label == "sev"
    assert ws.config('mode = "release"\n', policy, "sev").incident_label == "sev"
    args = ws.arguments().parse_args(["owner/other", "--incident-label", "sev"])
    assert args.incident_label == "sev"
    assert ws.arguments().parse_args(["owner/other"]).incident_label is None


# -- the report: watch_state.py, metrics.py, and the clone answered by a fake runner --------

NOW = dt.datetime(2026, 10, 5, 7, 0, tzinfo=dt.timezone.utc)  # noqa: UP017 (runs under 3.10 too)
NOT_FOUND = "GraphQL: Could not resolve to a Repository with the name 'owner/gone'. (repository)\nmore"


def row(state: str, subject: str, detail: str = "") -> dict[str, str]:
    return {"state": state, "subject": subject, "detail": detail}


def proc(code: int, out: str = "", err: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess([], code, out, err)


def measures(*texts: str) -> dict[str, Any]:
    names = ("Deploy frequency", "Lead time")
    return {
        "repo": "o/r",
        "days": 30,
        "start": "2026-09-05T07:00:00+00:00",
        "end": NOW.isoformat(),
        "measures": [
            {"measure": n, "value": None if t == "no data" else 1.0, "unit": "hours", "text": t}
            for n, t in zip(names, texts, strict=True)
        ],
    }


@dataclass
class Fake:
    """Answers each repo's clone, watch_state.py, and metrics.py as a real run would"""

    watch: dict[str, list[dict[str, str]]] = field(default_factory=dict)  # rows by repo; exit 1 on an action
    metrics: dict[str, dict[str, Any]] = field(default_factory=dict)
    failing: dict[str, tuple[str, str]] = field(default_factory=dict)  # repo: (the step that fails, its stderr)
    calls: list[list[str]] = field(default_factory=list)

    def __call__(self, cmd: Sequence[str]) -> subprocess.CompletedProcess[str]:
        cmd = list(cmd)
        self.calls.append(cmd)
        if cmd[:3] == ["gh", "repo", "clone"]:
            step, repo = "clone", cmd[3]
        else:
            step, repo = Path(cmd[1]).name, cmd[2]
        failing = self.failing.get(repo)
        if failing is not None and failing[0] == step:
            return proc(2, err=failing[1])
        if step == "clone":
            assert Path(cmd[4]).parent.is_dir()
            return proc(0)
        if step == "watch_state.py":
            rows = self.watch[repo]
            action = any(r["state"] in WATCH_ACTION for r in rows)
            return proc(1 if action else 0, "".join(json.dumps(r, sort_keys=True) + "\n" for r in rows))
        assert step == "metrics.py", cmd
        return proc(0, json.dumps(self.metrics[repo]))


WATCH_ACTION = {"BOT_FAILED", "UNANNOUNCED", "ISSUES", "INCIDENT_OPEN"}  # the action states these fixtures use

THREE = """\
[[repos]]
repo = "romamo/shipmill"

[[repos]]
repo = "owner/gone"

[[repos]]
repo = "owner/other"
incident_label = "sev"
"""


def fleet_run(
    fl: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str], fake: Fake, text: str, *flags: str
) -> tuple[int, str]:
    path = write(tmp_path, text)
    code = fl.main(["report", "--fleet", str(path), *flags], runner=fake, clock=lambda: NOW)
    return code, capsys.readouterr().out


def quiet_fake() -> Fake:
    return Fake(
        watch={
            "romamo/shipmill": [row("HOLD", "#7", "by @amy, 2 h"), row("BOT_OK", "release.yml")],
            "owner/other": [row("PUBLISHED", "v1.2.0", "other 1.2.0 on PyPI"), row("PRS_OPEN", "owner/other", "#4")],
        }
    )


def test_the_watch_action_states_are_watch_states(fl: ModuleType, ws: ModuleType) -> None:
    assert fl.ACTION == ws.ACTION


def test_s001_1_every_repos_rows_are_named_with_actions_first(
    fl: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = quiet_fake()
    fake.watch["romamo/shipmill"].append(row("UNANNOUNCED", "v0.11.0", "#70 (since v0.10.0)"))
    fake.watch["owner/other"].insert(0, row("INCIDENT_OPEN", "#9", "prod down, 1 h"))
    _, out = fleet_run(fl, tmp_path, capsys, fake, FLEET)
    lines = out.splitlines()
    assert lines[:2] == [
        "romamo/shipmill UNANNOUNCED    v0.11.0          #70 (since v0.10.0)",
        "owner/other     INCIDENT_OPEN  #9               prod down, 1 h",
    ]
    assert lines[2:] == [
        "romamo/shipmill HOLD           #7               by @amy, 2 h",
        "romamo/shipmill BOT_OK         release.yml",
        "owner/other     PUBLISHED      v1.2.0           other 1.2.0 on PyPI",
        "owner/other     PRS_OPEN       owner/other      #4",
    ]


def test_s001_1_each_repo_is_watched_on_its_own_clone(
    fl: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = quiet_fake()
    fleet_run(fl, tmp_path, capsys, fake, FLEET)
    clones = [c for c in fake.calls if c[:3] == ["gh", "repo", "clone"]]
    watches = [c for c in fake.calls if c[1] == str(fl.WATCH_STATE)]
    assert [c[3] for c in clones] == ["romamo/shipmill", "owner/other"]
    assert [c[2] for c in watches] == ["romamo/shipmill", "owner/other"]
    assert [c[c.index("--repo-dir") + 1] for c in watches] == [c[4] for c in clones]
    assert not any(c[1] == str(fl.METRICS) for c in fake.calls)  # metrics only with --metrics


def test_s001_2_the_report_exits_1_on_any_action_row_and_0_on_none(
    fl: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = quiet_fake()
    assert fleet_run(fl, tmp_path, capsys, fake, FLEET)[0] == 0  # a HOLD and PRS_OPEN are report-only
    fake.watch["owner/other"].append(row("ISSUES", "owner/other", "NEW #12"))
    assert fleet_run(fl, tmp_path, capsys, fake, FLEET)[0] == 1


def test_s001_5_a_failed_check_is_one_repo_error_row_and_the_rest_is_reported(
    fl: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = quiet_fake()
    fake.failing["owner/gone"] = ("clone", NOT_FOUND)
    code, out = fleet_run(fl, tmp_path, capsys, fake, THREE)
    assert code == 2
    lines = out.splitlines()
    assert lines[0] == (
        "owner/gone      REPO_ERROR     gh repo clone    "
        "GraphQL: Could not resolve to a Repository with the name 'owner/gone'. (repository)"
    )
    assert [line.split()[0] for line in lines[1:]] == ["romamo/shipmill"] * 2 + ["owner/other"] * 2
    assert not any(c[2:3] == ["owner/gone"] and c[1] == str(fl.WATCH_STATE) for c in fake.calls)


def test_s001_5_a_failed_watch_or_metrics_is_a_repo_error_too(
    fl: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = quiet_fake()
    fake.failing["owner/other"] = ("watch_state.py", "error: gh repo view...: HTTP 403\nsecond line")
    code, out = fleet_run(fl, tmp_path, capsys, fake, FLEET)
    assert code == 2
    assert out.splitlines()[0] == "owner/other     REPO_ERROR     watch_state.py   error: gh repo view...: HTTP 403"
    assert sum("owner/other" in line for line in out.splitlines()) == 1

    fake = quiet_fake()
    fake.metrics["romamo/shipmill"] = measures("2.0 per week", "no data")
    fake.failing["owner/other"] = ("metrics.py", "error: gh api graphql: rate limited")
    code, out = fleet_run(fl, tmp_path, capsys, fake, FLEET, "--json", "--metrics")
    assert code == 2
    other = json.loads(out)["repos"][1]
    assert other["metrics"] is None
    assert other["rows"][-1] == row("REPO_ERROR", "metrics.py", "error: gh api graphql: rate limited")


def test_s001_5_a_failure_with_nothing_on_stderr_names_its_exit(fl: ModuleType) -> None:
    assert fl.error_row("watch_state.py", proc(-9)) == fl.Row(
        "REPO_ERROR", "watch_state.py", "watch_state.py exited -9"
    )


def test_s001_7_json_is_one_object_with_each_repos_rows(
    fl: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = quiet_fake()
    fake.failing["owner/gone"] = ("clone", NOT_FOUND)
    code, out = fleet_run(fl, tmp_path, capsys, fake, THREE, "--json")
    assert code == 2
    assert json.loads(out) == {
        "repos": [
            {"repo": "romamo/shipmill", "rows": fake.watch["romamo/shipmill"]},
            {"repo": "owner/gone", "rows": [row("REPO_ERROR", "gh repo clone", NOT_FOUND.splitlines()[0])]},
            {"repo": "owner/other", "rows": fake.watch["owner/other"]},
        ]
    }


def test_s001_7_json_holds_each_repos_metrics_with_metrics(
    fl: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = quiet_fake()
    fake.metrics = {"romamo/shipmill": measures("2.0 per week", "no data"), "owner/other": measures("no data", "1.0 h")}
    code, out = fleet_run(fl, tmp_path, capsys, fake, FLEET, "--json", "--metrics")
    assert code == 0
    found = json.loads(out)
    assert [r["metrics"] for r in found["repos"]] == [fake.metrics["romamo/shipmill"], fake.metrics["owner/other"]]
    runs = [c for c in fake.calls if c[1] == str(fl.METRICS)]
    assert [c[c.index("--until") + 1] for c in runs] == [NOW.isoformat()] * 2  # one window for the fleet
    assert "--incident-label" not in runs[0]
    assert runs[1][-2:] == ["--incident-label", "sev"]


def test_s001_6_metrics_sit_side_by_side_one_column_per_repo(
    fl: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = quiet_fake()
    fake.metrics = {
        "romamo/shipmill": measures("1.4/week (6 deploys)", "median 1.0 h, p90 1.9 h"),
        "owner/other": measures("no data", "no data"),
    }
    code, out = fleet_run(fl, tmp_path, capsys, fake, FLEET, "--metrics")
    assert code == 0
    watch, metrics = out.split("\n\n")
    assert len(watch.splitlines()) == 4
    assert metrics.splitlines() == [
        "Metrics: the 30 days to 2026-10-05 07:00 UTC",
        "Measure          romamo/shipmill         owner/other",
        "Deploy frequency 1.4/week (6 deploys)    no data",
        "Lead time        median 1.0 h, p90 1.9 h no data",
    ]


def test_s001_6_a_repo_whose_check_failed_has_no_metrics_column(
    fl: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = quiet_fake()
    fake.metrics = {"romamo/shipmill": measures("no data", "no data"), "owner/other": measures("no data", "no data")}
    fake.failing["owner/gone"] = ("clone", NOT_FOUND)
    code, out = fleet_run(fl, tmp_path, capsys, fake, THREE, "--metrics")
    assert code == 2
    assert out.split("\n\n")[1].splitlines()[1:] == [
        "Measure          romamo/shipmill owner/other",
        "Deploy frequency no data         no data",
        "Lead time        no data         no data",
    ]
    fake.failing = {"romamo/shipmill": ("clone", NOT_FOUND), "owner/other": ("metrics.py", "error: gh: 502")}
    code, out = fleet_run(fl, tmp_path, capsys, fake, FLEET, "--metrics")
    assert code == 2
    assert "Metrics" not in out  # no repo measured: no table


def test_s001_6_no_data_stays_no_data_in_json(
    fl: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = quiet_fake()
    fake.metrics = {"romamo/shipmill": measures("no data", "1.0 h"), "owner/other": measures("no data", "no data")}
    _, out = fleet_run(fl, tmp_path, capsys, fake, FLEET, "--metrics", "--json")
    first = json.loads(out)["repos"][0]["metrics"]["measures"][0]
    assert (first["value"], first["text"]) == (None, "no data")


def test_a_watch_line_that_isnt_a_row_stops_the_report(fl: ModuleType) -> None:
    with pytest.raises(SystemExit) as refused:
        fl.watch_rows("o/r", '{"state": "BOT_OK"}\n')
    assert "not a row" in str(refused.value)


def workdirs(fake: Fake) -> list[Path]:
    """The folder each clone went into: <temp>/fleet-*/<owner>/<name>"""
    return [Path(c[4]).parent.parent for c in fake.calls if c[:3] == ["gh", "repo", "clone"]]


@pytest.mark.parametrize(
    ("text", "failing"),
    [
        (FLEET, {}),
        (THREE, {"owner/gone": ("clone", NOT_FOUND)}),
        (FLEET, {"owner/other": ("watch_state.py", "error: HTTP 403")}),
    ],
)
def test_the_clones_go_in_a_temporary_folder_removed_after_the_run(
    fl: ModuleType,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    text: str,
    failing: dict[str, tuple[str, str]],
) -> None:
    fake = quiet_fake()
    fake.failing = failing
    fleet_run(fl, tmp_path, capsys, fake, text)
    found = workdirs(fake)
    assert len(found) == text.count("[[repos]]")
    assert len(set(found)) == 1  # one folder for the run
    assert found[0].name.startswith("fleet-")
    assert found[0].parent.resolve() == Path(tempfile.gettempdir()).resolve()
    assert not found[0].exists()


@dataclass
class Breaking(Fake):
    """A fake whose watch_state.py prints a traceback, or whose run is interrupted"""

    interrupt: bool = False

    def __call__(self, cmd: Sequence[str]) -> subprocess.CompletedProcess[str]:
        done = super().__call__(cmd)
        if Path(list(cmd)[1]).name != "watch_state.py":
            return done
        if self.interrupt:
            raise KeyboardInterrupt
        return proc(1, "Traceback (most recent call last):\n")


def test_a_watch_that_prints_no_json_stops_the_report_with_exit_2(
    fl: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = Breaking(watch=quiet_fake().watch)
    with pytest.raises(SystemExit) as refused:
        fleet_run(fl, tmp_path, capsys, fake, FLEET)
    assert refused.value.code == 2  # not a traceback's exit 1, which reads as an action row
    assert "watch_state.py for romamo/shipmill printed 'Traceback" in str(refused.value)
    assert not workdirs(fake)[0].exists()


def test_metrics_that_print_no_json_stop_the_report_with_exit_2(fl: ModuleType) -> None:
    with pytest.raises(SystemExit) as refused:
        fl.decoded("", "metrics.py for o/r")
    assert refused.value.code == 2
    assert str(refused.value) == "error: metrics.py for o/r printed '', not JSON"


def test_an_interrupted_run_removes_its_clones(
    fl: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = Breaking(watch=quiet_fake().watch, interrupt=True)
    with pytest.raises(KeyboardInterrupt):
        fleet_run(fl, tmp_path, capsys, fake, FLEET)
    assert not workdirs(fake)[0].exists()


def test_s001_3_the_report_refuses_a_malformed_fleet_file_with_exit_2(tmp_path: Path) -> None:
    path = write(tmp_path, '[[repos]]\nrepo = "shipmill"\n')
    done = subprocess.run(
        [sys.executable, str(SCRIPTS / "fleet.py"), "report", "--fleet", str(path)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert done.returncode == 2
    assert done.stdout == ""
    assert done.stderr == f"error: {path}: repos[0]: repo 'shipmill' is not in owner/name form\n"


# -- the skill documents the fleet --------------------------------------------------------


def test_s001_9_the_skill_documents_the_fleet_file_the_report_and_a_routine(fl: ModuleType) -> None:
    skill = (SCRIPTS.parent / "SKILL.md").read_text(encoding="utf-8")
    section = skill.split("\n## Fleet\n", 1)[1].split("\n## ", 1)[0]
    assert "[[repos]]" in section and "incident_label" in section  # the fleet file
    assert "scripts/fleet.py report --fleet" in section  # the report
    for flag in ("--metrics", "--json", "REPO_ERROR"):
        assert flag in section
    assert "/schedule" in section and "fleet.py report" in section.split("/schedule", 1)[1]  # a routine runs it
    example = section.split("```toml\n", 1)[1].split("```", 1)[0]
    assert fl.parse_plain(example, Path("fleet.toml")) == {
        "repos": [{"repo": "shipmill/shipmill"}, {"repo": "owner/other", "incident_label": "sev"}]
    }
