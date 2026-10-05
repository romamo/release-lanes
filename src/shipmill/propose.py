"""Release autonomy propose: one issue per lane saying what shipmill would release, opened
once and then kept up to date, so a person can release it by starting the lane"""

import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum

from shipmill.autonomy import HOLD_LABEL, Autonomy
from shipmill.changelog import Changelog, Entry
from shipmill.doctor import CALLER
from shipmill.errors import ReleaseError
from shipmill.github import OPEN_LIMIT, PROPOSAL_LABEL, GitHub, Issue
from shipmill.gitrepo import Git
from shipmill.planner import Proposal
from shipmill.policy import Lane, Policy
from shipmill.version import Version


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
    return f"<!-- shipmill:propose lane={lane} -->"


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
        f"shipmill would release **{proposal.version.tag}** on the {proposal.lane} lane now, but {proposal.cause}.",
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


def _find(github: GitHub, mark: str) -> tuple[Issue, bool] | None:
    """The open proposal whose body holds the marker, and whether it has PROPOSAL_LABEL: by
    the label, whatever the repository's size; else by scanning the newest open issues, for
    a proposal opened before the label, which its next update labels"""
    proposals = github.open_labelled_issues(PROPOSAL_LABEL)
    if len(proposals) >= OPEN_LIMIT:  # more may be cut off: refuse rather than open a duplicate
        raise ReleaseError(
            f"{len(proposals)} open issues carry the {PROPOSAL_LABEL} label, as many as shipmill reads;"
            " close the stale ones"
        )
    labelled = [i for i in proposals if mark in i.body]
    if labelled:
        return min(labelled, key=lambda i: i.number), True
    # scan on every miss, not once: a repo that upgraded mid-proposal still has unlabelled ones (#65)
    found = github.find_issue(mark)
    return None if found is None else (found, False)


def find_proposal(github: GitHub, mark: str) -> Issue | None:
    """The open proposal whose body holds the marker"""
    found = _find(github, mark)
    return None if found is None else found[0]


def upsert(github: GitHub, mark: str, title: str, body: str, fix: str) -> tuple[int, Outcome]:
    """Open the issue whose body holds the marker, or bring it up to date; fix says how to
    grant `issues: write` when GitHub refuses"""
    try:
        found = _find(github, mark)
        if found is None:
            return github.create_issue(title, body, (PROPOSAL_LABEL,)), Outcome.OPENED
        issue, labelled = found
        if labelled and (issue.title, issue.body.replace("\r\n", "\n").strip()) == (title, body.strip()):
            return issue.number, Outcome.UNCHANGED
        github.update_issue(issue.number, title, body, () if labelled else (PROPOSAL_LABEL,))
        return issue.number, Outcome.UPDATED
    except ReleaseError as exc:
        raise ReleaseError(f"{exc}; if GitHub refused it (403): {fix}") from exc


def run_url(env: Mapping[str, str]) -> str | None:
    """The Actions run's URL, from the variables GitHub sets in every job"""
    parts = [env.get(name, "") for name in ("GITHUB_SERVER_URL", "GITHUB_REPOSITORY", "GITHUB_RUN_ID")]
    return "{}/{}/actions/runs/{}".format(*parts) if all(parts) else None


def close_released(policy: Policy, github: GitHub, lane: Lane, version: Version, run: str | None) -> str:
    """Close the lane's open proposal issue once land tagged its release, saying so; only
    under release = "propose", the one setup whose callers grant the land job `issues: write`.
    A proposal for a later version than the one released stays open, as operate leaves one"""
    if policy.autonomy.release is not Autonomy.PROPOSE:
        return f"release autonomy is {policy.autonomy.release}: no proposal to close"
    fix = f"grant `issues: write` to the land job in {CALLER}"
    try:
        found = find_proposal(github, marker(lane))
        if found is None:
            return f"no open proposal for the {lane} lane"
        named = re.search(r"would release \*\*(\S+)\*\*", found.body)
        if named is not None and Version.of_tag(named[1]) > version:
            return f"proposal #{found.number} names {named[1]}, later than {version.tag}: left open"
        text = f"Released {version.tag} on the {lane} lane" + (f" in {run}" if run else "") + "."
        if named is None or named[1] != version.tag:
            proposed = named[1] if named else "another version"
            text += f" This issue proposed {proposed}; the lane released {version.tag}, so it is closed too."
        github.close_issue(found.number, text)
    except ReleaseError as exc:
        raise ReleaseError(f"{exc}; if GitHub refused it (403): {fix}") from exc
    return f"closed proposal #{found.number}: {version.tag} released"
