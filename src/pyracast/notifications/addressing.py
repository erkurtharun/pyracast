from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from pyracast.notifications.models import RecipientRole

VISIBLE_MODE_RELEASED = False


def sent_hidden(hide_recipients: bool) -> bool:
    """Whether a schedule stored with this mode is sent hidden, under the gate as it is now."""
    return hide_recipients or not VISIBLE_MODE_RELEASED


def members_choice(choices: Iterable[RecipientRole]) -> RecipientRole | None:
    found = set(choices)
    if RecipientRole.BCC in found or len(found) > 1:
        return RecipientRole.BCC
    return next(iter(found), None)


@dataclass(frozen=True)
class Candidate:
    """`member`: the address is the verified primary address of active members of the workspace."""

    address: str
    member: bool = False
    administrator: bool = False
    chosen: RecipientRole | None = None
    typed: RecipientRole | None = None
    counts: bool = True


def explicit_role(candidate: Candidate) -> RecipientRole | None:
    typed = candidate.typed if candidate.counts else None
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
