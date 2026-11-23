import io
import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, timedelta
from functools import reduce
from operator import or_
from typing import Any
from unittest.mock import MagicMock
from uuid import uuid4

import pytest
from allauth.account.internal.flows.password_reset_by_code import (
    PASSWORD_RESET_VERIFICATION_SESSION_KEY,
)
from allauth.account.internal.stagekit import LOGIN_SESSION_KEY
from allauth.account.models import EmailAddress, EmailConfirmation
from allauth.mfa.models import Authenticator
from allauth.socialaccount.models import SocialAccount, SocialToken
from allauth.usersessions.models import UserSession
from django.apps import apps
from django.contrib.admin import site
from django.contrib.admin.models import CHANGE, LogEntry
from django.contrib.auth.models import Group, User
from django.contrib.contenttypes.models import ContentType
from django.contrib.sessions.backends.db import SessionStore
from django.contrib.sessions.models import Session
from django.core import mail
from django.core.management import CommandError, call_command
from django.db import OperationalError, models
from django.db.models.functions import Cast
from django.test import Client, override_settings
from django.urls import reverse
from django.utils import timezone
from django.utils.http import int_to_base36

from pyracast import erasure
from pyracast.accounts import retention as account_retention
from pyracast.accounts.authorization import is_platform_admin
from pyracast.accounts.invitations import invite_workspace_member
from pyracast.accounts.models import (
    AuthenticationCacheEntry,
    Membership,
    OutboundMail,
    OutboundMailKind,
    OutboundMailStatus,
    Tenant,
    UserLanguagePreference,
    WorkspaceInvitation,
    WorkspaceMemberRemoval,
    WorkspaceRole,
)
from pyracast.accounts.operations import reassign_workspace_admin
from pyracast.accounts.processes import PROVIDER_SIGNUP_SESSION_KEY
from pyracast.change_requests.models import ChangeRequest
from pyracast.change_requests.operations import submit_production
from pyracast.connections import operations as provider_operations
from pyracast.connections.models import ProviderAccount, SiteBinding
from pyracast.erasure import AdministersWorkspaceError, erase_account
from pyracast.errors import DomainError, ErrorCode
from pyracast.forecasts.models import RequestAttempt
from pyracast.forecasts.revalidation import RevalidationOutcome, revalidate_account
from pyracast.forecasts.validation import validate_binding
from pyracast.incidents.models import IncidentRevision, PlantIncident
from pyracast.notifications import outbox
from pyracast.notifications.models import (
    DeliveryRecipient,
    NotificationDelivery,
    ReminderRecipient,
    ReminderRecipientTie,
    ReminderSchedule,
)
from pyracast.notifications.operations import configure_reminders
from pyracast.notifications.outbox import send_outbound_mail
from pyracast.notifications.preparation import prepare_delivery
from pyracast.notifications.recipients import recipient_key
from pyracast.notifications.scheduler import enqueue_due
from pyracast.orchestration import dispatcher
from pyracast.plants.approval import approve_region
from pyracast.plants.models import Plant, PlantSubmission
from pyracast.plants.production_changes import ProductionChange
from pyracast.regions.audit import record_audit
from pyracast.regions.models import AuditAction, AuditEvent, Region, RegionLocationChangeRequest
from pyracast.regions.operations import configure_binding
from tests.support.account_sessions import capture_security_events
from tests.support.api import verified_admin
from tests.support.auth_lifecycle import submit
from tests.support.company_registration import signup
from tests.support.concurrency import (
    behind_open_transaction,
    committed_elsewhere,
    paused_in,
    row_locked_elsewhere,
    short_lock_timeout,
)
from tests.support.domain import NOW, FakeAdapter, System, make_system, provider_key
from tests.support.notifications import SLOT, saved
from tests.support.plant_operations import make_plant
from tests.support.social_accounts import PROVIDERS, begin, complete, serve_provider_profile
from tests.support.workspaces import administered_workspace, signed_in

pytestmark = pytest.mark.django_db(transaction=True)

PRIMARY = "Aylin.Quillfeather@example.com"
SECONDARY = "aylin.q@work-example.net"
# Earlier addresses of the person: changed away from and removed (both still named by the
# outbox), and one that only a reminder entry tied to the person's membership still holds.
EARLIER = "aylin.before@example.org"
REMOVED = "aylin.removed@example.org"
TIED = "aylin.tied@example.org"
FIRST_NAME = "Aylinzq"
LAST_NAME = "Quillfeather"
USERNAME = "aylinzq-person"
COLLEAGUE = "colleague@example.com"
KEPT_ADDRESS = "kept@example.com"
PERSONAL = (PRIMARY, SECONDARY, EARLIER, REMOVED, TIED, FIRST_NAME, LAST_NAME, USERNAME)
PROVIDER_UID = "person-google-subject"
LOCMEM = {"default": {"BACKEND": "django.core.mail.backends.locmem.EmailBackend"}}
HISTORY = "business history keeps pointing at the anonymized account"
# Relations erasure empties: the rows hold the person's identity, credentials or addresses.
ERASED = frozenset(
    {
        "auth.User_groups.user",
        "auth.User_user_permissions.user",
        "account.EmailAddress.user",
        "socialaccount.SocialAccount.user",
        "mfa.Authenticator.user",
        "usersessions.UserSession.user",
        "accounts.Membership.user",
        "accounts.UserLanguagePreference.user",
        "accounts.WorkspaceInvitation.accepted_by",
    }
)
KEPT = {
    "admin.LogEntry.user": "a console change names the changed record, never the account",
    "accounts.WorkspaceInvitation.created_by": "a sent invitation names its invitee; "
    "it expires with the invitation retention",
    "accounts.WorkspaceAdminChange.actor": HISTORY,
    "accounts.WorkspaceAdminChange.administrator": HISTORY,
    "accounts.WorkspaceAdminChange.previous_administrator": HISTORY,
    "accounts.WorkspaceMemberRemoval.actor": HISTORY,
    "accounts.WorkspaceMemberRemoval.user": HISTORY,
    "regions.RegionLocationChangeRequest.requested_by": HISTORY,
    "regions.RegionLocationChangeRequest.reviewed_by": HISTORY,
    "regions.AuditEvent.actor": HISTORY,
    "connections.ProviderAccount.authorized_by": HISTORY,
    "forecasts.HistoricalReferenceImport.imported_by": HISTORY,
    "forecasts.ReferenceCapacityNormalization.authorized_by": HISTORY,
    "plants.Plant.automatic_calibration_enabled_by": HISTORY,
    "plants.PlantSubmission.submitted_by": HISTORY,
    "plants.PlantConfiguration.created_by": HISTORY,
    "plants.ProductionObservation.recorded_by": HISTORY,
    "plants.CalibrationRun.requested_by": HISTORY,
    "plants.CalibrationActivation.actor": HISTORY,
    "plants.PlantEvent.actor": HISTORY,
    "incidents.PlantIncident.created_by": HISTORY,
    "incidents.PlantIncident.updated_by": HISTORY,
    "incidents.IncidentRevision.actor": HISTORY,
    "notifications.ReminderSchedule.configured_by": HISTORY,
    "metering.EpiasConnection.connected_by": HISTORY,
    "change_requests.ChangeRequest.proposed_by": HISTORY,
    "change_requests.ChangeRequest.reviewed_by": HISTORY,
}


def relations_to_accounts() -> dict[str, tuple[type[models.Model], str]]:
    return {
        f"{model._meta.label}.{field.name}": (model, field.name)
        for model in apps.get_models(include_auto_created=True)
        for field in model._meta.get_fields(include_hidden=True)
        if field.is_relation and field.concrete and field.related_model is User
    }


def pointing_at(user: User) -> dict[str, int]:
    return {
        relation: model._base_manager.filter(**{field: user.pk}).count()
        for relation, (model, field) in relations_to_accounts().items()
    }


def decoded_session(data: str) -> dict[str, Any]:
    return dict(SessionStore().decode(data))


def rows_holding(values: tuple[str, ...]) -> list[str]:
    found = []
    for model in apps.get_models(include_auto_created=True):
        for field in model._meta.concrete_fields:
            if not isinstance(field, models.CharField | models.TextField | models.JSONField):
                continue
            text = Cast(field.attname, models.TextField())
            matches = reduce(or_, (models.Q(text__icontains=value) for value in values))
            if model._base_manager.annotate(text=text).filter(matches).exists():
                found.append(f"{model._meta.db_table}.{field.column}")
    # Session data is signed and encoded; its decoded content is what a reader recovers.
    if any(
        value.casefold() in json.dumps(decoded_session(data), ensure_ascii=False).casefold()
        for data in Session.objects.values_list("session_data", flat=True)
        for value in values
    ):
        found.append("django_session.session_data (decoded)")
    return sorted(found)


