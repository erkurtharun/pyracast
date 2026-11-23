from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from pyracast.notifications.models import RecipientRole

VISIBLE_MODE_RELEASED = False


def sent_hidden(hide_recipients: bool) -> bool:
    """Whether a schedule stored with this mode is sent hidden, under the gate as it is now."""
    return hide_recipients or not VISIBLE_MODE_RELEASED


def members_choice(choices: Iterable[RecipientRole]) -> RecipientRole | None:
    """The choice of an address's members: Hidden if any of them chose it or if those who chose
    disagree, otherwise the role they agree on."""
    found = set(choices)
    if RecipientRole.BCC in found or len(found) > 1:
        return RecipientRole.BCC
    return next(iter(found), None)


@dataclass(frozen=True)
class Candidate:
    """One address of a reminder's list. `member` and `administrator` describe the active members
    whose verified primary address it is; `chosen` is their combined choice and `typed` the
    choice of the typed row that holds the address, which counts only while `settled`."""

    address: str
    member: bool = False
    administrator: bool = False
    chosen: RecipientRole | None = None
    typed: RecipientRole | None = None
    settled: bool = True


def explicit_role(candidate: Candidate) -> RecipientRole | None:
    typed = candidate.typed if candidate.settled else None
    if RecipientRole.BCC in (candidate.chosen, candidate.typed):
        return RecipientRole.BCC
    return candidate.chosen or typed


def roles(
    candidates: Sequence[Candidate], *, hide_recipients: bool, include_workspace_members: bool
) -> list[RecipientRole]:
    if hide_recipients:
        return [RecipientRole.BCC] * len(candidates)
    placed: list[RecipientRole | None] = []
    for candidate in candidates:
        role = explicit_role(candidate)
        if role is None and include_workspace_members and candidate.member:
            role = RecipientRole.TO if candidate.administrator else None
        elif role is None:
            role = RecipientRole.BCC
        placed.append(role)
    default = RecipientRole.CC if RecipientRole.TO in placed else RecipientRole.TO
    return [role or default for role in placed]
