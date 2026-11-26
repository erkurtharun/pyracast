import re
from collections.abc import Callable, Iterator, Sequence
from typing import ClassVar, cast

import pytest
from allauth.account.models import EmailAddress
from django.contrib.auth.models import User
from django.core.mail import EmailMessage, EmailMultiAlternatives
from django.core.mail.backends.base import BaseEmailBackend
from django.db import connection, transaction
from django.test import override_settings
from django.urls import reverse
from django.utils import dateformat, timezone, translation
from django.utils.html import escape
from psycopg import sql

from pyracast.accounts.invitations import invite_workspace_member
from pyracast.accounts.models import (
    Membership,
    OutboundMail,
    Tenant,
    UserLanguagePreference,
    WorkspaceAdminChange,
    WorkspaceRole,
)
from pyracast.accounts.operations import reassign_workspace_admin
from pyracast.forecasts.validation import validate_binding
from pyracast.plants.approval import ReviewDecision, approve_region
from pyracast.plants.models import Plant, PlantSubmission
from pyracast.regions.models import AuditAction, AuditEvent, Region
from pyracast.regions.operations import submit_region
from tests.support.account_mail import TIME_FORMATS, TURKIYE
from tests.support.api import verified_admin
from tests.support.domain import FakeAdapter, System, make_system
from tests.support.notifications import ABSENT_HEADERS, links
from tests.support.outbox import deliver_mail

pytestmark = pytest.mark.django_db(transaction=True)

ORIGIN = "https://pyracast.example"
REASON = "The declared capacity does not match the grid licence."
ADMIN_ADDRESS = "plant-admin@example.com"


class Recorder(BaseEmailBackend):
    sent: ClassVar[list[EmailMessage]] = []

    def send_messages(self, email_messages: Sequence[EmailMessage] | None) -> int:
        assert email_messages is not None
        self.sent.extend(email_messages)
        return len(email_messages)


@pytest.fixture(autouse=True)
def recorder() -> Iterator[type[Recorder]]:
    Recorder.sent = []
    with override_settings(
        MAILERS={"default": {"BACKEND": "tests.test_review_mail.Recorder"}},
        PYRACAST_PUBLIC_ORIGIN=ORIGIN,
    ):
        yield Recorder


def addressed(user: User, address: str, language: str | None = None) -> User:
    user.email = address
    user.save(update_fields=["email"])
    EmailAddress.objects.create(user=user, email=address, verified=True, primary=True)
    if language is not None:
        UserLanguagePreference.objects.create(user=user, language=language)
    return user


def plant_system(*, language: str | None = None, approved: bool = False) -> System:
    system = make_system()
    addressed(system.member, ADMIN_ADDRESS, language)
    plant = Plant.objects.create(region=system.region, name="Kayseri solar park")
    PlantSubmission.objects.create(plant=plant, dc_capacity_mw=4.2, submitted_by=system.member)
    validate_binding(system.admin, system.binding.pk, FakeAdapter(), timezone.now())
    if approved:
        approve(system)
        OutboundMail.objects.all().delete()
    return system


def approve(system: System) -> bool:
    return approve_region(system.admin, system.region.pk, system.binding.pk, timezone.now()).changed


def decide(system: System, decision: str, reason: str = REASON) -> bool:
    from pyracast.plants.approval import decide_review

    return decide_review(system.admin, system.region.pk, decision, reason=reason)


def another_workspace(name: str, address: str, language: str | None = None) -> Region:
    tenant = Tenant.objects.create(name=f"{name} workspace")
    administrator = addressed(User.objects.create_user(username=address), address, language)
    Membership.objects.create(tenant=tenant, user=administrator, role=WorkspaceRole.ADMIN)
    return submit_region(
        administrator, tenant.pk, name=name, latitude=41, longitude=30, timezone="UTC"
    )


def reject(system: System, region: Region, reason: str = REASON) -> bool:
    from pyracast.plants.approval import decide_review

    return decide_review(system.admin, region.pk, ReviewDecision.REJECT, reason=reason)


