import json
import os
import shutil
import subprocess
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from allauth.account.models import EmailAddress
from allauth.mfa.models import Authenticator
from allauth.mfa.totp.internal.auth import generate_totp_secret
from django.contrib.auth.models import User
from django.db import transaction
from django.test import override_settings
from django.utils import timezone

from pyracast.accounts.adapters import AccountAdapter
from pyracast.accounts.mfa import MFAAdapter
from pyracast.accounts.models import Membership, Tenant
from pyracast.calculation import calculate
from pyracast.connections.operations import validate_binding
from pyracast.forecasts.jobs import collect
from pyracast.forecasts.models import CollectionJob, CollectionSchedule
from pyracast.notifications.models import (
    NotificationDelivery,
    NotificationSettings,
    ReminderSchedule,
)
from pyracast.plants.models import CalibrationRun, Plant, ProductionObservation
from pyracast.plants.sites import submit_site
from pyracast.providers import FixtureProvider
from pyracast.regions.operations import (
    approve_region,
    configure_binding,
    request_location_change,
    submit_region,
)
from tests.test_domain import FakeAdapter, make_system
from tests.test_plant_operations import publish_snapshot, snapshot_for

pytestmark = [
    pytest.mark.django_db(transaction=True, databases=["default", "auth_cache"]),
    pytest.mark.skipif(
        os.environ.get("PYRACAST_BROWSER_TEST") != "1",
        reason="requires PYRACAST_BROWSER_TEST=1, Node dependencies, and Playwright Chromium",
    ),
]


