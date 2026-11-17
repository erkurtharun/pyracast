from datetime import datetime, timedelta
from html.parser import HTMLParser

import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from django.utils import timezone

from pyracast.connections.models import ProviderAccount
from pyracast.connections.recovery import clear_provider_cooldown
from pyracast.forecasts.models import RequestAttempt
from pyracast.regions.models import AuditEvent
from tests.support.admin_setup import admin_system, setup_url
from tests.support.api import verified_admin

pytestmark = pytest.mark.django_db(transaction=True)


class HiddenFields(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.values: dict[str, str] = {}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        name = values.get("name")
        if tag == "input" and values.get("type") == "hidden" and name is not None:
            self.values[name] = values.get("value") or ""


def test_confirmed_admin_recovery_is_audited_and_keeps_quota_and_authentication_block() -> None:
    system = admin_system()
    now = timezone.now()
    until = now.replace(microsecond=123456) + timedelta(days=2)
    system.account.request_blocked_until = until
    system.account.authentication_failed_revision = system.account.credential_revision
    system.account.save()
    RequestAttempt.objects.create(
        provider_account=system.account,
        utc_day=now.date(),
        created_at=now,
        purpose="forecasts",
        outcome="uncertain",
    )
    client, admin = verified_admin()
    response = client.get(setup_url(system))
    assert b"Clear confirmed provider delay" in response.content
    fields = HiddenFields()
    fields.feed(response.content.decode())
    expected = datetime.fromisoformat(fields.values["expected_cooldown_until"])
    assert expected == until
    assert expected.microsecond == 123456
    payload = {**fields.values, "action": "clear_cooldown"}
    assert client.post(setup_url(system), payload).status_code == 400
    system.account.refresh_from_db()
    assert system.account.request_blocked_until is not None
    response = client.post(setup_url(system), {**payload, "provider_recovered": "on"})
    assert response.status_code == 302
    account = ProviderAccount.objects.get(pk=system.account.pk)
    assert account.request_blocked_until is None
    assert account.authentication_failed_revision == account.credential_revision
    assert RequestAttempt.objects.count() == 1
    event = AuditEvent.objects.get(action="provider_cooldown_cleared")
    assert event.actor == admin
    assert event.provider_account == system.account
    clear_provider_cooldown(
        admin, system.account.pk, system.account.credential_revision, expected_blocked_until=until
    )
    assert AuditEvent.objects.filter(action="provider_cooldown_cleared").count() == 1


def test_cooldown_recovery_rejects_non_admin_and_stale_credentials() -> None:
    system = admin_system()
    until = timezone.now() + timedelta(hours=1)
    system.account.request_blocked_until = until
    system.account.save()
    with pytest.raises(PermissionDenied):
        clear_provider_cooldown(
            system.member,
            system.account.pk,
            system.account.credential_revision,
            expected_blocked_until=until,
        )
    with pytest.raises(ValidationError, match="account changed"):
        clear_provider_cooldown(
            system.admin,
            system.account.pk,
            system.account.credential_revision + 1,
            expected_blocked_until=until,
        )
    system.account.refresh_from_db()
    assert system.account.request_blocked_until == until
    assert not AuditEvent.objects.filter(action="provider_cooldown_cleared").exists()


def test_cooldown_reset_rolls_back_when_auditing_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    system = admin_system()
    until = timezone.now() + timedelta(hours=1)
    system.account.request_blocked_until = until
    system.account.save()

    def fail_audit(**kwargs: object) -> None:
        raise RuntimeError("audit unavailable")

    monkeypatch.setattr(AuditEvent.objects, "create", fail_audit)
    with pytest.raises(RuntimeError, match="audit unavailable"):
        clear_provider_cooldown(
            system.admin,
            system.account.pk,
            system.account.credential_revision,
            expected_blocked_until=until,
        )
    system.account.refresh_from_db()
    assert system.account.request_blocked_until == until


def test_stale_confirmation_cannot_clear_a_newer_provider_delay() -> None:
    system = admin_system()
    original = timezone.now().replace(microsecond=123456) + timedelta(hours=1)
    system.account.request_blocked_until = original
    system.account.save(update_fields=("request_blocked_until",))
    client, _ = verified_admin()
    response = client.get(setup_url(system))
    fields = HiddenFields()
    fields.feed(response.content.decode())
    newer = original + timedelta(hours=2)
    ProviderAccount.objects.filter(pk=system.account.pk).update(request_blocked_until=newer)
    response = client.post(
        setup_url(system), {**fields.values, "action": "clear_cooldown", "provider_recovered": "on"}
    )
    assert response.status_code == 400
    system.account.refresh_from_db()
    assert system.account.request_blocked_until == newer
    assert not AuditEvent.objects.filter(action="provider_cooldown_cleared").exists()


def test_empty_cooldown_is_idempotent_but_cannot_authorize_a_later_delay() -> None:
    system = admin_system()
    clear_provider_cooldown(
        system.admin,
        system.account.pk,
        system.account.credential_revision,
        expected_blocked_until=None,
    )
    until = timezone.now() + timedelta(hours=1)
    ProviderAccount.objects.filter(pk=system.account.pk).update(request_blocked_until=until)
    with pytest.raises(ValidationError, match="provider delay changed"):
        clear_provider_cooldown(
            system.admin,
            system.account.pk,
            system.account.credential_revision,
            expected_blocked_until=None,
        )
    assert ProviderAccount.objects.get(pk=system.account.pk).request_blocked_until == until
    assert not AuditEvent.objects.filter(action="provider_cooldown_cleared").exists()
