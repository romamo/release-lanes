import re
from pathlib import Path

PREPARE = Path(__file__).parent.parent / ".github" / "workflows" / "prepare.yml"


def _job_concurrency(text: str, job: str) -> tuple[str, str]:
    match = re.search(
        rf"^  {job}:\n(?:    \S.*\n)*?    concurrency:\n"
        r"      group: (?P<group>.+)\n"
        r"      cancel-in-progress: (?P<cancel>\S+)$",
        text,
        re.MULTILINE,
    )
    assert match is not None, f"no concurrency block in the {job} job"
    return match["group"], match["cancel"]


def test_only_a_push_cancels_the_settle_wait() -> None:
    # A schedule or workflow_dispatch run used to share the push run's settle group and
    # cancel its quiet-window wait, then plan skip (#6)
    group, cancel = _job_concurrency(PREPARE.read_text(), "settle")
    assert group == (
        "${{ github.event_name == 'push' && 'shipyard-settle' || format('shipyard-settle-{0}', github.run_id) }}"
    )
    assert cancel == "true"


def test_prepare_queues_and_never_cancels() -> None:
    group, cancel = _job_concurrency(PREPARE.read_text(), "prepare")
    assert (group, cancel) == ("shipyard", "false")
