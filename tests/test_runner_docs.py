"""Spec 015, Docs and shipmill-setup: the self-hosted runner docs and setup's runner step"""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def folded(*parts: str) -> str:
    """A doc with its line wrapping folded, so a statement reads as one line"""
    return " ".join(ROOT.joinpath(*parts).read_text(encoding="utf-8").split())


def section(doc: str, heading: str) -> str:
    return doc.split(heading, 1)[1].split(" ### ", 1)[0].split(" ## ", 1)[0]


def test_s015_10_release_lanes_documents_self_hosted_runners() -> None:
    doc = folded("docs", "release-lanes.md")
    runners = section(doc, "### Self-hosted runners")
    assert "take a `runs-on` input" in runners and "`shipmill init --runs-on <runner>`" in runners
    assert "Without it every job runs on `ubuntu-latest`" in runners
    for needed in ("`bash`", "`git`", "`curl`", "`github.com`", "`api.github.com`", "`astral-sh/setup-uv`"):
        assert needed in runners, needed
    assert "A runner takes one job at a time." in runners
    assert "the `settle` job holds the runner for up to `quiet_minutes`" in runners
    assert "Give shipmill a runner of its own" in runners and "drop `quiet_minutes` for a `schedule`" in runners


def test_s015_10_release_lanes_says_how_init_derives_the_schedule() -> None:
    doc = folded("docs", "release-lanes.md")
    schedule = section(doc, "### The schedule in `release.yml`")
    assert "`shipmill init` derives `release.yml`'s cron from the policy" in schedule
    assert "`shipmill init --caller --force`" in schedule
    assert "**The hourly tick** (`7 * * * *`)" in schedule and "**One cron per window time**" in schedule
    assert "**No schedule**" in schedule
    held = "a lane that was held (a blocker, a freeze, a hold) or had nothing pending when its window opened"
    assert f"{held} releases on the next push or the next window tick" in schedule
    # next to the policy's triggers, where `schedule` is explained
    assert doc.index("### The schedule in `release.yml`") < doc.index("### Cost on private repositories")


def test_s015_11_setup_asks_for_the_runner_and_warns_about_a_shared_one() -> None:
    skill = folded("skills", "shipmill-setup", "SKILL.md")
    step3 = skill.split("## 3. Choose the lanes with the user", 1)[1].split(" ## 4. ", 1)[0]
    private = step3.split("### On a private repository", 1)[1].split(" ### ", 1)[0]
    assert "**The runner.** On a private repo, ask with the decisions below" in private
    assert "GitHub-hosted (the default" in private and "a self-hosted runner, named by its labels" in private
    assert "a lane uses `quiet_minutes`, ask whether that runner also runs deploys" in private
    assert "the settle wait holds it for up to `quiet_minutes` after each push" in private
    assert "a deploy started meanwhile queues behind it" in private
    assert "give shipmill a runner of its own (another label or another instance)" in private
    assert "drop `quiet_minutes` for a `schedule`" in private
    assert "aren't possible yet" not in skill


def test_s015_11_setup_rewrites_release_yml_after_editing_the_policy() -> None:
    skill = folded("skills", "shipmill-setup", "SKILL.md")
    step4 = skill.split("## 4. Write the files", 1)[1].split(" ## 5. ", 1)[0]
    edit = step4.index("Edit the policy to the user's choices from step 3")
    rewrite = step4.index("$CR init --caller --force --ci <ci-file>.yml")
    assert edit < rewrite
    assert "add `--runs-on <label>` or `--runs-on '<JSON list>'`" in step4
