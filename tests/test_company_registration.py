from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from threading import Barrier
from types import SimpleNamespace
from typing import Any

import pytest
from allauth.account.internal.flows.email_verification import (
    consume_email_verification_rate_limit,
)
from allauth.account.internal.flows.email_verification_by_code import EmailVerificationProcess
from allauth.account.models import EmailAddress
from django.contrib.auth.hashers import BasePasswordHasher, get_hasher
from django.contrib.auth.models import User
from django.core import mail
from django.db import IntegrityError, connections
from django.http import HttpRequest
from django.test import Client, override_settings

from pyracast.accounts import signup as signup_view
from pyracast.accounts.adapters import AccountAdapter
from pyracast.accounts.invitations import invite_workspace_member
from pyracast.accounts.models import Membership, Tenant, WorkspaceRole
from pyracast.accounts.signup import CompanySignupInput
from tests.support.account_sessions import capture_security_events
from tests.support.auth_lifecycle import submit
from tests.support.company_registration import AUTH, DETAILS, PASSWORD, mailed_code, signup
from tests.support.workspaces import invitation_token

pytestmark = pytest.mark.django_db(transaction=True, databases=["default", "auth_cache"])


def test_signup_requires_verification_before_tenant_access() -> None:
    other = Tenant.objects.create(name="Another workspace")
    client = Client(enforce_csrf_checks=True)
    assert (
        client.post(AUTH + "auth/signup", DETAILS, content_type="application/json").status_code
        == 403
    )
    response = signup(client)
    assert response.status_code == 401
    assert not response.json()["meta"]["is_authenticated"]
    assert any(flow["id"] == "verify_email" for flow in response.json()["data"]["flows"])
    user = User.objects.get(email="owner@example.com")
    assert user.check_password(PASSWORD)
    assert not user.is_staff
    assert not user.is_superuser
    membership = Membership.objects.get(user=user)
    assert membership.tenant.name == DETAILS["company_name"]
    assert membership.tenant_id != other.pk
    assert membership.role == WorkspaceRole.ADMIN
    assert not EmailAddress.objects.get(user=user).verified
    assert client.get("/api/session/").json()["user"] is None
    assert client.get("/api/tenants/").status_code in {401, 403}
    assert len(mail.outbox) == 1

    code = mailed_code()
    outsider = Client(enforce_csrf_checks=True)
    outsider_token = outsider.get("/api/session/").json()["csrf_token"]
    assert (
        outsider.post(
            AUTH + "auth/email/verify",
            {"key": code},
            content_type="application/json",
            HTTP_X_CSRFTOKEN=outsider_token,
        ).status_code
        == 409
    )
    assert not EmailAddress.objects.get(user=user).verified
    token = client.get("/api/session/").json()["csrf_token"]
    confirmed = client.post(
        AUTH + "auth/email/verify",
        {"key": code},
        content_type="application/json",
        HTTP_X_CSRFTOKEN=token,
    )
    assert confirmed.status_code == 200
    assert EmailAddress.objects.get(user=user).verified
    assert confirmed.json()["meta"]["is_authenticated"]
    token = client.get("/api/session/").json()["csrf_token"]
    assert client.delete(AUTH + "auth/session", HTTP_X_CSRFTOKEN=token).status_code == 401
    token = client.get("/api/session/").json()["csrf_token"]
    login = client.post(
        AUTH + "auth/login",
        {"email": "OWNER@example.com", "password": PASSWORD},
        content_type="application/json",
        HTTP_X_CSRFTOKEN=token,
    )
    assert login.status_code == 200
    assert login.json()["meta"]["is_authenticated"]
    assert client.get("/api/tenants/").json()["results"] == [
        {
            "id": membership.tenant_id,
            "name": DETAILS["company_name"],
            "role": "admin",
            "pending_review_count": 0,
            "unread_outcome_count": 0,
        }
    ]
    assert client.get(f"/api/tenants/{other.pk}/sites/").status_code == 403


