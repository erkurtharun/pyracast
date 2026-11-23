import pytest

from pyracast.notifications.addressing import Candidate, explicit_role, members_choice, roles
from pyracast.notifications.models import RecipientRole
from pyracast.notifications.recipients import mailbox_key

TO, CC, BCC = RecipientRole.TO, RecipientRole.CC, RecipientRole.BCC
ADMIN = Candidate("admin@example.com", member=True, administrator=True)
ALICE = Candidate("alice@example.com", member=True)
BOB = Candidate("bob@example.com", member=True)
OUTSIDE = Candidate("outside@example.com")


def placed(
    *candidates: Candidate, hidden: bool = False, include: bool = True
) -> list[RecipientRole]:
    return roles(candidates, hide_recipients=hidden, include_workspace_members=include)


@pytest.mark.parametrize(
    ("candidate", "role"),
    [
        (Candidate("a@example.com", chosen=TO, typed=BCC), BCC),
        (Candidate("a@example.com", chosen=BCC, typed=TO), BCC),
        (Candidate("a@example.com", chosen=CC, typed=TO), CC),
        (Candidate("a@example.com", chosen=TO, typed=CC), TO),
        (Candidate("a@example.com", typed=CC), CC),
        (Candidate("a@example.com", typed=TO, counts=False), None),
        (Candidate("a@example.com", typed=BCC, counts=False), BCC),
        (Candidate("a@example.com", chosen=CC, typed=TO, counts=False), CC),
        (Candidate("a@example.com", chosen=TO, typed=BCC, counts=False), BCC),
        (Candidate("a@example.com", member=True), None),
    ],
)
def test_hidden_wins_then_the_member_choice_then_a_typed_choice_that_counts(
    candidate: Candidate, role: RecipientRole | None
) -> None:
    assert explicit_role(candidate) == role


def test_the_hidden_mode_hides_every_address_whatever_was_chosen() -> None:
    assert placed(
        ADMIN,
        Candidate("a@example.com", chosen=TO),
        Candidate("b@example.com", typed=CC),
        BOB,
        hidden=True,
    ) == [BCC, BCC, BCC, BCC]


def test_by_default_the_administrator_is_in_to_members_in_copy_and_other_addresses_hidden() -> None:
    assert placed(OUTSIDE, ADMIN, ALICE, BOB) == [BCC, TO, CC, CC]


def test_default_members_move_to_to_while_nobody_is_in_to() -> None:
    assert placed(OUTSIDE, ALICE, BOB) == [BCC, TO, TO]


def test_an_explicit_to_keeps_default_members_in_copy() -> None:
    assert placed(Candidate("boss@example.com", typed=TO), ALICE) == [TO, CC]


def test_a_chosen_copy_stays_copy_with_nobody_in_to() -> None:
    assert placed(Candidate("admin@example.com", member=True, administrator=True, chosen=CC)) == [
        CC
    ]
    assert placed(Candidate("a@example.com", typed=CC), Candidate("b@example.com", chosen=CC)) == [
        CC,
        CC,
    ]


def test_a_chosen_copy_of_the_administrator_puts_default_members_in_to() -> None:
    chosen_copy = Candidate("admin@example.com", member=True, administrator=True, chosen=CC)
    assert placed(chosen_copy, ALICE, OUTSIDE) == [CC, TO, BCC]


def test_without_inclusion_a_member_address_follows_only_choices() -> None:
    assert placed(
        ADMIN, ALICE, Candidate("bob@example.com", member=True, chosen=CC), include=False
    ) == [
        BCC,
        BCC,
        CC,
    ]


def test_a_resting_typed_to_falls_back_to_the_default() -> None:
    gave_up = Candidate("left@example.com", typed=TO, counts=False)
    taken_over = Candidate("left@example.com", member=True, typed=TO, counts=False)
    assert placed(gave_up, ADMIN) == [BCC, TO]
    assert placed(taken_over, ADMIN) == [CC, TO]


def test_a_member_hidden_by_choice_stays_hidden_under_a_typed_to() -> None:
    assert placed(Candidate("alice@example.com", member=True, chosen=BCC, typed=TO), ADMIN) == [
        BCC,
        TO,
    ]


def test_a_typed_to_of_a_hidden_member_shows_only_while_the_address_is_not_the_members() -> None:
    unverified = Candidate("alice@example.com", typed=TO)
    verified = Candidate("alice@example.com", member=True, chosen=BCC, typed=TO)
    assert placed(unverified, ADMIN) == [TO, TO]
    assert placed(verified, ADMIN) == [BCC, TO]


def test_an_address_no_step_attributes_is_hidden() -> None:
    assert placed(OUTSIDE) == [BCC]
    assert placed(Candidate("outside@example.com", typed=TO, counts=False)) == [BCC]


def test_an_empty_list_places_nobody() -> None:
    assert placed() == []


@pytest.mark.parametrize(
    ("choices", "combined"),
    [
        ((), None),
        ((CC,), CC),
        ((TO, TO), TO),
        ((TO, CC), BCC),
        ((CC, BCC), BCC),
        ((TO, BCC, TO), BCC),
    ],
)
def test_the_members_of_an_address_choose_hidden_if_any_does_or_if_they_disagree(
    choices: tuple[RecipientRole, ...], combined: RecipientRole | None
) -> None:
    assert members_choice(choices) == combined


UNICODE_MAILBOX = "ops@b\u00fccher.de"
SPELLINGS = [
    "ops@xn--bcher-kva.de",
    "OPS@XN--BCHER-KVA.DE",
    "ops@Xn--Bcher-Kva.De",
    "ops@xn--bcher-kva\u3002de",
    "ops@b\u00fccher\uff0ede",
    "ops@b\u00fccher\uff61de",
    "\uff4f\uff50\uff53@\uff42\u00fc\uff43\uff48\uff45\uff52.\uff44\uff45",
    "ops@bu\u0308cher.de",
    " Ops@B\u00dcCHER.de ",
]
UNDECODABLE = ["xn--99999999999999999999", "xn--abc-", "xn--xn--abc-", "xn--", "xn--a\u00fc"]


@pytest.mark.parametrize("spelling", SPELLINGS)
def test_every_spelling_of_one_mailbox_has_one_key(spelling: str) -> None:
    assert mailbox_key(spelling) == mailbox_key(UNICODE_MAILBOX) == UNICODE_MAILBOX


def test_the_local_part_is_only_normalised_and_folded() -> None:
    assert mailbox_key("Stra\u00dfe@example.com") == "strasse@example.com"
    assert mailbox_key("xn--bcher-kva@example.com") == "xn--bcher-kva@example.com"


@pytest.mark.parametrize("label", UNDECODABLE)
def test_a_label_that_does_not_decode_stays_as_it_is(label: str) -> None:
    assert mailbox_key(f"ops@{label}.de") == f"ops@{label}.de"


@pytest.mark.parametrize(
    "address", [*SPELLINGS, *(f"ops@{label}.de" for label in UNDECODABLE), "ops@xn--\u00df.de"]
)
def test_a_mailbox_key_is_its_own_key(address: str) -> None:
    assert mailbox_key(mailbox_key(address)) == mailbox_key(address)
