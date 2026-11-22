import http.client
import json
import os
import shutil
import socket
import subprocess
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import pytest
from allauth.account.models import EmailAddress
from allauth.mfa.models import Authenticator
from allauth.mfa.recovery_codes.internal.auth import RecoveryCodes
from allauth.mfa.totp.internal.auth import generate_totp_secret
from django.contrib.auth.models import User
from django.db import transaction
from django.test import override_settings
from django.utils import timezone

from pyracast.accounts.adapters import AccountAdapter
from pyracast.accounts.mfa import MFAAdapter
from pyracast.accounts.models import Membership, Tenant, WorkspaceRole
from pyracast.calculation import calculate
from pyracast.forecasts.models import CollectionJob
from pyracast.forecasts.validation import validate_binding
from pyracast.notifications.models import NotificationDelivery, ReminderSchedule
from pyracast.plants.approval import approve_region
from pyracast.plants.parameters import PlantParameters
from pyracast.plants.sites import submit_site
from pyracast.providers import FixtureProvider
from pyracast.regions.models import Region, RegionLocationChangeRequest
from pyracast.regions.operations import configure_binding, request_location_change, submit_region
from tests.support.domain import FakeAdapter, System, make_system
from tests.support.gateway_routes import SITE, backend_route
from tests.support.jobs import collect
from tests.support.notifications import freeze, store
from tests.support.plant_operations import publish_snapshot, snapshot_for

ROOT = Path(__file__).resolve().parents[2]
PASSWORD = "browser-test-" + "x" * 40
# The integration config's globalTimeout stays below this, so Playwright reports a hung spec first.
SPEC_TIMEOUT_SECONDS = 300
# RFC 9110 section 7.6.1: these describe one connection; Content-Length is recomputed.
HOP_BY_HOP = frozenset(
    {
        "connection",
        "content-length",
        "keep-alive",
        "proxy-connection",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
    }
)


class Gateway(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, backend: str, frontend: str) -> None:
        super().__init__(("127.0.0.1", 0), Forward)
        self.backend = backend
        self.frontend = frontend
        self.routes_to_backend = backend_route()

    @property
    def origin(self) -> str:
        return f"http://127.0.0.1:{self.server_port}"

    def upstream(self, path: str) -> str:
        return self.backend if self.routes_to_backend(SITE, path) else self.frontend