@pytest.mark.parametrize(
    "overrides",
    [
        {"company_name": " "},
        {"company_name": "x" * 201},
        {"email": "invalid"},
        {"password": "aB!57mQ8nP4"},
    ],
)
def test_registration_rejects_invalid_input_without_partial_records(
    overrides: dict[str, Any],
) -> None:
    response = signup(Client(enforce_csrf_checks=True), overrides)
    assert response.status_code == 400
    assert not User.objects.exists()
    assert not Tenant.objects.exists()
    assert not Membership.objects.exists()
    assert not EmailAddress.objects.exists()


def test_registration_accepts_a_fifteen_character_password() -> None:
    response = signup(Client(enforce_csrf_checks=True), {"password": "aB!57mQ8nP4zK2w"})

    assert response.status_code == 401
    assert any(flow["id"] == "verify_email" for flow in response.json()["data"]["flows"])
    user = User.objects.get(email="owner@example.com")
    assert user.check_password("aB!57mQ8nP4zK2w")
    assert not EmailAddress.objects.get(user=user).verified


@override_settings(ACCOUNT_RATE_LIMITS={"confirm_email": "100/m/key"})
def test_duplicate_signup_does_not_disclose_or_join_existing_account() -> None:
    client = Client(enforce_csrf_checks=True)
    first = signup(client)
    user = User.objects.get()
    response = signup(Client(enforce_csrf_checks=True), {"company_name": "Attacker company"})
    assert response.status_code == first.status_code == 401
    assert response.json()["data"]["flows"] == first.json()["data"]["flows"]
    assert User.objects.count() == Tenant.objects.count() == Membership.objects.count() == 1
    assert Membership.objects.get().user == user
    assert user.check_password(PASSWORD)


def test_signup_rolls_back_user_tenant_and_email_if_membership_write_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail(**kwargs: Any) -> None:
        raise IntegrityError("membership write failed")

    monkeypatch.setattr(Membership.objects, "create", fail)
    with pytest.raises(IntegrityError, match="membership write failed"):
        signup(Client(enforce_csrf_checks=True))
    assert not User.objects.exists()
    assert not Tenant.objects.exists()
    assert not EmailAddress.objects.exists()


def test_signup_preserves_password_whitespace_and_bounds_hash_input() -> None:
    client = Client(enforce_csrf_checks=True)
    password = "  " + PASSWORD + "  "
    assert signup(client, {"password": password}).status_code == 401
    user = User.objects.get()
    assert user.check_password(password)
    assert not user.check_password(password.strip())
    assert (
        signup(
            Client(enforce_csrf_checks=True),
            {"email": "another@example.com", "password": "x" * 1025},
        ).status_code
        == 400
    )
    assert User.objects.count() == 1


def test_signup_code_attempt_limit_rejects_the_correct_code_after_abort() -> None:
    client = Client(enforce_csrf_checks=True)
    assert signup(client).status_code == 401
    code = mailed_code()
    token = client.get("/api/session/").json()["csrf_token"]
    for _ in range(3):
        assert (
            client.post(
                AUTH + "auth/email/verify",
                {"key": "WRONG-CODE"},
                content_type="application/json",
                HTTP_X_CSRFTOKEN=token,
            ).status_code
            == 400
        )
    assert (
        client.post(
            AUTH + "auth/email/verify",
            {"key": code},
            content_type="application/json",
            HTTP_X_CSRFTOKEN=token,
        ).status_code
        == 409
    )
    assert not EmailAddress.objects.get().verified