def test_browser_against_live_django(
    live_server: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    npm = shutil.which("npm.cmd" if os.name == "nt" else "npm")
    assert npm is not None, "Node and npm are required for the browser integration test"
    system = make_system()
    password = "browser-test-" + "x" * 40
    system.member.set_password(password)
    system.member.email = "member@example.com"
    system.member.save(update_fields=["password", "email"])
    EmailAddress.objects.create(
        user=system.member, email=system.member.email, primary=True, verified=True
    )
    admin_secret = generate_totp_secret()
    system.admin.is_staff = True
    system.admin.email = "browser-admin@example.com"
    system.admin.set_password(password)
    system.admin.save(update_fields=["is_staff", "email", "password"])
    EmailAddress.objects.create(
        user=system.admin, email=system.admin.email, primary=True, verified=True
    )
    Authenticator.objects.create(
        user=system.admin, type="totp", data={"secret": MFAAdapter().encrypt(admin_secret)}
    )
    mailbox = tmp_path / "auth-mailbox.json"
    mailbox.write_text("[]", encoding="utf-8")
    messages: list[dict[str, str]] = []
    send_mail = AccountAdapter.send_mail

    def capture_mail(
        adapter: AccountAdapter, template_prefix: str, email: str, context: dict[str, Any]
    ) -> None:
        send_mail(adapter, template_prefix, email, context)
        code = context.get("code")
        if isinstance(code, str):

            def capture_code() -> None:
                messages.append({"email": email, "template": template_prefix, "code": code})
                mailbox.write_text(json.dumps(messages), encoding="utf-8")

            transaction.on_commit(capture_code)

    monkeypatch.setattr(AccountAdapter, "send_mail", capture_mail)
    now = timezone.now()
    validate_binding(system.admin, system.binding.pk, FakeAdapter(), now)
    job = approve_region(system.admin, system.region.pk, system.binding.pk, now)
    assert collect(job.pk, FixtureProvider(), calculate, timezone.now())
    publish_snapshot(snapshot_for(system, job.target_date - timedelta(days=1)))
    history_date = job.target_date - timedelta(days=2)
    history_job = CollectionJob.objects.create(
        binding=system.binding,
        endpoint="estimated_actuals",
        target_date=history_date,
        scheduled_slot=now,
        available_at=now,
    )
    assert collect(history_job.pk, FixtureProvider(), calculate, timezone.now())

    outsider = User.objects.create_user(username="other")
    other_tenant = Tenant.objects.create(name="Other")
    Membership.objects.create(user=outsider, tenant=other_tenant, role=Membership.Role.ADMIN)
    other_region = submit_region(
        outsider,
        other_tenant.pk,
        name="Private",
        latitude=40,
        longitude=29,
        timezone="Europe/Istanbul",
    )
    review_region = submit_site(
        outsider,
        other_tenant.pk,
        name="Review solar plant",
        latitude=39.5,
        longitude=28.5,
        timezone="Europe/Istanbul",
        dc_capacity_mw=12,
        inverter_capacity_mw=10,
        mounting_type="fixed",
        surface_tilt=25,
        surface_azimuth=180,
    )
    review_binding = configure_binding(
        system.admin,
        review_region.pk,
        provider_account_id=system.account.pk,
        site_id="review-plant",
        capacity_kw=1,
        capacity_semantics_verified=True,
    )
    validate_binding(system.admin, review_binding.pk, FakeAdapter(), now)
    location_change = request_location_change(
        outsider,
        review_region.pk,
        other_tenant.pk,
        expected_revision=review_region.revision,
        latitude=39.6,
        longitude=28.6,
        timezone="Europe/Istanbul",
        reason="Confirm the revised survey coordinates.",
    )
    review_reminder = ReminderSchedule.objects.create(
        configured_by=outsider,
        region=review_region,
        recipients=["operations-recipient@example.test"],
        times=["10:00"],
        effective_from=now,
    )
    NotificationDelivery.objects.create(
        schedule=review_reminder,
        recipient="operations-recipient@example.test",
        status="uncertain",
        scheduled_for=now - timedelta(minutes=5),
        available_at=now,
        attempts=1,
        last_error="smtp_uncertain",
    )
    env = os.environ | {
        "DJANGO_API_ORIGIN": live_server.url,
        "BROWSER_EMAIL": system.member.email,
        "BROWSER_PASSWORD": password,
        "BROWSER_SECURITY_EMAIL": "security@example.com",
        "BROWSER_SECURITY_PASSWORD": password,
        "BROWSER_AUTH_MAILBOX": str(mailbox),
        "BROWSER_ADMIN_EMAIL": system.admin.email,
        "BROWSER_ADMIN_PASSWORD": password,
        "BROWSER_ADMIN_TOTP_SEED": admin_secret,
        "BROWSER_ADMIN_ORIGIN": live_server.url,
        "BROWSER_REVIEW_REGION": str(review_region.pk),
        "BROWSER_DATE": job.target_date.isoformat(),
        "BROWSER_TODAY": (job.target_date - timedelta(days=1)).isoformat(),
        "BROWSER_HISTORY_DATE": history_date.isoformat(),
        "BROWSER_OTHER_TENANT": str(other_tenant.pk),
        "BROWSER_OTHER_REGION": str(other_region.pk),
    }
    with override_settings(
        CSRF_TRUSTED_ORIGINS=["http://127.0.0.1:3101"],
        SESSION_COOKIE_SECURE=False,
        CSRF_COOKIE_SECURE=False,
        SESSION_COOKIE_NAME="sessionid",
        CSRF_COOKIE_NAME="csrftoken",
        PYRACAST_PUBLIC_ORIGIN="http://127.0.0.1:3101",
        MAILERS={"default": {"BACKEND": "django.core.mail.backends.locmem.EmailBackend"}},
        SECURE_SSL_REDIRECT=False,
    ):
        subprocess.run(  # noqa: S603 -- npm is resolved locally and arguments are fixed.
            [npm, "run", "test:integration", "--prefix", "web"],
            cwd=Path(__file__).resolve().parent.parent,
            env=env,
            check=True,
            timeout=240,
        )
    plant = Plant.objects.select_related("current_configuration").get(
        region=system.region, name="Browser plant"
    )
    assert plant.current_configuration is not None
    assert plant.current_configuration.dc_capacity_mw == 130
    assert plant.current_configuration.inverter_capacity_mw == 100
    assert plant.current_configuration.export_limit_mw is None
    assert plant.current_configuration.revision == 2
    assert plant.current_configuration.mounting_type == "fixed"
    assert plant.current_configuration.surface_tilt == 33
    assert plant.current_configuration.surface_azimuth == 180
    assert plant.deleted_at is None
    system.region.refresh_from_db()
    assert system.region.deleted_at is None
    observation = ProductionObservation.objects.get(plant=plant)
    assert observation.energy_mwh == 1.25
    assert observation.configuration_id != plant.current_configuration_id
    assert observation.configuration.revision == 1
    assert not CalibrationRun.objects.filter(plant=plant).exists()
    schedule = CollectionSchedule.objects.get(region=system.region)
    assert schedule.enabled
    assert schedule.times == ["09:00", "16:00"]
    reminder = ReminderSchedule.objects.get(configured_by=system.member, region=system.region)
    assert reminder.enabled
    assert reminder.recipients == ["forecast-one@example.test", "forecast-two@example.test"]
    assert reminder.times == ["10:00"]
    review_region.refresh_from_db()
    location_change.refresh_from_db()
    assert review_region.status == "approved"
    assert location_change.status == "rejected"
    assert NotificationSettings.objects.get().max_recipients == 6
