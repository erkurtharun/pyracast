import io
from dataclasses import dataclass
from datetime import timedelta
from functools import reduce
from operator import or_
from typing import Any

import pytest
from allauth.account.models import EmailAddress
from allauth.mfa.models import Authenticator
from allauth.socialaccount.models import SocialAccount
from allauth.usersessions.models import UserSession
from django.apps import apps
from django.contrib.auth.models import Group, User
from django.contrib.sessions.models import Session
from django.core.management import CommandError, call_command
from django.db import models
from django.db.models.functions import Cast

from pyracast.accounts.erasure import AdministersWorkspaceError, erase_account
from pyracast.accounts.invitations import invite_workspace_member
from pyracast.accounts.models import (
    AuthenticationCacheEntry,
    Membership,
    OutboundMail,
    OutboundMailKind,
    Tenant,
    UserLanguagePreference,
    WorkspaceRole,
)
from pyracast.accounts.operations import reassign_workspace_admin
from pyracast.notifications.models import DeliveryRecipient, ReminderRecipient, ReminderSchedule
from pyracast.notifications.operations import configure_reminders
from pyracast.notifications.recipients import recipient_key
from pyracast.notifications.scheduler import enqueue_due
from pyracast.regions.models import Region
from tests.support.account_sessions import capture_security_events
from tests.support.concurrency import behind_open_transaction
from tests.support.domain import NOW, System
from tests.support.notifications import SLOT, saved
from tests.support.plant_operations import make_plant

pytestmark = pytest.mark.django_db(transaction=True)

PRIMARY = "Aylin.Quillfeather@example.com"
SECONDARY = "aylin.q@work-example.net"
FIRST_NAME = "Aylinzq"
LAST_NAME = "Quillfeather"
USERNAME = "aylinzq-person"
COLLEAGUE = "colleague@example.com"
KEPT_ADDRESS = "kept@example.com"
PERSONAL = (PRIMARY, SECONDARY, FIRST_NAME, LAST_NAME, USERNAME)
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
    return found


def database() -> dict[str, list[tuple[Any, ...]]]:
    return {
        model._meta.label: list(model._base_manager.order_by("pk").values_list())
        for model in apps.get_models(include_auto_created=True)
    }


@dataclass(frozen=True)
class Seeded:
    system: System
    person: User
    successor: User
    other: Tenant
    schedule: ReminderSchedule
    other_schedule: ReminderSchedule