TRIGGERS: dict[str, tuple[bool, Callable[[System], bool], str]] = {
    "approved": (False, lambda system: approve(system), AuditAction.REGION_APPROVED),
    "rejected": (
        False,
        lambda system: decide(system, ReviewDecision.REJECT),
        AuditAction.REGION_REJECTED,
    ),
    "suspended": (
        True,
        lambda system: decide(system, ReviewDecision.SUSPEND),
        AuditAction.REGION_SUSPENDED,
    ),
}


def intents() -> list[tuple[str, str, str, dict[str, str]]]:
    return list(
        OutboundMail.objects.order_by("pk").values_list("kind", "recipient", "language", "context")
    )


def tables_holding(text: str) -> list[str]:
    found = []
    with connection.cursor() as cursor:
        for table in connection.introspection.table_names():
            cursor.execute(
                sql.SQL(
                    "SELECT count(*) FROM {} AS stored"
                    " WHERE strpos(row_to_json(stored)::text, %s) > 0"
                ).format(sql.Identifier(table)),
                [text],
            )
            if cursor.fetchone() != (0,):
                found.append(table)
    return found


def test_an_invitation_writes_one_intent_naming_only_its_invitation() -> None:
    tenant = Tenant.objects.create(name="Solar workspace")
    administrator = User.objects.create_user(username="workspace-admin", email="admin@example.com")
    Membership.objects.create(tenant=tenant, user=administrator, role=WorkspaceRole.ADMIN)

    invitation = invite_workspace_member(administrator, tenant.pk, "guest@example.com")

    mail = OutboundMail.objects.get()
    assert (mail.kind, mail.recipient, mail.context, mail.invitation_id) == (
        "workspace_invitation",
        "guest@example.com",
        {},
        invitation.pk,
    )


@pytest.mark.parametrize("decision", sorted(TRIGGERS))
def test_a_review_decision_writes_one_intent_for_the_administrator_in_its_transaction(
    decision: str,
) -> None:
    approved, trigger, action = TRIGGERS[decision]
    system = plant_system(language="tr", approved=approved)

    with transaction.atomic():
        assert trigger(system) is True
        inside = intents()

    event = AuditEvent.objects.filter(action=action).latest("pk")
    assert (
        inside
        == intents()
        == [(f"plant_{decision}", ADMIN_ADDRESS, "tr", {"subject_id": str(event.pk)})]
    )


@pytest.mark.parametrize("decision", sorted(TRIGGERS))
def test_a_rolled_back_review_decision_leaves_no_intent(decision: str) -> None:
    approved, trigger, _ = TRIGGERS[decision]
    system = plant_system(approved=approved)

    with pytest.raises(RuntimeError):
        rolled_back(lambda: trigger(system))

    assert intents() == []


def rolled_back(action: Callable[[], object]) -> None:
    with transaction.atomic():
        action()
        raise RuntimeError


def test_a_repeated_decision_writes_no_second_intent() -> None:
    system = plant_system()

    decide(system, ReviewDecision.REJECT)
    decide(system, ReviewDecision.REJECT)

    assert [kind for kind, *_ in intents()] == ["plant_rejected"]


def test_review_mail_goes_only_to_an_active_administrator_with_a_verified_address() -> None:
    system = plant_system()
    viewer = addressed(User.objects.create_user(username="viewer"), "viewer@example.com")
    Membership.objects.create(tenant=system.tenant, user=viewer, role=WorkspaceRole.VIEWER)
    unverified = another_workspace("Unverified", "unverified@example.com")
    EmailAddress.objects.filter(email="unverified@example.com").update(verified=False)
    inactive = another_workspace("Inactive", "inactive@example.com")
    User.objects.filter(username="inactive@example.com").update(is_active=False)

    for region in (system.region, unverified, inactive):
        reject(system, region)

    assert [recipient for _, recipient, *_ in intents()] == [ADMIN_ADDRESS]