def test_concurrent_signup_for_one_mailbox_can_be_recovered_by_its_owner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    validation = Barrier(2)
    address_save = Barrier(2)
    User.objects.create_user(username="owner")
    clean_email = CompanySignupInput.clean_email
    save = EmailAddress.save

    # A username collision would block the second transaction before the email barrier.
    def distinct_username(adapter: AccountAdapter, request: HttpRequest, user: User) -> None:
        user.username = "mailbox-race-" + request.headers["X-Registration-Order"]

    def synchronized_email(form: CompanySignupInput) -> str:
        email: str = clean_email(form)
        validation.wait(timeout=10)
        return email

    def synchronized_address_save(address: EmailAddress, *args: Any, **kwargs: Any) -> None:
        if address._state.adding and not address.verified:
            address_save.wait(timeout=10)
        save(address, *args, **kwargs)

    monkeypatch.setattr(CompanySignupInput, "clean_email", synchronized_email)
    monkeypatch.setattr(AccountAdapter, "populate_username", distinct_username)
    monkeypatch.setattr(EmailAddress, "save", synchronized_address_save)

    def register(order: str) -> int:
        client = Client(enforce_csrf_checks=True)
        try:
            token = client.get("/api/session/").json()["csrf_token"]
            return client.post(
                AUTH + "auth/signup",
                DETAILS | {"company_name": f"Mailbox race {order}"},
                content_type="application/json",
                HTTP_X_CSRFTOKEN=token,
                HTTP_X_REGISTRATION_ORDER=order,
            ).status_code
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=2) as pool:
        statuses = list(pool.map(register, ("first", "second")))
    assert all(status in {401, 429} for status in statuses)
    assert User.objects.count() == 3
    assert set(User.objects.exclude(username="owner").values_list("username", flat=True)) == {
        "mailbox-race-first",
        "mailbox-race-second",
    }
    assert EmailAddress.objects.filter(primary=True, verified=False).count() == 2
    assert Tenant.objects.count() == Membership.objects.count() == 2
    assert not EmailAddress.objects.filter(verified=True).exists()
    owner = Client(enforce_csrf_checks=True)
    assert (
        submit(owner, "auth/login", {"email": DETAILS["email"], "password": PASSWORD}).status_code
        == 400
    )
    mail.outbox.clear()
    assert submit(owner, "auth/password/request", {"email": DETAILS["email"]}).status_code == 401
    code = mailed_code()
    assert (
        submit(
            owner, "auth/password/reset", {"key": code, "password": PASSWORD + "owned"}
        ).status_code
        == 401
    )
    identity = EmailAddress.objects.get(verified=True)
    assert identity.user_id == min(EmailAddress.objects.values_list("user_id", flat=True))
    assert (
        submit(
            owner, "auth/login", {"email": DETAILS["email"], "password": PASSWORD + "owned"}
        ).status_code
        == 200
    )


def test_concurrent_signups_with_one_workspace_name_both_wait_for_verification(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ready = Barrier(2)
    clean_name = CompanySignupInput.clean_company_name

    def synchronized_name(form: CompanySignupInput) -> str:
        name = str(clean_name(form))
        ready.wait(timeout=15)
        return name

    monkeypatch.setattr(CompanySignupInput, "clean_company_name", synchronized_name)

    def register(order: int) -> int:
        try:
            return int(
                signup(
                    Client(enforce_csrf_checks=True), {"email": f"owner-{order}@example.com"}
                ).status_code
            )
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=2) as pool:
        statuses = list(pool.map(register, (1, 2)))
    assert statuses == [401, 401]
    assert User.objects.count() == Tenant.objects.count() == Membership.objects.count() == 2
    assert set(Tenant.objects.values_list("name", flat=True)) == {DETAILS["company_name"]}