def database() -> dict[str, list[tuple[Any, ...]]]:
    return {
        model._meta.label: list(model._base_manager.order_by("pk").values_list())
        for model in apps.get_models(include_auto_created=True)
    }


def session(key: str, data: dict[str, Any]) -> None:
    Session.objects.create(
        session_key=key,
        session_data=SessionStore().encode(data),
        expire_date=timezone.now() + timedelta(hours=1),
    )


def outbound(
    kind: OutboundMailKind, recipient: str, context: dict[str, str], *, finished: bool
) -> OutboundMail:
    return OutboundMail.objects.create(
        kind=kind,
        recipient=recipient,
        language="en",
        context=context,
        created_at=NOW - timedelta(days=1),
        available_at=NOW - timedelta(days=1),
        expires_at=NOW + timedelta(hours=1),
        status=OutboundMailStatus.SENT if finished else OutboundMailStatus.PENDING,
        finished_at=NOW - timedelta(hours=1) if finished else None,
    )


def person_with(*addresses: str, username: str = USERNAME) -> User:
    person = User.objects.create_user(
        username=username, email=addresses[0], first_name=FIRST_NAME, last_name=LAST_NAME
    )
    for index, address in enumerate(addresses):
        EmailAddress.objects.create(user=person, email=address, primary=not index, verified=True)
    return person


@dataclass(frozen=True)
class Seeded:
    system: System
    plant: Plant
    person: User
    successor: User
    other: Tenant
    schedule: ReminderSchedule
    other_schedule: ReminderSchedule
    change_request: ChangeRequest
    bystanders: dict[str, set[Any]]


def seeded(monkeypatch: pytest.MonkeyPatch) -> Seeded:
    system, plant = make_plant(monkeypatch)
    person = system.member
    User.objects.filter(pk=person.pk).update(
        username=USERNAME, email=PRIMARY, first_name=FIRST_NAME, last_name=LAST_NAME
    )
    person.refresh_from_db()
    person.set_password("Person-passw0rd-2026")
    person.save()
    primary = EmailAddress.objects.create(user=person, email=PRIMARY, primary=True, verified=True)
    EmailAddress.objects.create(user=person, email=SECONDARY, primary=False, verified=True)
    EmailConfirmation.objects.create(email_address=primary, key="person-confirmation", created=NOW)
    identity = SocialAccount.objects.create(
        user=person,
        provider="google",
        uid=PROVIDER_UID,
        extra_data={"email": PRIMARY, "name": f"{FIRST_NAME} {LAST_NAME}"},
    )
    SocialToken.objects.create(account=identity, token=uuid4().hex)
    Authenticator.objects.create(
        user=person, type=Authenticator.Type.TOTP, data={"secret": "person-totp-secret"}
    )
    UserLanguagePreference.objects.create(user=person, language="tr")
    person.groups.add(Group.objects.create(name="operators"))
    session("person-session", {"_auth_user_id": str(person.pk), "noted": PRIMARY})
    UserSession.objects.create(
        user=person,
        session_key="person-session",
        ip="203.0.113.7",
        user_agent=f"Browser of {FIRST_NAME}",
    )
    # Sessions without a device row: pending flows of the person and of a bystander.
    session("person-unbound", {"_auth_user_id": str(person.pk)})
    session("person-reset", {PASSWORD_RESET_VERIFICATION_SESSION_KEY: {"user_id": str(person.pk)}})
    session("person-login", {LOGIN_SESSION_KEY: {"user_pk": int_to_base36(person.pk)}})
    session(
        "person-provider-signup",
        {
            PROVIDER_SIGNUP_SESSION_KEY: {
                "account": {"provider": "microsoft", "extra_data": {}},
                "user": {"email": "", "first_name": "", "last_name": ""},
                "email_addresses": [{"email": EARLIER.upper(), "verified": False}],
            }
        },
    )
    session(
        "person-named-signup",
        {PROVIDER_SIGNUP_SESSION_KEY: {"user": {"first_name": FIRST_NAME, "last_name": LAST_NAME}}},
    )
    schedule = configure_reminders(
        person,
        system.tenant.pk,
        system.region.pk,
        language="en",
        enabled=True,
        recipients=[PRIMARY, COLLEAGUE],
        times=["10:00"],
        now=NOW,
    )
    saved(system)
    (delivery_id,) = enqueue_due(SLOT)
    assert prepare_delivery(NotificationDelivery.objects.get(pk=delivery_id), NOW)
    successor = User.objects.create(username="successor", email="successor@example.com")
    Membership.objects.create(tenant=system.tenant, user=successor)
    reassign_workspace_admin(
        system.admin, system.tenant.pk, successor.pk, expected_admin_id=person.pk
    )
    membership = Membership.objects.get(tenant=system.tenant, user=person)
    tied = ReminderRecipient.objects.create(
        schedule=schedule, address=TIED, key=recipient_key(TIED)
    )
    ReminderRecipientTie.objects.create(recipient=tied, membership_id=membership.pk)
    assert plant.current_configuration_id is not None
    change_request = submit_production(
        person,
        system.tenant.pk,
        system.region.pk,
        plant.pk,
        configuration_id=plant.current_configuration_id,
        unit="kWh",
        changes=[ProductionChange(NOW - timedelta(hours=1), NOW, None, "set", 1234.5)],
        reason="Verified meter reading",
        idempotency_key=uuid4(),
    )
    RegionLocationChangeRequest.objects.create(
        region=system.region,
        requested_by=person,
        base_revision=Region.objects.get(pk=system.region.pk).revision,
        latitude=41,
        longitude=29,
        timezone="Europe/Istanbul",
    )
    PlantSubmission.objects.create(plant=plant, dc_capacity_mw=130, submitted_by=person)
    incident = PlantIncident.objects.create(
        plant=plant,
        declared_configuration_id=plant.current_configuration_id,
        kind="inverter",
        title="Inverter maintenance",
        starts_at=NOW - timedelta(hours=2),
        created_by=person,
        updated_by=person,
    )
    IncidentRevision.objects.create(
        incident=incident,
        revision=1,
        snapshot={
            "kind": incident.kind,
            "title": incident.title,
            "notes": "",
            "starts_at": incident.starts_at.isoformat(),
            "ends_at": None,
            "available_dc_mw": None,
            "available_inverter_mw": None,
            "available_export_mw": None,
            "declared_configuration_id": plant.current_configuration_id,
            "cancelled_at": None,
        },
        actor=person,
    )
    inviter = User.objects.create(username="inviter", email="inviter@example.com")
    other = Tenant.objects.create(name="Other workspace")
    Membership.objects.create(tenant=other, user=inviter, role=WorkspaceRole.ADMIN)
    WorkspaceMemberRemoval.objects.create(tenant=other, actor=inviter, user=person)
    LogEntry.objects.create(
        user=person,
        content_type=ContentType.objects.get_for_model(Tenant),
        object_id=str(other.pk),
        object_repr=other.name,
        action_flag=CHANGE,
    )
    invite_workspace_member(inviter, other.pk, SECONDARY)
    joined = Tenant.objects.create(name="Joined workspace")
    Membership.objects.create(tenant=joined, user=person)
    invitations = (
        WorkspaceInvitation.objects.create(
            tenant=joined,
            email=PRIMARY.lower(),
            created_by=inviter,
            expires_at=NOW + timedelta(days=7),
            accepted_at=NOW,
            accepted_by=person,
        ),
        WorkspaceInvitation.objects.create(
            tenant=other, email=TIED, created_by=inviter, expires_at=NOW + timedelta(days=7)
        ),
    )
    assert all(invitation.pk for invitation in invitations)
    colleague_invitation = WorkspaceInvitation.objects.create(
        tenant=other, email=COLLEAGUE, created_by=inviter, expires_at=NOW + timedelta(days=7)
    )
    other_region = Region.objects.create(
        tenant=other, name="Other site", latitude=38, longitude=27, timezone="Europe/Istanbul"
    )
    other_schedule = ReminderSchedule.objects.create(region=other_region, effective_from=NOW)
    for address in (SECONDARY.upper(), KEPT_ADDRESS, REMOVED):
        ReminderRecipient.objects.create(
            schedule=other_schedule, address=address, key=recipient_key(address)
        )
    outbound(OutboundMailKind.PASSWORD_CHANGED, PRIMARY, {"to_email": PRIMARY}, finished=False)
    outbound(
        OutboundMailKind.EMAIL_CHANGED,
        EARLIER,
        {"from_email": EARLIER, "to_email": PRIMARY},
        finished=True,
    )
    outbound(OutboundMailKind.EMAIL_DELETED, PRIMARY, {"deleted_email": REMOVED}, finished=True)
    outbound(OutboundMailKind.PASSWORD_RESET, REMOVED, {}, finished=True)
    colleague_mail = outbound(
        OutboundMailKind.PASSWORD_CHANGED, COLLEAGUE, {"to_email": COLLEAGUE}, finished=False
    )
    AuthenticationCacheEntry.objects.create(
        cache_key="provider-login:person",
        value={"email": PRIMARY, "name": LAST_NAME},
        expires=NOW + timedelta(minutes=10),
    )
    AuthenticationCacheEntry.objects.create(
        cache_key="provider-login:earlier",
        value={"account": {"extra_data": {"email": EARLIER.upper()}}},
        expires=NOW + timedelta(minutes=10),
    )
    AuthenticationCacheEntry.objects.create(
        cache_key="provider-login:colleague",
        value={"email": COLLEAGUE},
        expires=NOW + timedelta(minutes=10),
    )
    session(
        "colleague-signup",
        {PROVIDER_SIGNUP_SESSION_KEY: {"user": {"first_name": "Colleague", "email": COLLEAGUE}}},
    )
    session("successor-unbound", {"_auth_user_id": str(successor.pk)})
    bystanders: dict[str, set[Any]] = {
        "sessions": {"colleague-signup", "successor-unbound"},
        "mail": {colleague_mail.pk},
        "cache": {"provider-login:colleague"},
        "invitations": {colleague_invitation.pk},
    }
    return Seeded(
        system,
        plant,
        person,
        successor,
        other,
        schedule,
        other_schedule,
        change_request,
        bystanders,
    )