def test_the_language_follows_each_recipient() -> None:
    english = plant_system(language="en")
    turkish = another_workspace("Turkish", "tr-admin@example.com", "tr")
    default = another_workspace("Default", "default-admin@example.com")

    for region in (english.region, turkish, default):
        reject(english, region)

    assert [(recipient, language) for _, recipient, language, _ in intents()] == [
        (ADMIN_ADDRESS, "en"),
        ("tr-admin@example.com", "tr"),
        ("default-admin@example.com", "en"),
    ]


def test_the_rejection_mail_names_the_plant_reason_and_a_link_from_the_configured_origin(
    recorder: type[Recorder],
) -> None:
    system = plant_system()
    operator, _ = verified_admin()

    operator.post(
        reverse("admin:workspace_decision", args=(system.region.pk,)),
        {
            "expected_revision": system.region.revision,
            "expected_status": "pending",
            "action": "reject",
            "decision_reason": REASON,
        },
        HTTP_X_FORWARDED_HOST="attacker.example",
        HTTP_X_FORWARDED_PROTO="http",
    )
    assert deliver_mail() == 1

    (message,) = recorder.sent
    link = f"{ORIGIN}/app/w/{system.tenant.pk}/plants/{system.region.pk}/forecast/"
    assert message.to == [ADMIN_ADDRESS]
    assert message.subject == "Kayseri solar park was not approved"
    assert REASON in message.body
    assert link in message.body
    html = str(cast(EmailMultiAlternatives, message).alternatives[0][0])
    assert REASON in html
    assert link in html
    assert "attacker" not in message.body + html
    assert OutboundMail.objects.get().status == "sent"


@pytest.mark.parametrize(
    ("decision", "language", "subject", "sentence"),
    [
        ("approved", "en", "Kayseri solar park is approved", "approved"),
        ("approved", "tr", "Kayseri solar park onayland\u0131", "onaylad\u0131"),
        ("rejected", "en", "Kayseri solar park was not approved", "Reason"),
        ("rejected", "tr", "Kayseri solar park onaylanmad\u0131", "Gerekçe"),
        ("suspended", "en", "Kayseri solar park is suspended", "Reason"),
        ("suspended", "tr", "Kayseri solar park ask\u0131ya al\u0131nd\u0131", "Gerekçe"),
    ],
)
def test_the_decision_mail_is_rendered_in_the_recipient_language(
    recorder: type[Recorder], decision: str, language: str, subject: str, sentence: str
) -> None:
    approved, trigger, _ = TRIGGERS[decision]
    system = plant_system(language=language, approved=approved)
    trigger(system)

    assert deliver_mail() == 1

    (message,) = recorder.sent
    assert message.subject == subject
    assert sentence in message.body
    assert system.tenant.name in message.body


def test_no_reason_of_another_workspace_reaches_a_mail(recorder: type[Recorder]) -> None:
    first = plant_system()
    second = another_workspace("Second", "second-admin@example.com")

    reject(first, first.region, "First workspace reason")
    reject(first, second, "Second workspace reason")
    deliver_mail()

    bodies = {message.to[0]: str(message.body) for message in recorder.sent}
    assert "First workspace reason" in bodies[ADMIN_ADDRESS]
    assert "Second workspace reason" not in bodies[ADMIN_ADDRESS]
    assert "Second workspace reason" in bodies["second-admin@example.com"]
    assert "First workspace reason" not in bodies["second-admin@example.com"]


def test_no_outbox_row_holds_the_reason_a_link_or_another_address() -> None:
    system = plant_system()
    decide(system, ReviewDecision.REJECT)

    assert "notifications_outboundmail" not in tables_holding(REASON)
    assert "notifications_outboundmail" not in tables_holding("/app/")
    assert OutboundMail.objects.get().context.keys() == {"subject_id"}