@override_settings(ACCOUNT_RATE_LIMITS={"confirm_email": "100/m/key"})
@pytest.mark.parametrize("address_has_account", [False, True])
def test_a_signup_answers_alike_for_a_reused_and_a_fresh_workspace_name(
    address_has_account: bool,
) -> None:
    if address_has_account:
        existing = signup(Client(enforce_csrf_checks=True), {"email": "first@example.com"})
        assert existing.status_code == 401
    first = signup(
        Client(enforce_csrf_checks=True),
        {"email": "first@example.com", "company_name": "Harbor Solar"},
    )
    reused = signup(
        Client(enforce_csrf_checks=True),
        {"email": "second@example.com", "company_name": " HARBOR  solar "},
    )
    fresh = signup(
        Client(enforce_csrf_checks=True),
        {"email": "third@example.com", "company_name": "Fresh Solar"},
    )

    assert first.status_code == reused.status_code == fresh.status_code == 401
    assert first.json() == reused.json() == fresh.json()
    reused_owner = Membership.objects.get(user__email="second@example.com")
    assert (reused_owner.tenant.name, reused_owner.role) == ("HARBOR solar", WorkspaceRole.ADMIN)
    assert Membership.objects.filter(tenant=reused_owner.tenant).count() == 1


@override_settings(ACCOUNT_RATE_LIMITS={"confirm_email": "100/m/key"})
@pytest.mark.parametrize("address_has_account", [False, True])
def test_a_signup_hashes_its_password_once_whether_or_not_the_address_has_an_account(
    address_has_account: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    if address_has_account:
        assert signup(Client(enforce_csrf_checks=True)).status_code == 401
    hasher = type(get_hasher())
    encode = hasher.encode
    hashed: list[str] = []

    def counted(self: BasePasswordHasher, password: str, salt: str) -> str:
        hashed.append(password)
        return encode(self, password, salt)

    monkeypatch.setattr(hasher, "encode", counted)

    response = signup(Client(enforce_csrf_checks=True))

    assert response.status_code == 401
    assert hashed == [PASSWORD]
    assert User.objects.count() == 1


@dataclass
class AnswerClock:
    readings: Iterator[float]
    waits: list[float] = field(default_factory=list)

    def monotonic(self) -> float:
        return next(self.readings)

    def sleep(self, seconds: float) -> None:
        self.waits.append(seconds)


def answer_clock(monkeypatch: pytest.MonkeyPatch, *readings: float) -> AnswerClock:
    clock = AnswerClock(iter(readings))
    monkeypatch.setattr(
        signup_view, "time", SimpleNamespace(monotonic=clock.monotonic, sleep=clock.sleep)
    )
    return clock


@override_settings(ACCOUNT_RATE_LIMITS={"confirm_email": "100/m/key"})
@pytest.mark.parametrize(("elapsed", "waits"), [(0.25, [0.75]), (1.0, []), (1.75, [])])
@pytest.mark.parametrize("address_has_account", [False, True])
def test_a_signup_answers_after_the_minimum_time_whether_or_not_the_address_has_an_account(
    address_has_account: bool,
    elapsed: float,
    waits: list[float],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if address_has_account:
        assert signup(Client(enforce_csrf_checks=True)).status_code == 401
    clock = answer_clock(monkeypatch, 100.0, 100.0 + elapsed)

    with override_settings(PYRACAST_SIGNUP_MINIMUM_ANSWER_SECONDS=1.0):
        response = signup(Client(enforce_csrf_checks=True))

    assert response.status_code == 401
    assert clock.waits == waits
    assert User.objects.count() == 1


def request_as_new_email(address: str, requester: str) -> None:
    email = f"{requester}@example.com"
    account = User.objects.create_user(username=requester, email=email, password=PASSWORD)
    EmailAddress.objects.create(user=account, email=email, primary=True, verified=True)
    client = Client(enforce_csrf_checks=True)
    login = submit(client, "auth/login", {"email": email, "password": PASSWORD})
    assert login.status_code == 200
    assert submit(client, "auth/reauthenticate", {"password": PASSWORD}).status_code == 200
    assert submit(client, "account/email", {"email": address}).status_code == 200


def test_a_signup_answers_alike_after_its_address_was_requested_as_a_new_email(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for address, verified in (("verified@example.com", True), ("unverified@example.com", False)):
        account = User.objects.create_user(username=address.partition("@")[0], email=address)
        EmailAddress.objects.create(user=account, email=address, primary=True, verified=verified)
    addresses = ["new@example.com", "verified@example.com", "unverified@example.com"]
    for index, address in enumerate(addresses):
        request_as_new_email(address, f"requester-{index}")
    mail.outbox.clear()
    clock = answer_clock(monkeypatch, 100.0, 100.25, 200.0, 200.25, 300.0, 300.25)

    with override_settings(PYRACAST_SIGNUP_MINIMUM_ANSWER_SECONDS=1.0):
        answers = [
            signup(Client(enforce_csrf_checks=True), {"email": email}) for email in addresses
        ]

    assert [answer.status_code for answer in answers] == [401, 401, 401]
    assert answers[0].json() == answers[1].json() == answers[2].json()
    assert clock.waits == [0.75, 0.75, 0.75]
    assert mail.outbox == []


def test_a_signup_that_meets_the_address_mail_limit_records_one_throttle_for_either_kind(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    known = User.objects.create_user(username="known", email="known@example.com")
    EmailAddress.objects.create(user=known, email=known.email, primary=True, verified=True)
    addresses = ["new@example.com", "known@example.com"]
    for index, address in enumerate(addresses):
        request_as_new_email(address, f"requester-{index}")
    events = capture_security_events(caplog, monkeypatch)
    records = []

    for address in addresses:
        caplog.clear()
        assert signup(Client(enforce_csrf_checks=True), {"email": address}).status_code == 401
        records.append(
            [event for event in events() if event["event"] == "authentication_request_throttled"]
        )

    throttled = {"event": "authentication_request_throttled", "user_id": None}
    assert records == [[throttled | {"client_ip": "127.0.0.1"}]] * 2


def test_a_signup_whose_address_mail_limit_is_met_meanwhile_records_one_throttle_for_either_kind(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    known = User.objects.create_user(username="known", email="known@example.com")
    EmailAddress.objects.create(user=known, email=known.email, primary=True, verified=True)
    events = capture_security_events(caplog, monkeypatch)
    send = EmailVerificationProcess.send

    def limit_met_meanwhile(self: EmailVerificationProcess, **kwargs: Any) -> None:
        assert consume_email_verification_rate_limit(self.request, self.email)
        send(self, **kwargs)

    monkeypatch.setattr(EmailVerificationProcess, "send", limit_met_meanwhile)
    records = []

    for address in ["new@example.com", "known@example.com"]:
        caplog.clear()
        assert signup(Client(enforce_csrf_checks=True), {"email": address}).status_code == 401
        records.append(
            [event for event in events() if event["event"] == "authentication_request_throttled"]
        )

    throttled = {"event": "authentication_request_throttled", "user_id": None}
    assert records == [[throttled | {"client_ip": "127.0.0.1"}]] * 2
    assert mail.outbox == []


@pytest.mark.parametrize(
    "body",
    [
        pytest.param(DETAILS | {"email": "invalid"}, id="address"),
        pytest.param(DETAILS | {"password": "aB!57mQ8nP4"}, id="password"),
        pytest.param("not json", id="body"),
    ],
)
def test_an_invalid_signup_answers_after_the_minimum_time(
    body: dict[str, str] | str, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = Client(enforce_csrf_checks=True)
    token = client.get("/api/session/").json()["csrf_token"]
    clock = answer_clock(monkeypatch, 100.0, 100.25)

    with override_settings(PYRACAST_SIGNUP_MINIMUM_ANSWER_SECONDS=1.0):
        response = client.post(
            AUTH + "auth/signup", body, content_type="application/json", HTTP_X_CSRFTOKEN=token
        )

    assert response.status_code == 400
    assert clock.waits == [0.75]
    assert not User.objects.exists()


@pytest.mark.parametrize("refusal", ["missing_token", "untrusted_origin"])
def test_a_csrf_refused_signup_answers_after_the_minimum_time(
    refusal: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = Client(enforce_csrf_checks=True)
    headers = {}
    if refusal == "untrusted_origin":
        headers = {
            "HTTP_X_CSRFTOKEN": client.get("/api/session/").json()["csrf_token"],
            "HTTP_ORIGIN": "https://untrusted.example",
        }
    clock = answer_clock(monkeypatch, 100.0, 100.25)

    with override_settings(PYRACAST_SIGNUP_MINIMUM_ANSWER_SECONDS=1.0):
        response = client.post(
            AUTH + "auth/signup", DETAILS, content_type="application/json", **headers
        )

    assert response.status_code == 403
    assert clock.waits == [0.75]
    assert not User.objects.exists()


def test_a_failed_signup_answers_after_the_minimum_time(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(**kwargs: Any) -> None:
        raise IntegrityError("membership write failed")

    monkeypatch.setattr(Membership.objects, "create", fail)
    clock = answer_clock(monkeypatch, 100.0, 100.25)

    with (
        override_settings(PYRACAST_SIGNUP_MINIMUM_ANSWER_SECONDS=1.0),
        pytest.raises(IntegrityError, match="membership write failed"),
    ):
        signup(Client(enforce_csrf_checks=True))

    assert clock.waits == [0.75]


@override_settings(
    ACCOUNT_RATE_LIMITS={"signup": "1/h/ip"}, PYRACAST_SIGNUP_MINIMUM_ANSWER_SECONDS=1.0
)
def test_a_rate_limited_signup_answers_at_once(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = answer_clock(monkeypatch, 100.0, 100.25, 200.0, 200.25)

    admitted = signup(Client(enforce_csrf_checks=True))
    refused = signup(Client(enforce_csrf_checks=True), {"email": "second@example.com"})

    assert (admitted.status_code, refused.status_code) == (401, 429)
    assert clock.waits == [0.75]


def test_the_signup_route_answers_other_methods_at_once(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = answer_clock(monkeypatch)

    with override_settings(PYRACAST_SIGNUP_MINIMUM_ANSWER_SECONDS=1.0):
        response = Client().get(AUTH + "auth/signup")

    assert response.status_code == 405
    assert clock.waits == []


@override_settings(ACCOUNT_RATE_LIMITS={"confirm_email": "100/m/key"})
@pytest.mark.parametrize(
    ("address_has_account", "template"),
    [
        (False, "account/email/email_confirmation_signup"),
        (True, "account/email/account_already_exists"),
    ],
)
def test_a_signup_waits_after_its_records_are_committed_and_its_mail_is_handed_over(
    address_has_account: bool, template: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    if address_has_account:
        assert signup(Client(enforce_csrf_checks=True)).status_code == 401
    mail.outbox.clear()
    observed: list[tuple[list[str], list[str], object]] = []

    def sleep(seconds: float) -> None:
        # The authentication cache has its own session, so it sees only committed rows.
        with connections["auth_cache"].cursor() as cursor:
            cursor.execute("SELECT count(*) FROM auth_user WHERE email = %s", ["owner@example.com"])
            committed = cursor.fetchone()
        observed.append(
            (
                [alias for alias in connections if connections[alias].in_atomic_block],
                [message.extra_headers["X-Pyracast-Mail"] for message in mail.outbox],
                committed,
            )
        )

    readings = iter((100.0, 100.25))
    monkeypatch.setattr(
        signup_view, "time", SimpleNamespace(monotonic=readings.__next__, sleep=sleep)
    )

    with override_settings(PYRACAST_SIGNUP_MINIMUM_ANSWER_SECONDS=1.0):
        assert signup(Client(enforce_csrf_checks=True)).status_code == 401

    assert observed == [([], [template], (1,))]


def test_a_signup_waits_after_its_session_is_released(monkeypatch: pytest.MonkeyPatch) -> None:
    client = Client(enforce_csrf_checks=True)
    client.session.save()
    held: list[tuple[int]] = []

    def sleep(seconds: float) -> None:
        with connections["auth_cache"].cursor() as cursor:
            cursor.execute(
                "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND database = "
                "(SELECT oid FROM pg_database WHERE datname = current_database())"
            )
            held.append(cursor.fetchone())

    readings = iter((100.0, 100.25))
    monkeypatch.setattr(
        signup_view, "time", SimpleNamespace(monotonic=readings.__next__, sleep=sleep)
    )

    with override_settings(PYRACAST_SIGNUP_MINIMUM_ANSWER_SECONDS=1.0):
        assert signup(client).status_code == 401

    assert held == [(0,)]


@pytest.mark.parametrize(
    "name",
    [
        "Sunfield\nEnergy",
        "Sunfield​Energy",
        "Sunfield\u202eEnergy",
        "Visit https://sunfield.test now",
        "www.sunfield",
        "Sunfield.energy",
        "billing@sunfield",
    ],
)
def test_workspace_names_with_control_characters_or_addresses_are_refused(name: str) -> None:
    response = signup(Client(enforce_csrf_checks=True), {"company_name": name})

    assert response.status_code == 400
    assert response.json()["fields"].keys() == {"company_name"}
    assert not User.objects.exists()
    assert not Tenant.objects.exists()


def test_workspace_name_whitespace_is_collapsed() -> None:
    response = signup(
        Client(enforce_csrf_checks=True), {"company_name": "  Sunfield   Enerji  A.S. "}
    )

    assert response.status_code == 401
    assert Tenant.objects.get().name == "Sunfield Enerji A.S."


def test_a_password_resembling_the_address_is_refused() -> None:
    response = signup(
        Client(enforce_csrf_checks=True),
        {"email": "harvestmoonpanel@example.com", "password": "harvestmoonpanel"},
    )

    assert response.status_code == 400
    assert response.json()["fields"].keys() == {"password"}
    assert not User.objects.exists()


def test_concurrent_signups_sharing_a_mailbox_name_both_wait_for_verification(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidates = Barrier(2)
    generate = AccountAdapter.generate_unique_username

    def synchronized(
        adapter: AccountAdapter, txts: list[str | None], regex: str | None = None
    ) -> str:
        username = generate(adapter, txts, regex)
        candidates.wait(timeout=10)
        return username

    monkeypatch.setattr(AccountAdapter, "generate_unique_username", synchronized)

    def register(domain: str) -> int:
        try:
            return int(
                signup(
                    Client(enforce_csrf_checks=True),
                    {"email": f"info@{domain}", "company_name": f"Workspace {domain[0]}"},
                ).status_code
            )
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=2) as pool:
        statuses = list(pool.map(register, ("a.test", "b.test")))

    assert statuses == [401, 401]
    assert User.objects.count() == Tenant.objects.count() == 2


def test_a_signup_whose_random_username_part_repeats_still_gets_a_unique_username(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    User.objects.create_user(username="info-0a1b")
    monkeypatch.setattr("pyracast.accounts.adapters.secrets.token_hex", lambda size: "0a1b")

    response = signup(Client(enforce_csrf_checks=True), {"email": "info@example.com"})

    assert response.status_code == 401
    assert User.objects.filter(email="info@example.com").exclude(username="info-0a1b").exists()


@override_settings(
    MAILERS={"default": {"BACKEND": "django.core.mail.backends.locmem.EmailBackend"}}
)
def test_an_invited_signup_ignores_the_workspace_name() -> None:
    tenant = Tenant.objects.create(name="Sunfield Energy")
    administrator = User.objects.create_user(username="administrator", email="admin@example.com")
    Membership.objects.create(tenant=tenant, user=administrator, role=WorkspaceRole.ADMIN)
    invite_workspace_member(administrator, tenant.pk, "guest@example.com")

    response = signup(
        Client(enforce_csrf_checks=True),
        {
            "email": "guest@example.com",
            "company_name": "Sunfield Energy",
            "invitation_token": invitation_token(),
        },
    )

    assert response.status_code == 401
    assert Tenant.objects.count() == 1
    assert not Membership.objects.filter(user__email="guest@example.com").exists()