def seeded(monkeypatch: pytest.MonkeyPatch) -> Seeded:
    system, _plant = make_plant(monkeypatch)
    person = system.member
    User.objects.filter(pk=person.pk).update(
        username=USERNAME, email=PRIMARY, first_name=FIRST_NAME, last_name=LAST_NAME
    )
    person.refresh_from_db()
    person.set_password("Person-passw0rd-2026")
    person.save()
    EmailAddress.objects.create(user=person, email=PRIMARY, primary=True, verified=True)
    EmailAddress.objects.create(user=person, email=SECONDARY, primary=False, verified=True)
    SocialAccount.objects.create(
        user=person,
        provider="google",
        uid="person-google-subject",
        extra_data={"email": PRIMARY, "name": f"{FIRST_NAME} {LAST_NAME}"},
    )
    Authenticator.objects.create(
        user=person, type=Authenticator.Type.TOTP, data={"secret": "person-totp-secret"}
    )
    UserLanguagePreference.objects.create(user=person, language="tr")
    person.groups.add(Group.objects.create(name="operators"))
    Session.objects.create(
        session_key="person-session", session_data="", expire_date=NOW + timedelta(hours=1)
    )
    UserSession.objects.create(
        user=person,
        session_key="person-session",
        ip="203.0.113.7",
        user_agent=f"Browser of {FIRST_NAME}",
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
    assert len(enqueue_due(SLOT)) == 1
    successor = User.objects.create(username="successor", email="successor@example.com")
    Membership.objects.create(tenant=system.tenant, user=successor)
    reassign_workspace_admin(
        system.admin, system.tenant.pk, successor.pk, expected_admin_id=person.pk
    )
    inviter = User.objects.create(username="inviter", email="inviter@example.com")
    other = Tenant.objects.create(name="Other workspace")
    Membership.objects.create(tenant=other, user=inviter, role=WorkspaceRole.ADMIN)
    invite_workspace_member(inviter, other.pk, SECONDARY)
    other_region = Region.objects.create(
        tenant=other, name="Other site", latitude=38, longitude=27, timezone="Europe/Istanbul"
    )
    other_schedule = ReminderSchedule.objects.create(region=other_region, effective_from=NOW)
    for address in (SECONDARY.upper(), KEPT_ADDRESS):
        ReminderRecipient.objects.create(
            schedule=other_schedule, address=address, key=recipient_key(address)
        )
    OutboundMail.objects.create(
        kind=OutboundMailKind.PASSWORD_CHANGED,
        recipient=PRIMARY,
        language="en",
        context={"to_email": PRIMARY},
        created_at=NOW,
        available_at=NOW,
        expires_at=NOW + timedelta(hours=1),
    )
    AuthenticationCacheEntry.objects.create(
        cache_key="provider-login:person",
        value={"email": PRIMARY, "name": LAST_NAME},
        expires=NOW + timedelta(minutes=10),
    )
    return Seeded(system, person, successor, other, schedule, other_schedule)


def test_every_relation_to_an_account_is_erased_or_kept_for_a_stated_reason() -> None:
    assert set(relations_to_accounts()) == ERASED | KEPT.keys()
    assert not ERASED & KEPT.keys()


def test_erasure_leaves_no_personal_data_and_keeps_the_history_that_points_at_the_account(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seed = seeded(monkeypatch)
    person = seed.person
    before = pointing_at(person)
    assert rows_holding(PERSONAL)
    revisions = dict(ReminderSchedule.objects.values_list("pk", "revision"))

    erasure = erase_account(person.pk, execute=True)

    assert rows_holding(PERSONAL) == []
    after = pointing_at(person)
    assert {relation: after[relation] for relation in ERASED} == dict.fromkeys(ERASED, 0)
    assert {relation: after[relation] for relation in KEPT} == {
        relation: before[relation] for relation in KEPT
    }
    assert before["plants.PlantSubmission.submitted_by"] == 1
    assert before["accounts.WorkspaceAdminChange.previous_administrator"] == 1
    assert before["regions.AuditEvent.actor"] > 0
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
    assert not Session.objects.filter(session_key="person-session").exists()
    assert not AuthenticationCacheEntry.objects.exists()
    assert sorted(ReminderRecipient.objects.values_list("address", flat=True)) == [
        COLLEAGUE,
        KEPT_ADDRESS,
    ]
    assert list(DeliveryRecipient.objects.values_list("address", flat=True)) == [COLLEAGUE]
    assert dict(ReminderSchedule.objects.values_list("pk", "revision")) == {
        pk: revision + 1 for pk, revision in revisions.items()
    }
    assert Membership.objects.filter(tenant=seed.system.tenant).get().user == seed.successor
    assert erasure.account_id == person.pk
    assert erasure.changes == {
        "account.EmailAddress": 2,
        "accounts.AuthenticationCacheEntry": 1,
        "accounts.Membership": 1,
        "accounts.UserLanguagePreference": 1,
        "accounts.WorkspaceInvitation": 1,
        "auth.User": 1,
        "auth.User_groups": 1,
        "mfa.Authenticator": 1,
        "notifications.DeliveryRecipient": 1,
        "notifications.OutboundMail": 3,
        "notifications.ReminderRecipient": 2,
        "notifications.ReminderRecipientTie": 1,
        "notifications.ReminderSchedule": 2,
        "sessions.Session": 1,
        "socialaccount.SocialAccount": 1,
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