@pytest.mark.parametrize("change", ["removed", "deactivated"])
def test_a_member_who_left_before_sending_gets_no_mail(
    recorder: type[Recorder], change: str
) -> None:
    system = plant_system()
    decide(system, ReviewDecision.REJECT)
    if change == "removed":
        Membership.objects.filter(user=system.member).delete()
    else:
        User.objects.filter(pk=system.member.pk).update(is_active=False)

    assert deliver_mail() == 0

    assert recorder.sent == []
    assert OutboundMail.objects.values_list("status", "last_error").get() == (
        "skipped",
        "recipient_unavailable",
    )


def test_a_decision_mail_for_a_plant_in_the_trash_is_not_sent(recorder: type[Recorder]) -> None:
    from pyracast.trash import trash_region

    system = plant_system()
    decide(system, ReviewDecision.REJECT)
    trash_region(system.member, system.tenant.pk, system.region.pk)

    assert deliver_mail() == 0
    assert OutboundMail.objects.values_list("status", "last_error").get() == (
        "skipped",
        "subject_unavailable",
    )


def role_system() -> tuple[Tenant, User, User, User]:
    platform = User.objects.create_user(username="platform", is_staff=True, is_superuser=True)
    tenant = Tenant.objects.create(name="Role workspace")
    former = addressed(User.objects.create_user(username="former"), "former@example.com", "tr")
    chosen = addressed(User.objects.create_user(username="chosen"), "chosen@example.com")
    Membership.objects.create(tenant=tenant, user=former, role=WorkspaceRole.ADMIN)
    Membership.objects.create(tenant=tenant, user=chosen, role=WorkspaceRole.VIEWER)
    return tenant, platform, former, chosen


def test_a_moved_administrator_role_mails_the_new_and_the_former_administrator(
    recorder: type[Recorder],
) -> None:
    tenant, platform, former, chosen = role_system()

    with transaction.atomic():
        assert reassign_workspace_admin(platform, tenant.pk, chosen.pk, expected_admin_id=former.pk)
        inside = intents()
    assert deliver_mail() == 2

    assert {(kind, recipient, language) for kind, recipient, language, _ in inside} == {
        ("workspace_admin_assigned", "chosen@example.com", "en"),
        ("workspace_admin_released", "former@example.com", "tr"),
    }
    messages = {message.to[0]: message for message in recorder.sent}
    assert f"{ORIGIN}/app/w/{tenant.pk}/members/" in str(messages["chosen@example.com"].body)
    assert f"{ORIGIN}/app/w/{tenant.pk}/" in str(messages["former@example.com"].body)
    assert "Role workspace" in str(messages["former@example.com"].body)
    assert "chosen@example.com" not in str(messages["former@example.com"].body)


def test_an_unchanged_or_rolled_back_role_change_writes_no_mail() -> None:
    tenant, platform, former, chosen = role_system()

    assert not reassign_workspace_admin(platform, tenant.pk, former.pk, expected_admin_id=None)
    with pytest.raises(RuntimeError):
        rolled_back(
            lambda: reassign_workspace_admin(
                platform, tenant.pk, chosen.pk, expected_admin_id=former.pk
            )
        )

    assert intents() == []


def test_a_role_moved_to_a_member_of_a_workspace_without_an_administrator_mails_only_them() -> None:
    platform = User.objects.create_user(username="platform", is_staff=True, is_superuser=True)
    tenant = Tenant.objects.create(name="Unadministered")
    chosen = addressed(User.objects.create_user(username="chosen"), "chosen@example.com")
    Membership.objects.create(tenant=tenant, user=chosen, role=WorkspaceRole.VIEWER)

    reassign_workspace_admin(platform, tenant.pk, chosen.pk, expected_admin_id=None)

    assert [(kind, recipient) for kind, recipient, *_ in intents()] == [
        ("workspace_admin_assigned", "chosen@example.com")
    ]


