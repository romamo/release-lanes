"""Release autonomy propose: one issue per lane saying what shipyard would release, opened
once and then kept up to date, so a person can release it by starting the lane"""

from dataclasses import dataclass
from enum import StrEnum

from shipyard.autonomy import HOLD_LABEL
from shipyard.changelog import Changelog, Entry
from shipyard.doctor import CALLER
from shipyard.errors import ReleaseError
from shipyard.github import GitHub
from shipyard.gitrepo import Git
from shipyard.planner import Proposal
from shipyard.policy import Lane, Policy


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
        wanted = (title(proposal), body(policy, proposal, Changelog(text, policy.style).pending()))
        try:
            found = github.find_issue(marker(proposal.lane))
            if found is None:
                done.append(Proposed(proposal, github.create_issue(*wanted), Outcome.OPENED))
            elif (found.title, found.body.replace("\r\n", "\n").strip()) == (wanted[0], wanted[1].strip()):
                done.append(Proposed(proposal, found.number, Outcome.UNCHANGED))
            else:
                github.update_issue(found.number, *wanted)
                done.append(Proposed(proposal, found.number, Outcome.UPDATED))
        except ReleaseError as exc:
            fix = f"change `issues: read` to `issues: write` on the prepare job in {CALLER}"
            hint = f"if GitHub refused it (403): {fix}"
            raise ReleaseError(f"{exc}; {hint}") from exc
    return done