def bystanders_left() -> dict[str, set[Any]]:
    return {
        "sessions": set(Session.objects.values_list("session_key", flat=True)),
        "mail": set(OutboundMail.objects.values_list("pk", flat=True)),
        "cache": set(AuthenticationCacheEntry.objects.values_list("cache_key", flat=True)),
        "invitations": set(WorkspaceInvitation.objects.values_list("pk", flat=True)),
    }


def test_every_relation_to_an_account_is_erased_or_kept_for_a_stated_reason() -> None:
    assert set(relations_to_accounts()) == ERASED | KEPT.keys()
    assert not ERASED & KEPT.keys()


def test_erasure_leaves_no_personal_data_and_keeps_the_history_that_points_at_the_account(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seed = seeded(monkeypatch)
    person = seed.person
    before = pointing_at(person)
    assert rows_holding(PERSONAL) == [
        "account_emailaddress.email",
        "accounts_workspaceinvitation.email",
        "auth_user.email",
        "auth_user.first_name",
        "auth_user.last_name",
        "auth_user.username",
        "django_session.session_data (decoded)",
        "notifications_deliveryrecipient.address",
        "notifications_deliveryrecipient.key",
        "notifications_outboundmail.context",
        "notifications_outboundmail.recipient",
        "notifications_reminderrecipient.address",
        "notifications_reminderrecipient.key",
        "pyracast_authentication_cache.value",
        "socialaccount_socialaccount.extra_data",
        "usersessions_usersession.user_agent",
    ]
    revisions = dict(ReminderSchedule.objects.values_list("pk", "revision"))

    erasure_result = erase_account(person.pk, execute=True)

    assert rows_holding(PERSONAL) == []
    assert bystanders_left() == seed.bystanders
    after = pointing_at(person)
    assert {relation: after[relation] for relation in ERASED} == dict.fromkeys(ERASED, 0)
    assert {relation: after[relation] for relation in KEPT} == {
        relation: before[relation] for relation in KEPT
    }
    assert {relation for relation in KEPT if before[relation]} == {
        "accounts.WorkspaceAdminChange.previous_administrator",
        "accounts.WorkspaceMemberRemoval.user",
        "admin.LogEntry.user",
        "change_requests.ChangeRequest.proposed_by",
        "incidents.IncidentRevision.actor",
        "incidents.PlantIncident.created_by",
        "incidents.PlantIncident.updated_by",
        "notifications.ReminderSchedule.configured_by",
        "plants.PlantConfiguration.created_by",
        "plants.PlantEvent.actor",
        "plants.PlantSubmission.submitted_by",
        "regions.AuditEvent.actor",
        "regions.RegionLocationChangeRequest.requested_by",
    }
    person.refresh_from_db()
    assert (person.username, person.email, person.first_name, person.last_name) == (
        f"erased-{person.pk}",
        "",
        "",
        "",
    )
    assert not person.has_usable_password()
    assert (person.is_active, person.is_staff, person.is_superuser, person.last_login) == (
        False,
        False,
        False,
        None,
    )
    assert not EmailConfirmation.objects.exists()
    assert not SocialToken.objects.exists()
    assert NotificationDelivery.objects.get().prepared_message is not None
    assert sorted(ReminderRecipient.objects.values_list("address", flat=True)) == [
        COLLEAGUE,
        KEPT_ADDRESS,
    ]
    assert list(DeliveryRecipient.objects.values_list("address", flat=True)) == [COLLEAGUE]
    assert dict(ReminderSchedule.objects.values_list("pk", "revision")) == {
        pk: revision + 1 for pk, revision in revisions.items()
    }
    assert Membership.objects.filter(tenant=seed.system.tenant).get().user == seed.successor
    assert erasure_result.account_id == person.pk
    assert erasure_result.changes == {
        "account.EmailAddress": 2,
        "account.EmailConfirmation": 1,
        "accounts.AuthenticationCacheEntry": 2,
        "accounts.Membership": 2,
        "accounts.UserLanguagePreference": 1,
        "accounts.WorkspaceInvitation": 3,
        "auth.User": 1,
        "auth.User_groups": 1,
        "mfa.Authenticator": 1,
        "notifications.DeliveryRecipient": 1,
        "notifications.OutboundMail": 6,
        "notifications.ReminderRecipient": 4,
        "notifications.ReminderRecipientTie": 2,
        "notifications.ReminderSchedule": 2,
        "sessions.Session": 6,
        "socialaccount.SocialAccount": 1,
        "socialaccount.SocialToken": 1,
        "usersessions.UserSession": 1,
    }


def test_a_dry_run_reports_what_erasure_would_change_and_changes_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seed = seeded(monkeypatch)
    before = database()

    dry_run = erase_account(seed.person.pk, execute=False)

    assert database() == before
    assert dry_run.changes == erase_account(seed.person.pk, execute=True).changes


def test_erasure_deletes_only_mail_and_cache_entries_that_hold_the_exact_address() -> None:
    person = person_with("A@example.com")
    bystanders = ("aa@example.com", "ba@example.com", "a@example.com.tr")
    kept_mail, kept_entries = set(), set()
    for index, address in enumerate(bystanders):
        kept_mail |= {
            outbound(
                OutboundMailKind.PASSWORD_CHANGED, address, {"to_email": address}, finished=False
            ).pk,
            outbound(
                OutboundMailKind.EMAIL_CHANGED,
                address,
                {"from_email": address, "to_email": f"next{index}@example.com"},
                finished=True,
            ).pk,
            outbound(
                OutboundMailKind.EMAIL_DELETED,
                f"next{index}@example.com",
                {"deleted_email": address},
                finished=True,
            ).pk,
        }
        for key, value in (
            (f"flat-{index}", {"email": address}),
            (f"nested-{index}", {"user": {"emails": [address.upper()]}}),
        ):
            AuthenticationCacheEntry.objects.create(cache_key=key, value=value, expires=None)
            kept_entries.add(key)
    outbound(OutboundMailKind.PASSWORD_CHANGED, "a@example.com", {}, finished=False)
    outbound(
        OutboundMailKind.EMAIL_DELETED,
        "a@example.com",
        {"deleted_email": "a.old@example.com"},
        finished=True,
    )
    for key, value in (
        ("own", {"email": "a@example.com"}),
        ("own-nested", {"user": {"emails": ["A@EXAMPLE.COM"]}}),
    ):
        AuthenticationCacheEntry.objects.create(cache_key=key, value=value, expires=None)

    changes = erase_account(person.pk, execute=True).changes

    assert set(OutboundMail.objects.values_list("pk", flat=True)) == kept_mail
    assert set(AuthenticationCacheEntry.objects.values_list("cache_key", flat=True)) == (
        kept_entries
    )
    assert (
        changes["notifications.OutboundMail"],
        changes["accounts.AuthenticationCacheEntry"],
    ) == (2, 2)


def test_erasure_follows_the_earlier_addresses_of_the_account_everywhere() -> None:
    person = person_with("now@example.com")
    tenant, inviter = administered_workspace()
    membership = Membership.objects.create(tenant=tenant, user=person)
    region = Region.objects.create(
        tenant=tenant, name="Site", latitude=38, longitude=27, timezone="Europe/Istanbul"
    )
    schedule = ReminderSchedule.objects.create(region=region, effective_from=NOW)
    recipient = ReminderRecipient.objects.create(
        schedule=schedule, address="Tied@example.com", key=recipient_key("tied@example.com")
    )
    ReminderRecipientTie.objects.create(recipient=recipient, membership_id=membership.pk)
    # A chain the outbox still names: first -> before -> now, and gone removed from now.
    outbound(
        OutboundMailKind.EMAIL_CHANGED,
        "before@example.com",
        {"from_email": "before@example.com", "to_email": "now@example.com"},
        finished=True,
    )
    outbound(
        OutboundMailKind.EMAIL_CHANGED,
        "first@example.com",
        {"from_email": "first@example.com", "to_email": "Before@example.com"},
        finished=True,
    )
    outbound(
        OutboundMailKind.EMAIL_DELETED,
        "now@example.com",
        {"deleted_email": "gone@example.com"},
        finished=True,
    )
    held = {
        "invitations": {
            WorkspaceInvitation.objects.create(
                tenant=tenant, email=email, created_by=inviter, expires_at=NOW + timedelta(days=7)
            ).pk
            for email in ("before@example.com", "tied@example.com")
        },
        "mail": {
            outbound(OutboundMailKind.PASSWORD_RESET, "first@example.com", {}, finished=True).pk
        },
    }
    ReminderRecipient.objects.create(
        schedule=schedule, address="gone@example.com", key="gone@example.com"
    )
    AuthenticationCacheEntry.objects.create(
        cache_key="provider-login:gone", value={"email": "GONE@example.com"}, expires=None
    )
    session(
        "pending-reset",
        {PASSWORD_RESET_VERIFICATION_SESSION_KEY: {"email": "tied@example.com"}},
    )
    bystander = outbound(
        OutboundMailKind.EMAIL_CHANGED,
        "other.before@example.com",
        {"from_email": "other.before@example.com", "to_email": "other@example.com"},
        finished=True,
    )
    session(
        "bystander-reset", {PASSWORD_RESET_VERIFICATION_SESSION_KEY: {"email": "x@example.com"}}
    )

    erase_account(person.pk, execute=True)

    assert not WorkspaceInvitation.objects.filter(pk__in=held["invitations"]).exists()
    assert list(OutboundMail.objects.values_list("pk", flat=True)) == [bystander.pk]
    assert not ReminderRecipient.objects.exists()
    assert not AuthenticationCacheEntry.objects.exists()
    assert list(Session.objects.values_list("session_key", flat=True)) == ["bystander-reset"]


def address_rows(address: str, tag: str, tenant: Tenant, inviter: User) -> dict[str, set[Any]]:
    schedule = ReminderSchedule.objects.create(
        region=Region.objects.create(
            tenant=tenant,
            name=f"Site {tag}",
            latitude=38 + Region.objects.count() / 100,
            longitude=27,
            timezone="Europe/Istanbul",
        ),
        effective_from=NOW,
    )
    session(f"pending-{tag}", {PASSWORD_RESET_VERIFICATION_SESSION_KEY: {"email": address}})
    return {
        "mail": {
            outbound(
                OutboundMailKind.PASSWORD_CHANGED, address, {"to_email": address}, finished=False
            ).pk
        },
        "invitations": {
            WorkspaceInvitation.objects.create(
                tenant=tenant,
                email=address.lower(),
                created_by=inviter,
                expires_at=NOW + timedelta(days=7),
            ).pk
        },
        "cache": {
            AuthenticationCacheEntry.objects.create(
                cache_key=f"provider-login:{tag}", value={"email": address}, expires=None
            ).cache_key
        },
        "recipients": {
            ReminderRecipient.objects.create(
                schedule=schedule, address=address, key=recipient_key(address)
            ).pk
        },
        "sessions": {f"pending-{tag}"},
    }


def address_rows_left() -> dict[str, set[Any]]:
    return {
        **bystanders_left(),
        "recipients": set(ReminderRecipient.objects.values_list("pk", flat=True)),
    }


@pytest.mark.parametrize("spelling", [str.lower, str.upper], ids=["same_case", "other_case"])
def test_an_earlier_address_another_account_holds_verified_keeps_its_rows(
    spelling: Any,
) -> None:
    earlier, current = "Moved.From@example.com", "moved.to@example.com"
    person = person_with(current)
    holder = User.objects.create_user(username="holder", email="holder@example.com")
    EmailAddress.objects.create(user=holder, email=spelling(earlier), primary=False, verified=True)
    session("holder-session", {"_auth_user_id": str(holder.pk)})
    tenant, inviter = administered_workspace()
    outbound(
        OutboundMailKind.EMAIL_CHANGED,
        earlier,
        {"from_email": earlier, "to_email": current},
        finished=True,
    )
    kept = address_rows(earlier, "held", tenant, inviter)
    erased = address_rows(current, "own", tenant, inviter)
    kept["sessions"].add("holder-session")
    holder_rows = (
        User.objects.filter(pk=holder.pk).values().get(),
        list(EmailAddress.objects.filter(user=holder).values()),
    )

    dry_run = erase_account(person.pk, execute=False)
    reports = [erase_command("--account", str(person.pk), *flag) for flag in ((), ("--execute",))]

    assert address_rows_left() == kept
    assert (
        User.objects.filter(pk=holder.pk).values().get(),
        list(EmailAddress.objects.filter(user=holder).values()),
    ) == holder_rows
    # The mail of its own address and the address change that names it.
    assert dry_run.changes["notifications.OutboundMail"] == len(erased["mail"]) + 1
    left_out = "Addresses left out because another account holds them now: 1"
    assert [left_out in report.splitlines() for report in reports] == [True, True]
    assert not [report for report in reports if earlier.casefold() in report.casefold()]
    assert dry_run.addresses_left_out == 1


NO_ADDRESS_ROWS: dict[str, set[Any]] = {
    "sessions": set(),
    "mail": set(),
    "cache": set(),
    "invitations": set(),
    "recipients": set(),
}


@pytest.mark.parametrize("claim", ["account_address", "unverified_row"])
@pytest.mark.parametrize("claimed", ["earlier", "own"])
def test_an_unverified_claim_never_keeps_an_address_out_of_erasure(
    claim: str, claimed: str
) -> None:
    earlier, current = "claimed.before@example.com", "claimed.now@example.com"
    person = person_with(current)
    address = (earlier if claimed == "earlier" else current).upper()
    claimant = User.objects.create_user(
        username="claimant",
        email=address if claim == "account_address" else "claimant@example.com",
    )
    if claim == "unverified_row":
        EmailAddress.objects.create(user=claimant, email=address, primary=True, verified=False)
    claimant_rows = (
        User.objects.filter(pk=claimant.pk).values().get(),
        list(EmailAddress.objects.filter(user=claimant).values()),
    )
    tenant, inviter = administered_workspace()
    outbound(
        OutboundMailKind.EMAIL_CHANGED,
        earlier,
        {"from_email": earlier, "to_email": current},
        finished=True,
    )
    address_rows(earlier, "earlier", tenant, inviter)
    address_rows(current, "own", tenant, inviter)

    erased = erase_account(person.pk, execute=True)

    assert address_rows_left() == NO_ADDRESS_ROWS
    assert (
        User.objects.filter(pk=claimant.pk).values().get(),
        list(EmailAddress.objects.filter(user=claimant).values()),
    ) == claimant_rows
    assert erased.addresses_left_out == 0


def ties_by_address() -> dict[str, set[int]]:
    return {
        entry.address: set(entry.ties.values_list("membership_id", flat=True))
        for entry in ReminderRecipient.objects.all()
    }


def test_erasure_removes_its_membership_ties_from_reminder_entries_whatever_address_they_hold() -> (
    None
):
    person = person_with("member.now@example.com")
    tenant, _administrator = administered_workspace()
    own = Membership.objects.create(tenant=tenant, user=person)
    colleague = Membership.objects.create(
        tenant=tenant, user=User.objects.create_user(username="colleague", email=COLLEAGUE)
    )
    holder = User.objects.create_user(username="holder", email="holder@example.com")
    only, shared, direct = (
        "held.only@example.com",
        "held.shared@example.com",
        "held.direct@example.com",
    )
    for address in (only, shared, direct):
        EmailAddress.objects.create(user=holder, email=address, primary=False, verified=True)
    schedule = ReminderSchedule.objects.create(
        region=Region.objects.create(
            tenant=tenant, name="Site", latitude=38, longitude=27, timezone="Europe/Istanbul"
        ),
        effective_from=NOW,
    )
    entries = {
        address: ReminderRecipient.objects.create(
            schedule=schedule, address=address, key=recipient_key(address)
        )
        for address in (only, shared, direct)
    }
    ReminderRecipientTie.objects.bulk_create(
        ReminderRecipientTie(recipient=entries[address], membership_id=membership.pk)
        for address, membership in ((only, own), (shared, own), (shared, colleague))
    )
    revision = ReminderSchedule.objects.get(pk=schedule.pk).revision

    erased = erase_account(person.pk, execute=True)

    assert ties_by_address() == {shared: {colleague.pk}, direct: set()}
    assert ReminderSchedule.objects.get(pk=schedule.pk).revision == revision + 1
    assert {
        label: erased.changes.get(label)
        for label in (
            "notifications.ReminderRecipient",
            "notifications.ReminderRecipientTie",
            "notifications.ReminderSchedule",
        )
    } == {
        "notifications.ReminderRecipient": 1,
        "notifications.ReminderRecipientTie": 2,
        "notifications.ReminderSchedule": 1,
    }
    assert erased.addresses_left_out == 2


def test_rows_another_account_gets_for_an_earlier_address_while_erasure_waits_are_kept() -> None:
    earlier, current = "was.here@example.com", "now.here@example.com"
    person = person_with(current)
    tenant, inviter = administered_workspace()
    Membership.objects.create(tenant=tenant, user=person)
    newcomer = User.objects.create_user(username="newcomer", email="newcomer@example.com")
    outbound(
        OutboundMailKind.EMAIL_CHANGED,
        earlier,
        {"from_email": earlier, "to_email": current},
        finished=True,
    )
    address_rows(current, "own", tenant, inviter)

    def verify_and_receive() -> dict[str, set[Any]]:
        Tenant.objects.select_for_update().get(pk=tenant.pk)
        EmailAddress.objects.create(user=newcomer, email=earlier, primary=True, verified=True)
        return address_rows(earlier, "newcomer", tenant, inviter)

    kept, erased = behind_open_transaction(
        verify_and_receive, lambda: erase_account(person.pk, execute=True)
    )

    assert address_rows_left() == kept
    assert list(EmailAddress.objects.filter(user=newcomer).values_list("email", "verified")) == [
        (earlier, True)
    ]
    assert erased.addresses_left_out == 0


def provider_signup(first_name: str, last_name: str) -> dict[str, Any]:
    return {
        PROVIDER_SIGNUP_SESSION_KEY: {"user": {"first_name": first_name, "last_name": last_name}}
    }


@pytest.mark.parametrize(
    ("names", "pending", "deleted"),
    [
        (("Quill", "Quill"), lambda _other: provider_signup("Quill", "Stranger"), False),
        (("Quill", "Quill"), lambda _other: provider_signup("Quill", "Quill"), True),
        ((FIRST_NAME, LAST_NAME), lambda _other: provider_signup(FIRST_NAME, LAST_NAME), True),
        ((FIRST_NAME, LAST_NAME), lambda _other: provider_signup(LAST_NAME, FIRST_NAME), False),
        (
            (FIRST_NAME, LAST_NAME),
            lambda _other: {"note": FIRST_NAME, "flow": [LAST_NAME, 3, None]},
            False,
        ),
        (
            (FIRST_NAME, LAST_NAME),
            lambda _other: {
                PROVIDER_SIGNUP_SESSION_KEY: {
                    "user": {"first_name": "", "last_name": ""},
                    "account": {"extra_data": {"given": FIRST_NAME, "family": LAST_NAME}},
                }
            },
            False,
        ),
        (
            (FIRST_NAME, LAST_NAME),
            lambda other: {"_auth_user_id": str(other.pk)} | provider_signup(FIRST_NAME, LAST_NAME),
            False,
        ),
        (
            (FIRST_NAME, LAST_NAME),
            lambda other: (
                {PASSWORD_RESET_VERIFICATION_SESSION_KEY: {"user_id": str(other.pk)}}
                | provider_signup(FIRST_NAME, LAST_NAME)
            ),
            False,
        ),
        (
            (FIRST_NAME, LAST_NAME),
            lambda other: {"_auth_user_id": str(other.pk), "noted": "NAMED.PERSON@example.com"},
            True,
        ),
    ],
    ids=[
        "one_name_twice_and_another",
        "one_name_twice",
        "ordered_names",
        "swapped_names",
        "names_elsewhere",
        "names_elsewhere_in_the_sign_up",
        "signed_in_as_another_account",
        "process_of_another_account",
        "another_account_holding_an_address",
    ],
)
def test_erasure_matches_a_pending_sign_up_only_by_its_own_ordered_names(
    names: tuple[str, str], pending: Any, deleted: bool
) -> None:
    person = person_with("named.person@example.com")
    User.objects.filter(pk=person.pk).update(first_name=names[0], last_name=names[1])
    other = User.objects.create_user(username="other", email="other@example.com")
    session("pending", pending(other))

    erase_account(person.pk, execute=True)

    assert Session.objects.filter(session_key="pending").exists() is not deleted


@pytest.mark.parametrize("table", ["mail", "invitations", "recipients"])
def test_a_row_committed_after_erasure_locked_its_table_is_not_deleted(
    monkeypatch: pytest.MonkeyPatch, table: str
) -> None:
    address = "locked.out@example.com"
    person = person_with(address)
    tenant, inviter = administered_workspace()
    Membership.objects.create(tenant=tenant, user=person)
    address_rows(address, "own", tenant, inviter)
    elsewhere = Tenant.objects.create(name="Later workspace")
    later_schedule = ReminderSchedule.objects.create(
        region=Region.objects.create(
            tenant=elsewhere, name="Later", latitude=37, longitude=27, timezone="Europe/Istanbul"
        ),
        effective_from=NOW,
    )
    revision = ReminderSchedule.objects.get(pk=later_schedule.pk).revision
    model: type[models.Model]
    model, add = {
        "mail": (
            OutboundMail,
            lambda: (
                outbound(
                    OutboundMailKind.PASSWORD_CHANGED,
                    address,
                    {"to_email": address},
                    finished=False,
                ).pk
            ),
        ),
        "invitations": (
            WorkspaceInvitation,
            lambda: (
                WorkspaceInvitation.objects.create(
                    tenant=elsewhere,
                    email=address,
                    created_by=inviter,
                    expires_at=NOW + timedelta(days=7),
                ).pk
            ),
        ),
        "recipients": (
            ReminderRecipient,
            lambda: (
                ReminderRecipient.objects.create(
                    schedule=later_schedule, address=address, key=recipient_key(address)
                ).pk
            ),
        ),
    }[table]
    locking = erasure._locked
    later: list[int] = []

    def lock_then_another_client_commits(
        rows: models.QuerySet[Any], *fields: str, no_key: bool = False
    ) -> Any:
        locked = locking(rows, *fields, no_key=no_key)
        if rows.model is model and not later:
            later.append(committed_elsewhere(add))
        return locked

    monkeypatch.setattr(erasure, "_locked", lock_then_another_client_commits)

    erase_account(person.pk, execute=True)

    assert later
    assert list(model._default_manager.values_list("pk", flat=True)) == later
    assert ReminderSchedule.objects.get(pk=later_schedule.pk).revision == revision


def test_erasure_takes_addresses_differing_only_in_ascii_letter_case_as_one() -> None:
    own, held = "Ilker@Example.com", "Beyza@Example.com"
    person = person_with(own.lower())
    holder = User.objects.create_user(username="holder", email="holder@example.com")
    EmailAddress.objects.create(user=holder, email=held.upper(), primary=False, verified=True)
    tenant, inviter = administered_workspace()
    outbound(
        OutboundMailKind.EMAIL_DELETED, own.lower(), {"deleted_email": held.lower()}, finished=True
    )
    kept = address_rows(held.lower(), "held", tenant, inviter)
    address_rows(own.upper(), "own", tenant, inviter)

    erased = erase_account(person.pk, execute=True)

    assert address_rows_left() == kept
    assert erased.addresses_left_out == 1


# U+A7CE, a capital Python 3.15 lower-cases (Unicode 17) and PostgreSQL 18 does not (Unicode 16).
NEWER_CAPITAL = chr(0xA7CE)


@pytest.mark.django_db(transaction=True, databases=["default", "auth_cache"])
def test_a_password_reset_typed_with_a_capital_the_database_cannot_lower_is_erased() -> None:
    typed = f"Reset.Person@{NEWER_CAPITAL}.com"
    person = person_with(typed.lower())

    with override_settings(MAILERS=LOCMEM):
        submit(Client(enforce_csrf_checks=True), "auth/password/request", {"email": typed})
    queued = list(OutboundMail.objects.values_list("recipient", flat=True))
    erase_account(person.pk, execute=True)

    assert queued == [typed.lower()]
    assert not OutboundMail.objects.exists()


def test_a_reminder_entry_typed_with_such_a_capital_that_another_account_verifies_is_kept() -> None:
    earlier, current = f"held.later@{NEWER_CAPITAL}.com", "moved.on@example.com"
    person = person_with(current)
    tenant, _inviter = administered_workspace()
    Membership.objects.create(tenant=tenant, user=person)
    newcomer = User.objects.create_user(username="newcomer", email="newcomer@example.com")
    outbound(
        OutboundMailKind.EMAIL_CHANGED,
        earlier.lower(),
        {"from_email": earlier.lower(), "to_email": current},
        finished=True,
    )
    schedule = ReminderSchedule.objects.create(
        region=Region.objects.create(
            tenant=tenant, name="Site", latitude=38, longitude=27, timezone="Europe/Istanbul"
        ),
        effective_from=NOW,
    )
    # An administrator's entry keeps the address as typed; its key is folded by Python.
    entry = ReminderRecipient.objects.create(
        schedule=schedule, address=earlier, key=recipient_key(earlier)
    )
    revision = ReminderSchedule.objects.get(pk=schedule.pk).revision

    def verify() -> None:
        Tenant.objects.select_for_update().get(pk=tenant.pk)
        EmailAddress.objects.create(
            user=newcomer, email=earlier.lower(), primary=True, verified=True
        )

    behind_open_transaction(verify, lambda: erase_account(person.pk, execute=True))

    assert list(ReminderRecipient.objects.values_list("pk", "address")) == [(entry.pk, earlier)]
    assert ReminderSchedule.objects.get(pk=schedule.pk).revision == revision


def test_a_reminder_entry_committed_in_another_workspace_after_the_workspace_locks_is_kept(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    address = "between.locks@example.com"
    person = person_with(address)
    tenant, inviter = administered_workspace()
    Membership.objects.create(tenant=tenant, user=person)
    address_rows(address, "own", tenant, inviter)
    elsewhere = Tenant.objects.create(name="Other workspace")
    region = Region.objects.create(
        tenant=elsewhere, name="Other", latitude=37, longitude=27, timezone="Europe/Istanbul"
    )
    schedule = ReminderSchedule.objects.create(region=region, effective_from=NOW)
    revision = ReminderSchedule.objects.get(pk=schedule.pk).revision
    locking = erasure._locked
    later: list[int] = []
    locked_regions: list[int] = []

    def commit_after_the_workspace_lock(
        rows: models.QuerySet[Any], *fields: str, no_key: bool = False
    ) -> Any:
        locked = locking(rows, *fields, no_key=no_key)
        if rows.model is Tenant and not later:
            later.append(
                committed_elsewhere(
                    lambda: (
                        ReminderRecipient.objects.create(
                            schedule=schedule, address=address, key=recipient_key(address)
                        ).pk
                    )
                )
            )
        if rows.model is Region:
            locked_regions.extend(locked)
        return locked

    monkeypatch.setattr(erasure, "_locked", commit_after_the_workspace_lock)

    erase_account(person.pk, execute=True)

    assert later
    assert region.pk not in locked_regions
    assert list(ReminderRecipient.objects.values_list("pk", flat=True)) == later
    assert ReminderSchedule.objects.get(pk=schedule.pk).revision == revision


def test_a_mail_row_held_by_another_client_while_another_account_verifies_its_address_is_kept() -> (
    None
):
    earlier, current = "was.mailed@example.com", "mailed.now@example.com"
    person = person_with(current)
    newcomer = User.objects.create_user(username="newcomer", email="newcomer@example.com")
    outbound(
        OutboundMailKind.EMAIL_CHANGED,
        earlier,
        {"from_email": earlier, "to_email": current},
        finished=True,
    )
    held = outbound(
        OutboundMailKind.PASSWORD_CHANGED, earlier, {"to_email": earlier}, finished=False
    )

    def hold_and_verify() -> None:
        OutboundMail.objects.select_for_update().get(pk=held.pk)
        EmailAddress.objects.create(user=newcomer, email=earlier, primary=True, verified=True)

    behind_open_transaction(hold_and_verify, lambda: erase_account(person.pk, execute=True))

    assert list(OutboundMail.objects.values_list("pk", flat=True)) == [held.pk]


def test_an_invitation_held_by_another_client_while_its_address_is_verified_elsewhere_is_kept() -> (
    None
):
    earlier, current = "was.invited@example.com", "invited.now@example.com"
    person = person_with(current)
    newcomer = User.objects.create_user(username="newcomer", email="newcomer@example.com")
    tenant, inviter = administered_workspace()
    outbound(
        OutboundMailKind.EMAIL_CHANGED,
        earlier,
        {"from_email": earlier, "to_email": current},
        finished=True,
    )
    held = WorkspaceInvitation.objects.create(
        tenant=tenant, email=earlier, created_by=inviter, expires_at=NOW + timedelta(days=7)
    )

    def hold_and_verify() -> None:
        WorkspaceInvitation.objects.select_for_update().get(pk=held.pk)
        EmailAddress.objects.create(user=newcomer, email=earlier, primary=True, verified=True)

    behind_open_transaction(hold_and_verify, lambda: erase_account(person.pk, execute=True))

    assert list(WorkspaceInvitation.objects.values_list("pk", flat=True)) == [held.pk]


def test_erasure_reports_only_the_tables_it_changes() -> None:
    person = person_with("plain.person@example.com")

    dry_run = erase_account(person.pk, execute=False)
    report = erase_command("--account", str(person.pk))

    assert dry_run.changes
    assert [label for label, count in dry_run.changes.items() if not count] == []
    assert [
        line for line in report.splitlines() if line.startswith("  ") and line.endswith(": 0")
    ] == []


def test_erasure_refuses_an_account_that_administers_a_workspace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, _plant = make_plant(monkeypatch)
    second = Tenant.objects.create(name="Second workspace")
    Membership.objects.create(tenant=second, user=system.member, role=WorkspaceRole.ADMIN)
    before = database()

    with pytest.raises(AdministersWorkspaceError) as refused:
        erase_account(system.member.pk, execute=True)

    assert refused.value.tenant_ids == (system.tenant.pk, second.pk)
    assert str(refused.value) == (
        f"Account {system.member.pk} administers workspaces {system.tenant.pk}, {second.pk}. "
        "Assign another administrator or purge each workspace first."
    )
    assert database() == before


@pytest.mark.parametrize("held", ["account", "workspace"])
def test_an_administrator_is_refused_before_erasure_takes_any_lock(
    monkeypatch: pytest.MonkeyPatch, held: str
) -> None:
    system, _plant = make_plant(monkeypatch)
    row: tuple[type[models.Model], int] = (
        (User, system.member.pk) if held == "account" else (Tenant, system.tenant.pk)
    )

    with (
        row_locked_elsewhere(*row),
        short_lock_timeout(),
        pytest.raises(AdministersWorkspaceError),
    ):
        erase_account(system.member.pk, execute=True)


def test_erasure_waiting_for_an_administrator_assignment_refuses_once_it_commits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seed = seeded(monkeypatch)

    def assign() -> None:
        reassign_workspace_admin(
            seed.system.admin,
            seed.system.tenant.pk,
            seed.person.pk,
            expected_admin_id=seed.successor.pk,
        )

    def erase() -> type[Exception] | None:
        try:
            erase_account(seed.person.pk, execute=True)
        except AdministersWorkspaceError as error:
            return type(error)
        return None

    _, refused = behind_open_transaction(assign, erase)

    assert refused is AdministersWorkspaceError
    assert Membership.objects.get(tenant=seed.system.tenant, role=WorkspaceRole.ADMIN).user_id == (
        seed.person.pk
    )
    assert EmailAddress.objects.filter(user=seed.person).count() == 2


def test_erasure_reaching_an_invitation_mail_a_mail_worker_holds_waits_for_the_worker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    person = person_with("invitee@example.com")
    tenant, inviter = administered_workspace()
    invitation = invite_workspace_member(inviter, tenant.pk, "invitee@example.com")
    held = OutboundMail.objects.get(invitation=invitation)

    with override_settings(MAILERS=LOCMEM):
        _sent, erased = paused_in(
            monkeypatch,
            outbox,
            "_compose",
            lambda: send_outbound_mail(held.pk, timezone.now()),
            lambda: erase_account(person.pk, execute=True),
        )

    # The worker held the mail before erasure reached it, so it sent first; erasure then
    # deleted both rows.
    assert [message.to for message in mail.outbox] == [["invitee@example.com"]]
    assert (
        erased.changes["notifications.OutboundMail"],
        erased.changes["accounts.WorkspaceInvitation"],
    ) == (1, 1)
    assert not WorkspaceInvitation.objects.exists()
    assert not OutboundMail.objects.exists()


def test_an_audit_entry_on_a_region_erasure_holds_is_recorded_while_its_actor_is_erased(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    platform_administrator("remaining-admin")
    person = platform_administrator("erased-admin")
    tenant, _inviter = administered_workspace()
    region = Region.objects.create(
        tenant=tenant, name="Reminded", latitude=38, longitude=27, timezone="Europe/Istanbul"
    )
    schedule = ReminderSchedule.objects.create(region=region, effective_from=NOW)
    ReminderRecipient.objects.create(
        schedule=schedule, address=person.email, key=recipient_key(person.email)
    )

    def audit_and_see_actor() -> tuple[int, bool]:
        entry = record_audit(AuditAction.REGION_RENAMED, actor=person, region=region, revision=0)
        return entry.pk, User.objects.get(pk=person.pk).is_active

    erased, (audited, actor_active_at_audit) = paused_in(
        monkeypatch,
        erasure,
        "_forget_recipients",
        lambda: erase_account(person.pk, execute=True),
        audit_and_see_actor,
    )

    assert actor_active_at_audit
    assert erased.changes["notifications.ReminderRecipient"] == 1
    assert list(AuditEvent.objects.values_list("pk", "actor_id", "region_id")) == [
        (audited, person.pk, region.pk)
    ]
    assert erasure.is_erased(User.objects.get(pk=person.pk))


def refusal(action: Callable[[], object]) -> ErrorCode | None:
    try:
        action()
    except DomainError as error:
        return error.code
    return None


def test_an_administrator_assignment_reaching_a_workspace_erasure_holds_waits_for_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system = make_system()
    person = person_with("assigned.during@example.com")
    Membership.objects.create(tenant=system.tenant, user=person)

    erased, refused = paused_in(
        monkeypatch,
        erasure,
        "_forget_recipients",
        lambda: erase_account(person.pk, execute=True),
        lambda: refusal(
            lambda: reassign_workspace_admin(
                system.admin, system.tenant.pk, person.pk, expected_admin_id=system.member.pk
            )
        ),
    )

    assert refused == ErrorCode.MEMBER_UNAVAILABLE
    assert erased.changes["accounts.Membership"] == 1
    assert list(Membership.objects.filter(tenant=system.tenant).values_list("user_id", "role")) == [
        (system.member.pk, WorkspaceRole.ADMIN)
    ]


def test_a_reminder_change_to_a_region_erasure_is_clearing_waits_for_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system = make_system()
    person = person_with("reminded.during@example.com")
    Membership.objects.create(tenant=system.tenant, user=person)

    def configure(recipients: list[str], revision: int | None = None) -> ReminderSchedule:
        return configure_reminders(
            system.member,
            system.tenant.pk,
            system.region.pk,
            enabled=False,
            recipients=recipients,
            times=["10:00"],
            language="en",
            now=NOW,
            expected_revision=revision,
        )

    schedule = configure(["reminded.during@example.com"])

    erased, refused = paused_in(
        monkeypatch,
        erasure,
        "_forget_recipients",
        lambda: erase_account(person.pk, execute=True),
        lambda: refusal(
            lambda: configure(["reminded.during@example.com", COLLEAGUE], schedule.revision)
        ),
    )

    assert refused == ErrorCode.REMINDER_SCHEDULE_CHANGED
    assert erased.changes["notifications.ReminderRecipient"] == 1
    assert not ReminderRecipient.objects.filter(schedule=schedule).exists()
    assert ReminderSchedule.objects.get(pk=schedule.pk).revision == schedule.revision + 1


def test_erasure_that_meets_a_held_lock_changes_nothing_and_says_to_run_it_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seed = seeded(monkeypatch)
    account = seed.person.pk
    before = database()

    with row_locked_elsewhere(Region, seed.system.region.pk), short_lock_timeout():
        with pytest.raises(erasure.ErasureInterruptedError) as interrupted:
            erase_account(account, execute=True)
        with pytest.raises(CommandError) as reported:
            erase_command("--account", str(account), "--execute")

    message = (
        f"Erasure of account {account} waited for a lock held by concurrent work and changed "
        "nothing. Run it again."
    )
    assert str(interrupted.value) == str(reported.value) == message
    assert interrupted.value.__suppress_context__
    assert database() == before


def test_a_database_failure_other_than_a_lock_conflict_is_not_reported_as_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    person = person_with("failing@example.com")

    def fail(_account: User) -> None:
        raise OperationalError("server closed the connection")

    monkeypatch.setattr(erasure, "_anonymize", fail)

    with pytest.raises(OperationalError, match=r"^server closed the connection$"):
        erase_account(person.pk, execute=True)
    assert User.objects.get(pk=person.pk).email == "failing@example.com"


def platform_administrator(name: str) -> User:
    return User.objects.create_user(
        username=name, email=f"{name}@example.com", is_staff=True, is_superuser=True
    )


def test_the_last_active_platform_administrator_is_not_erased() -> None:
    only = platform_administrator("only-admin")
    User.objects.create_user(
        username="former-admin", is_staff=True, is_superuser=True, is_active=False
    )
    User.objects.create_user(username="staff-only", is_staff=True)
    before = database()

    with pytest.raises(erasure.LastPlatformAdministratorError) as refused:
        erase_account(only.pk, execute=True)
    with pytest.raises(CommandError) as reported:
        erase_command("--account", str(only.pk))

    assert (
        str(refused.value)
        == str(reported.value)
        == (
            f"Account {only.pk} is the last active platform administrator. Create another one with "
            "bootstrap_admin first."
        )
    )
    assert database() == before


def test_erasures_of_the_last_two_platform_administrators_keep_one() -> None:
    first, second = platform_administrator("first-admin"), platform_administrator("second-admin")

    def erase_second() -> type[Exception] | None:
        try:
            erase_account(second.pk, execute=True)
        except erasure.LastPlatformAdministratorError as error:
            return type(error)
        return None

    _, refused = behind_open_transaction(
        lambda: erase_account(first.pk, execute=True), erase_second
    )

    assert refused is erasure.LastPlatformAdministratorError
    assert not is_platform_admin(User.objects.get(pk=first.pk))
    assert is_platform_admin(User.objects.get(pk=second.pk))


def test_the_dry_run_says_when_the_account_has_platform_access() -> None:
    platform_administrator("remaining-admin")
    leaving = platform_administrator("leaving-admin")
    staff = User.objects.create_user(username="staff-member", is_staff=True)
    customer = User.objects.create_user(username="customer")

    assert (
        f"Account {leaving.pk} is a platform administrator; erasure removes that access."
        in erase_command("--account", str(leaving.pk)).splitlines()
    )
    assert (
        f"Account {staff.pk} has staff or superuser access; erasure removes it."
        in erase_command("--account", str(staff.pk)).splitlines()
    )
    assert not [
        line
        for line in erase_command("--account", str(customer.pk)).splitlines()
        if "access" in line
    ]


def test_erasing_an_erased_account_again_changes_nothing(
    monkeypatch: pytest.MonkeyPatch, pyracast_logs: pytest.LogCaptureFixture
) -> None:
    seed = seeded(monkeypatch)
    account = seed.person.pk
    erase_account(account, execute=True)
    lines = capture_security_events(pyracast_logs, monkeypatch)
    pyracast_logs.clear()
    before = database()

    again = erase_account(account, execute=True)
    outputs = [erase_command("--account", str(account), *flag) for flag in ((), ("--execute",))]

    assert (again.already_erased, again.changes) == (True, {})
    assert outputs == [f"Account {account} is already erased. Nothing was changed.\n"] * 2
    assert database() == before
    assert not [line for line in lines() if line["event"] == "account_erased"]


def test_concurrent_erasures_of_one_account_erase_it_once(
    monkeypatch: pytest.MonkeyPatch, pyracast_logs: pytest.LogCaptureFixture
) -> None:
    person = person_with("twice@example.com")
    lines = capture_security_events(pyracast_logs, monkeypatch)

    first, second = behind_open_transaction(
        lambda: erase_account(person.pk, execute=True),
        lambda: erase_account(person.pk, execute=True),
    )

    assert (first.already_erased, second.already_erased, second.changes) == (False, True, {})
    assert [line["target_id"] for line in lines() if line["event"] == "account_erased"] == [
        person.pk
    ]


def test_erasure_refuses_while_another_account_holds_the_anonymized_username() -> None:
    person = person_with("named@example.com")
    holder = User.objects.create(username=f"erased-{person.pk}")
    before = database()

    with pytest.raises(erasure.ErasedNameTakenError) as refused:
        erase_account(person.pk, execute=True)
    with pytest.raises(CommandError) as reported:
        erase_command("--account", str(person.pk), "--execute")

    assert (
        str(refused.value)
        == str(reported.value)
        == (
            f"Account {holder.pk} holds the username erasure gives account {person.pk}. "
            f"Rename account {holder.pk} first."
        )
    )
    assert database() == before


def test_a_retention_cycle_leaves_erased_accounts_as_erasure_left_them(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seed = seeded(monkeypatch)
    loner = person_with("loner@example.com", username="loner")
    erased = (seed.person.pk, loner.pk)
    User.objects.filter(pk__in=erased).update(date_joined=NOW - timedelta(days=8))
    for account in erased:
        erase_account(account, execute=True)
    left = list(User.objects.filter(pk__in=erased).order_by("pk").values_list())
    selected: list[int] = []
    reclaim = account_retention.reclaim_signup

    def spied(user_id: int, cutoff: Any) -> bool:
        selected.append(user_id)
        return reclaim(user_id, cutoff)

    monkeypatch.setattr(account_retention, "reclaim_signup", spied)

    dispatcher._retention()

    assert selected == []
    assert list(User.objects.filter(pk__in=erased).order_by("pk").values_list()) == left


def test_the_console_and_the_api_show_kept_history_under_the_anonymized_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seed = seeded(monkeypatch)
    person = seed.person
    history = {
        relation: model
        for relation, (model, field) in relations_to_accounts().items()
        if relation in KEPT and model._base_manager.filter(**{field: person.pk}).exists()
    }
    erase_account(person.pk, execute=True)
    name = f"erased-{person.pk}"
    client, _ = verified_admin()
    pages = [
        reverse("admin:index"),
        reverse("admin:workspace_plant", args=(seed.system.region.pk,)),
    ]
    for relation, model in history.items():
        if model not in site._registry:
            continue
        options = model._meta
        _, field = relations_to_accounts()[relation]
        pages.append(reverse(f"admin:{options.app_label}_{options.model_name}_changelist"))
        pages += [
            reverse(f"admin:{options.app_label}_{options.model_name}_change", args=(pk,))
            for pk in model._base_manager.filter(**{field: person.pk}).values_list("pk", flat=True)
        ]

    shown = {page: client.get(page) for page in pages}

    assert {page: response.status_code for page, response in shown.items()} == dict.fromkeys(
        pages, 200
    )
    texts = {page: response.content.decode() for page, response in shown.items()}
    assert [page for page, text in texts.items() if name not in text] == []
    assert [page for page, text in texts.items() if ">None<" in text] == []
    assert [
        (page, value)
        for page, text in texts.items()
        for value in PERSONAL
        if value.casefold() in text.casefold()
    ] == []
    detail = signed_in(seed.successor).get(
        f"/api/tenants/{seed.system.tenant.pk}/change-requests/{seed.change_request.pk}/"
    )
    assert detail.status_code == 200
    assert detail.json()["proposed_by"] == {"id": person.pk, "name": name}


def test_an_erased_provider_account_authorizer_is_shown_only_by_the_anonymized_name() -> None:
    system = make_system()
    person = person_with(PRIMARY, SECONDARY)
    User.objects.filter(pk=person.pk).update(is_staff=True, is_superuser=True)
    person.refresh_from_db()
    account = provider_operations.create_account(
        person,
        provider="solcast",
        external_account_id="authorized-owner",
        api_key=provider_key("CURRENT"),
        authorization_reference="Authorized service use",
    )
    binding = configure_binding(
        person,
        system.region.pk,
        provider_account_id=account.pk,
        site_id="authorized-site",
        capacity_kw=1,
        capacity_semantics_verified=True,
        installation_date=date(2020, 1, 1),
        settings_verification_reference="Verified site settings",
    )
    validate_binding(person, binding.pk, FakeAdapter(), timezone.now())
    approve_region(person, system.region.pk, binding.pk, timezone.now())
    provider_operations.replace_api_key(
        person, account.pk, provider_key("REPLACEMENT"), expected_revision=1
    )
    lease = ProviderAccount.objects.get(pk=account.pk).revalidation_due_at
    assert lease is not None

    erase_account(person.pk, execute=True)
    requests = MagicMock()
    attempts = RequestAttempt.objects.filter(provider_account=account).count()

    outcome = revalidate_account(
        account.pk,
        timezone.now(),
        claim=lease,
        adapter_for=lambda _: FakeAdapter(requests),
    )

    assert outcome == RevalidationOutcome.STOPPED
    requests.assert_not_called()
    assert RequestAttempt.objects.filter(provider_account=account).count() == attempts
    assert ProviderAccount.objects.get(pk=account.pk).revalidation_due_at is None
    assert SiteBinding.objects.get(pk=binding.pk).validated_credential_revision == 1
    assert ProviderAccount.objects.get(pk=account.pk).authorized_by_id == person.pk
    name = f"erased-{person.pk}"
    client, _ = verified_admin()
    account_page = client.get(reverse("admin:workspace_provider_account", args=(account.pk,)))
    plant_page = client.get(reverse("admin:workspace_plant", args=(system.region.pk,)))

    assert (account_page.status_code, plant_page.status_code) == (200, 200)
    shown = {"account": account_page.content.decode(), "plant": plant_page.content.decode()}
    assert [page for page, text in shown.items() if name not in text] == []
    assert [page for page, text in shown.items() if ">None<" in text] == []
    assert [
        (page, value)
        for page, text in shown.items()
        for value in PERSONAL
        if value.casefold() in text.casefold()
    ] == []
    history = shown["account"].split('id="history-title"', 1)[1]
    assert history.count(name) == 2
    assert "1 approved plant waits for revalidation with the current key." in shown["account"]


@pytest.mark.django_db(transaction=True, databases=["default", "auth_cache"])
def test_a_later_sign_up_and_provider_sign_in_never_reach_the_erased_account(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    person = person_with(PRIMARY, SECONDARY)
    SocialAccount.objects.create(user=person, provider="google", uid=PROVIDER_UID)
    erase_account(person.pk, execute=True)
    erased = User.objects.filter(pk=person.pk).values().get()
    profile = serve_provider_profile(monkeypatch)
    profile.update(sub=PROVIDER_UID, email=SECONDARY)

    with override_settings(SOCIALACCOUNT_PROVIDERS=PROVIDERS, MAILERS=LOCMEM):
        signed_up = signup(
            Client(enforce_csrf_checks=True), {"email": PRIMARY, "company_name": "Later Solar"}
        )
        client = Client()
        pending = complete(client, begin(client))
        provider_signup = submit(
            client, "auth/provider/signup", {"email": SECONDARY, "company_name": "Provider Solar"}
        )

    assert (signed_up.status_code, pending.status_code, provider_signup.status_code) == (
        401,
        401,
        401,
    )
    later = User.objects.get(email__iexact=PRIMARY)
    provider_user = SocialAccount.objects.get(provider="google", uid=PROVIDER_UID).user
    assert person.pk not in (later.pk, provider_user.pk)
    assert later.pk != provider_user.pk
    assert User.objects.filter(pk=person.pk).values().get() == erased


def test_erasure_of_an_unknown_account_is_refused() -> None:
    with pytest.raises(User.DoesNotExist):
        erase_account(987654, execute=True)


def erase_command(*arguments: str) -> str:
    out = io.StringIO()
    call_command("erase_account", *arguments, stdout=out)
    return out.getvalue()


def test_the_command_is_a_dry_run_unless_execution_is_asked_for(
    monkeypatch: pytest.MonkeyPatch,
    pyracast_logs: pytest.LogCaptureFixture,
) -> None:
    seed = seeded(monkeypatch)
    lines = capture_security_events(pyracast_logs, monkeypatch)
    before = database()
    account = seed.person.pk
    pyracast_logs.clear()

    output = erase_command("--account", str(account))

    assert database() == before
    assert output.splitlines()[0] == f"Dry run: erasing account {account} would change:"
    assert "  account.EmailAddress: 2" in output.splitlines()
    assert "Addresses left out because another account holds them now: 0" in output.splitlines()
    assert output.splitlines()[-1] == (
        "Nothing was changed. Erasure cannot be undone: take a backup, then run again "
        "with --execute."
    )
    assert not [line for line in lines() if line["event"] == "account_erased"]

    output = erase_command("--account", str(account), "--execute")

    assert output.splitlines()[0] == f"Erased account {account}:"
    assert "  auth.User: 1" in output.splitlines()
    assert not EmailAddress.objects.filter(user_id=account).exists()
    assert [line for line in lines() if line["event"] == "account_erased"] == [
        {"event": "account_erased", "user_id": None, "client_ip": None, "target_id": account}
    ]
    logged = output + pyracast_logs.text
    assert not [value for value in PERSONAL if value.lower() in logged.lower()]


def test_the_command_reports_a_refusal_without_changing_anything(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, _plant = make_plant(monkeypatch)
    before = database()

    with pytest.raises(CommandError, match=rf"^Account {system.member.pk} administers workspace "):
        erase_command("--account", str(system.member.pk), "--execute")
    with pytest.raises(CommandError, match=r"^Account 987654 does not exist\.$"):
        erase_command("--account", "987654")

    assert database() == before