def test_each_plant_reaches_only_its_own_workspace() -> None:
    first = plant_system()
    other_tenant = Tenant.objects.create(name="Other")
    other_admin = addressed(User.objects.create_user(username="other"), "other@example.com")
    Membership.objects.create(tenant=other_tenant, user=other_admin, role=WorkspaceRole.ADMIN)
    submit_region(other_admin, other_tenant.pk, name="O", latitude=41, longitude=30, timezone="UTC")

    decide(first, ReviewDecision.REJECT)

    assert [recipient for _, recipient, *_ in intents()] == [ADMIN_ADDRESS]


def test_a_role_mail_whose_change_record_is_gone_is_not_sent(recorder: type[Recorder]) -> None:
    tenant, platform, former, chosen = role_system()
    reassign_workspace_admin(platform, tenant.pk, chosen.pk, expected_admin_id=former.pk)
    WorkspaceAdminChange.objects.all().delete()

    assert deliver_mail() == 0

    assert recorder.sent == []
    assert set(OutboundMail.objects.values_list("status", "last_error")) == {
        ("skipped", "subject_unavailable")
    }


DECISION_TEXT: dict[tuple[str, str], tuple[str, str, tuple[str, ...], str, str, str]] = {
    ("approved", "en"): (
        "Kayseri solar park is approved",
        "Pyracast approved the plant.",
        ("Plant: Kayseri solar park", "Workspace: Tenant", "Approved at: {time} (UTC+3)"),
        "",
        "Open plant",
        "You receive this email because of your role in Tenant.",
    ),
    ("approved", "tr"): (
        "Kayseri solar park onayland\u0131",
        "Pyracast santrali onaylad\u0131.",
        (
            "Santral: Kayseri solar park",
            "Çal\u0131şma alan\u0131: Tenant",
            "Onay zaman\u0131: {time} (UTC+3)",
        ),
        "",
        "Santrali aç",
        "Bu e-postay\u0131 Tenant çal\u0131şma alan\u0131ndaki rolünüz nedeniyle al\u0131yorsunuz.",
    ),
    ("rejected", "en"): (
        "Kayseri solar park was not approved",
        "Pyracast did not approve the plant.",
        (
            "Plant: Kayseri solar park",
            "Workspace: Tenant",
            "Decided at: {time} (UTC+3)",
            f"Reason: {REASON}",
        ),
        "Correct the details and send the plant for approval again.",
        "Open plant",
        "You receive this email because of your role in Tenant.",
    ),
    ("rejected", "tr"): (
        "Kayseri solar park onaylanmad\u0131",
        "Pyracast santrali onaylamad\u0131.",
        (
            "Santral: Kayseri solar park",
            "Çal\u0131şma alan\u0131: Tenant",
            "Karar zaman\u0131: {time} (UTC+3)",
            f"Gerekçe: {REASON}",
        ),
        "\u0130stenen bilgileri düzeltip santrali yeniden onaya gönderin.",
        "Santrali aç",
        "Bu e-postay\u0131 Tenant çal\u0131şma alan\u0131ndaki rolünüz nedeniyle al\u0131yorsunuz.",
    ),
    ("suspended", "en"): (
        "Kayseri solar park is suspended",
        "Pyracast suspended the plant. Forecasts and forecast emails for it are stopped.",
        (
            "Plant: Kayseri solar park",
            "Workspace: Tenant",
            "Suspended at: {time} (UTC+3)",
            f"Reason: {REASON}",
        ),
        "",
        "Open plant",
        "You receive this email because of your role in Tenant.",
    ),
    ("suspended", "tr"): (
        "Kayseri solar park ask\u0131ya al\u0131nd\u0131",
        "Pyracast santrali ask\u0131ya ald\u0131. "
        "Santralin tahminleri ve tahmin e-postalar\u0131 durduruldu.",
        (
            "Santral: Kayseri solar park",
            "Çal\u0131şma alan\u0131: Tenant",
            "Ask\u0131ya alma zaman\u0131: {time} (UTC+3)",
            f"Gerekçe: {REASON}",
        ),
        "",
        "Santrali aç",
        "Bu e-postay\u0131 Tenant çal\u0131şma alan\u0131ndaki rolünüz nedeniyle al\u0131yorsunuz.",
    ),
}


