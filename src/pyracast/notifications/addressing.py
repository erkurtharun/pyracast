from collections.abc import Sequence
from dataclasses import dataclass

from pyracast.notifications.models import RecipientRole


@dataclass(frozen=True)
class Candidate:
    """One address of a reminder's list. `member` and `administrator` describe the active member
    whose verified primary address it is; `chosen` is that member's stored choice and `typed`
    the choice of the typed row that holds the address, which counts only while `settled`."""

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