class Forward(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server: Gateway

    def log_message(self, *_args: object) -> None:
        pass

    def forward(self) -> None:
        upstream = urlsplit(self.server.upstream(urlsplit(self.path).path))
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else None
        headers = {
            name: value for name, value in self.headers.items() if name.lower() not in HOP_BY_HOP
        }
        connection = http.client.HTTPConnection(str(upstream.hostname), upstream.port, timeout=120)
        try:
            connection.request(self.command, self.path, body=body, headers=headers)
            response = connection.getresponse()
            status, reason, payload = response.status, response.reason, response.read()
            fields = [
                (name, value)
                for name, value in response.getheaders()
                if name.lower() not in HOP_BY_HOP
            ]
            if any("\r" in part or "\n" in part for field in fields for part in field):
                status, reason, payload, fields = 502, "Bad Gateway", b"", []
        except OSError:
            status, reason, payload, fields = 502, "Bad Gateway", b"", []
        finally:
            connection.close()
        self.send_response_only(status, reason)
        for name, value in fields:
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(payload)

    def do_GET(self) -> None:
        self.forward()

    def do_HEAD(self) -> None:
        self.forward()

    def do_POST(self) -> None:
        self.forward()

    def do_PUT(self) -> None:
        self.forward()

    def do_PATCH(self) -> None:
        self.forward()

    def do_DELETE(self) -> None:
        self.forward()

    def do_OPTIONS(self) -> None:
        self.forward()


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port: int = probe.getsockname()[1]
        return port


@contextmanager
def serving(api: str) -> Iterator[Gateway]:
    gateway = Gateway(api, "http://127.0.0.1:9")
    thread = threading.Thread(target=gateway.serve_forever)
    thread.start()
    try:
        yield gateway
    finally:
        gateway.shutdown()
        gateway.server_close()
        thread.join(10)


@contextmanager
def live_settings(origin: str) -> Iterator[None]:
    with override_settings(
        CSRF_TRUSTED_ORIGINS=[origin],
        SESSION_COOKIE_SECURE=False,
        CSRF_COOKIE_SECURE=False,
        SESSION_COOKIE_NAME="sessionid",
        CSRF_COOKIE_NAME="csrftoken",
        PYRACAST_PUBLIC_ORIGIN=origin,
        SOCIALACCOUNT_PROVIDERS={
            provider: {
                "APP": {"client_id": f"browser-{provider}", "secret": "browser-secret"},
                "SCOPE": ["openid", "email"] if provider == "google" else ["User.Read"],
                "OAUTH_PKCE_ENABLED": True,
                "VERIFIED_EMAIL": False,
            }
            for provider in ("google", "microsoft")
        },
        MAILERS={"default": {"BACKEND": "django.core.mail.backends.locmem.EmailBackend"}},
        SECURE_SSL_REDIRECT=False,
    ):
        yield


def capture_codes(mailbox: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    mailbox.write_text("[]", encoding="utf-8")
    messages: list[dict[str, str]] = []
    send_mail = AccountAdapter.send_mail

    def capture(
        adapter: AccountAdapter, template_prefix: str, email: str, context: dict[str, Any]
    ) -> None:
        send_mail(adapter, template_prefix, email, context)
        code = context.get("code")
        if isinstance(code, str):

            def record() -> None:
                messages.append({"email": email, "template": template_prefix, "code": code})
                mailbox.write_text(json.dumps(messages), encoding="utf-8")

            transaction.on_commit(record)

    monkeypatch.setattr(AccountAdapter, "send_mail", capture)
    return mailbox


def run_spec(spec: str, gateway: Gateway, **environment: str) -> None:
    npm = shutil.which("npm.cmd" if os.name == "nt" else "npm")
    assert npm is not None, "Node.js and npm run the browser scenarios"
    port = free_port()
    gateway.frontend = f"http://127.0.0.1:{port}"
    subprocess.run(  # noqa: S603 -- npm is resolved locally and arguments are fixed.
        [npm, "run", "test:integration", "--prefix", "web", "--", f"integration/{spec}"],
        cwd=ROOT,
        env=os.environ
        | environment
        | {"BROWSER_ORIGIN": gateway.origin, "BROWSER_WEB_PORT": str(port)},
        check=True,
        timeout=SPEC_TIMEOUT_SECONDS,
    )


@dataclass(frozen=True)
class Accounts:
    system: System
    password: str
    admin_codes: str
    console: User
    console_codes: str


def with_second_factor(user: User) -> str:
    """Two recovery codes, comma-separated, of an account that also has an authenticator: a
    code minted from the clock can reach the server in the next step and be refused."""
    Authenticator.objects.create(
        user=user, type="totp", data={"secret": MFAAdapter().encrypt(generate_totp_secret())}
    )
    return ",".join(RecoveryCodes.activate(user).generate_codes()[:2])


def seed_accounts() -> Accounts:
    system = make_system()
    system.member.set_password(PASSWORD)
    system.member.email = "member@example.com"
    system.member.save(update_fields=["password", "email"])
    system.admin.email = "browser-admin@example.com"
    system.admin.set_password(PASSWORD)
    system.admin.save(update_fields=["email", "password"])
    console = User.objects.create_superuser(
        username="console-admin", email="console-admin@example.com", password=PASSWORD
    )
    for user in (system.member, system.admin, console):
        EmailAddress.objects.create(user=user, email=user.email, primary=True, verified=True)
    return Accounts(
        system, PASSWORD, with_second_factor(system.admin), console, with_second_factor(console)
    )


@dataclass(frozen=True)
class ForecastDays:
    now: datetime
    target: date
    today: date
    history: date


def seed_forecast_days(system: System) -> ForecastDays:
    now = timezone.now()
    validate_binding(system.admin, system.binding.pk, FakeAdapter(), now)
    job = approve_region(system.admin, system.region.pk, system.binding.pk, now).job
    assert collect(job.pk, FixtureProvider(), calculate, timezone.now())
    today = job.target_date - timedelta(days=1)
    publish_snapshot(snapshot_for(system, today))
    history = job.target_date - timedelta(days=2)
    history_job = CollectionJob.objects.create(
        binding=system.binding,
        endpoint="estimated_actuals",
        target_date=history,
        scheduled_for=now,
        available_at=now,
    )
    assert collect(history_job.pk, FixtureProvider(), calculate, timezone.now())
    return ForecastDays(now, job.target_date, today, history)


@dataclass(frozen=True)
class OtherWorkspace:
    tenant: Tenant
    owner: User
    removable: User
    region: Region


def seed_other_workspace() -> OtherWorkspace:
    owner = User.objects.create_user(username="other")
    tenant = Tenant.objects.create(name="Other")
    Membership.objects.create(user=owner, tenant=tenant, role=WorkspaceRole.ADMIN)
    removable = User.objects.create_user(username="removable", email="removable@example.test")
    Membership.objects.create(user=removable, tenant=tenant)
    region = submit_region(
        owner, tenant.pk, name="Private", latitude=40, longitude=29, timezone="Europe/Istanbul"
    )
    return OtherWorkspace(tenant, owner, removable, region)


@dataclass(frozen=True)
class Review:
    region: Region
    location_change: RegionLocationChangeRequest


def seed_review(accounts: Accounts, other: OtherWorkspace) -> Review:
    system = accounts.system
    now = timezone.now()
    region = submit_site(
        other.owner,
        other.tenant.pk,
        PlantParameters(
            name="Review solar plant",
            dc_capacity_mw=12,
            inverter_capacity_mw=10,
            mounting_type="fixed",
            surface_tilt_degrees=25,
            surface_azimuth_degrees=180,
        ),
        latitude=39.5,
        longitude=28.5,
        timezone="Europe/Istanbul",
    )
    binding = configure_binding(
        system.admin,
        region.pk,
        provider_account_id=system.account.pk,
        site_id="review-plant",
        capacity_kw=1,
        capacity_semantics_verified=True,
    )
    validate_binding(system.admin, binding.pk, FakeAdapter(), now)
    location_change = request_location_change(
        other.owner,
        region.pk,
        other.tenant.pk,
        expected_revision=region.revision,
        latitude=39.6,
        longitude=28.6,
        timezone="Europe/Istanbul",
        reason="Confirm the revised survey coordinates.",
    )
    reminder = ReminderSchedule.objects.create(
        configured_by=other.owner,
        region=region,
        times=["10:00"],
        effective_from=now,
    )
    store(reminder, ["operations-recipient@example.test"])
    uncertain = NotificationDelivery.objects.create(
        schedule=reminder,
        binding=binding,
        status="uncertain",
        scheduled_for=now - timedelta(minutes=5),
        preparation_deadline=now + timedelta(minutes=10),
        available_at=now,
        attempts=1,
        last_error="mail_outcome_uncertain",
    )
    freeze(uncertain, ["operations-recipient@example.test"])
    return Review(region, location_change)