def assert_in_order(html: str, parts: Sequence[str]) -> None:
    position = 0
    for part in parts:
        found = html.find(part, position)
        assert found >= 0, part
        position = found + len(part)


@pytest.mark.parametrize(("decision", "language"), sorted(DECISION_TEXT))
def test_a_decision_mail_lists_the_plant_its_workspace_and_the_plant_time_in_both_parts(
    recorder: type[Recorder], decision: str, language: str
) -> None:
    approved, trigger, action = TRIGGERS[decision]
    system = plant_system(language=language, approved=approved)
    trigger(system)
    assert deliver_mail() == 1

    (message,) = recorder.sent
    event = AuditEvent.objects.filter(action=action).latest("pk")
    with translation.override(language):
        time = dateformat.format(event.created_at.astimezone(TURKIYE), TIME_FORMATS[language])
    heading, sentence, details, next_step, label, footer = DECISION_TEXT[(decision, language)]
    lines = [line.format(time=time) for line in details]
    link = f"{ORIGIN}/app/w/{system.tenant.pk}/plants/{system.region.pk}/forecast/"
    parts = [heading, sentence, "\n".join(lines), *([next_step] if next_step else [])]
    assert message.body == "\n\n".join(["Pyracast", *parts, f"{label}: {link}", footer])
    html = str(cast(EmailMultiAlternatives, message).alternatives[0][0])
    assert re.findall(r"<h1[^>]*>(.*?)</h1>", html) == [escape(heading)]
    shown = [escape(line).replace(": ", ":</strong> ", 1) for line in lines]
    assert_in_order(
        html,
        [
            f">{escape(heading)}</h1>",
            escape(sentence),
            *shown,
            *([escape(next_step)] if next_step else []),
            escape(label),
            escape(footer),
        ],
    )


def test_a_decision_mail_carries_no_list_header_and_links_the_plant_once_as_text(
    recorder: type[Recorder],
) -> None:
    system = plant_system()
    approve(system)
    assert deliver_mail() == 1

    (message,) = recorder.sent
    wire = message.message()
    assert [name for name in ABSENT_HEADERS if wire[name] is not None] == []
    html = str(cast(EmailMultiAlternatives, message).alternatives[0][0])
    plant = f"{ORIGIN}/app/w/{system.tenant.pk}/plants/{system.region.pk}/forecast/"
    assert links(html) == [plant]
    assert 'class="mail-button"' not in html


def test_the_role_mails_say_which_role_each_person_now_has(recorder: type[Recorder]) -> None:
    tenant, platform, former, chosen = role_system()
    reassign_workspace_admin(platform, tenant.pk, chosen.pk, expected_admin_id=former.pk)
    assert deliver_mail() == 2

    messages = {message.to[0]: message for message in recorder.sent}
    assert messages["chosen@example.com"].body == "\n\n".join(
        [
            "Pyracast",
            "You are now the workspace administrator of Role workspace",
            "Pyracast made you the workspace administrator and withdrew the pending invitations. "
            "Check the members and invite again where needed.",
            f"Open members: {ORIGIN}/app/w/{tenant.pk}/members/",
            "You receive this email because of your role in Role workspace.",
        ]
    )
    assert messages["former@example.com"].body == "\n\n".join(
        [
            "Pyracast",
            "Art\u0131k Role workspace çal\u0131şma alan\u0131n\u0131n yöneticisi değilsiniz",
            "Pyracast çal\u0131şma alan\u0131 yöneticisi rolünü başka bir üyeye devretti. "
            "Art\u0131k çal\u0131şma alan\u0131nda üyesiniz.",
            f"Çal\u0131şma alan\u0131n\u0131 aç: {ORIGIN}/app/w/{tenant.pk}/",
            "Bu e-postay\u0131 Role workspace "
            "çal\u0131şma alan\u0131ndaki rolünüz nedeniyle al\u0131yorsunuz.",
        ]
    )
