"""Release autonomy propose: one issue per lane saying what shipyard would release, opened
once and then kept up to date, so a person can release it by starting the lane"""

import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum

from shipyard.autonomy import HOLD_LABEL, Autonomy
from shipyard.changelog import Changelog, Entry
from shipyard.doctor import CALLER
from shipyard.errors import ReleaseError
from shipyard.github import GitHub
from shipyard.gitrepo import Git
from shipyard.planner import Proposal
from shipyard.policy import Lane, Policy
from shipyard.version import Version


class Outcome(StrEnum):
    OPENED = "opened"
    UPDATED = "updated"
    UNCHANGED = "unchanged"


@dataclass(frozen=True, slots=True)
class Proposed:
    proposal: Proposal
    issue: int
    outcome: Outcome


def marker(lane: Lane) -> str:
    """The hidden line that finds a lane's proposal issue again, whatever its title says"""
    return f"<!-- shipyard:propose lane={lane} -->"


def title(proposal: Proposal) -> str:
    return f"Ready to release {proposal.version.tag} on {proposal.lane}"


def body(policy: Policy, proposal: Proposal, entries: list[Entry]) -> str:
    start = f"gh workflow run {CALLER.name} -f lane={proposal.lane} -f dry-run=false"
    if proposal.cause.startswith(f"held by {HOLD_LABEL}"):
        how = f"Close the open `{HOLD_LABEL}` issues first; then start the lane by hand:"
    else:
        how = "To release it, start the lane by hand:"
    lines = [
        marker(proposal.lane),
        f"shipyard would release **{proposal.version.tag}** on the {proposal.lane} lane now, but {proposal.cause}.",
        "",
        f"Base: `{proposal.base}`",
        "",
        "### Entries",
        "",
    ]
    heading: str | None = None
    for entry in entries:
        if entry.heading is not None and entry.heading != heading:
            heading = entry.heading
            lines += [f"#### {heading}", ""]
        lines.append(entry.text)
    if not entries:
        lines.append("Nothing pending under Unreleased.")
    lines += [
        "",
        how,
        "",
        "```",
        start,
        "```",
        "",
        f"Each {policy.name} release run updates this issue while the release waits. Close it once released.",
    ]
    return "\n".join(lines) + "\n"


def propose(git: Git, policy: Policy, github: GitHub, proposals: tuple[Proposal, ...]) -> list[Proposed]:
    done = []
    for proposal in proposals:
        text = git.show(proposal.base, policy.changelog)
        if text is None:
            raise ReleaseError(f"no {policy.changelog} at {proposal.base[:12]}")
        wanted = body(policy, proposal, Changelog(text, policy.style).pending())
        fix = f"change `issues: read` to `issues: write` on the prepare job in {CALLER}"
        number, outcome = upsert(github, marker(proposal.lane), title(proposal), wanted, fix)
        done.append(Proposed(proposal, number, outcome))
    return done


def upsert(github: GitHub, mark: str, title: str, body: str, fix: str) -> tuple[int, Outcome]:
    """Open the issue whose body holds the marker, or bring it up to date; fix says how to
    grant `issues: write` when GitHub refuses"""
    try:
        found = github.find_issue(mark)
        if found is None:
            return github.create_issue(title, body), Outcome.OPENED
        if (found.title, found.body.replace("\r\n", "\n").strip()) == (title, body.strip()):
            return found.number, Outcome.UNCHANGED
        github.update_issue(found.number, title, body)
        return found.number, Outcome.UPDATED
    except ReleaseError as exc:
        raise ReleaseError(f"{exc}; if GitHub refused it (403): {fix}") from exc


def run_url(env: Mapping[str, str]) -> str | None:
    """The Actions run's URL, from the variables GitHub sets in every job"""
    parts = [env.get(name, "") for name in ("GITHUB_SERVER_URL", "GITHUB_REPOSITORY", "GITHUB_RUN_ID")]
    return "{}/{}/actions/runs/{}".format(*parts) if all(parts) else None


def close_released(policy: Policy, github: GitHub, lane: Lane, version: Version, run: str | None) -> str:
    """Close the lane's open proposal issue once land tagged its release, saying so; only
    under release = "propose", the one setup whose callers grant the land job `issues: write`"""
    if policy.autonomy.release is not Autonomy.PROPOSE:
        return f"release autonomy is {policy.autonomy.release}: no proposal to close"
    fix = f"grant `issues: write` to the land job in {CALLER}"
    try:
        found = github.find_issue(marker(lane))
        if found is None:
            return f"no open proposal for the {lane} lane"
        text = f"Released {version.tag} on the {lane} lane" + (f" in {run}" if run else "") + "."
        named = re.search(r"would release \*\*(\S+)\*\*", found.body)
        if named is None or named[1] != version.tag:
            proposed = named[1] if named else "another version"
            text += f" This issue proposed {proposed}; the lane released {version.tag}, so it is closed too."
        github.close_issue(found.number, text)
    except ReleaseError as exc:
        raise ReleaseError(f"{exc}; if GitHub refused it (403): {fix}") from exc
    return f"closed proposal #{found.number}: {version.tag} released"
