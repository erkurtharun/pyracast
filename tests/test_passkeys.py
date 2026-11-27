import secrets
from typing import Any

import pytest
from allauth.mfa.models import Authenticator
from allauth.mfa.webauthn.internal.auth import STATE_SESSION_KEY
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from django.contrib.auth.models import User
from django.test import Client, override_settings
from fido2.cose import CoseKey
from fido2.utils import sha256, websafe_decode
from fido2.webauthn import (
    Aaguid,
    AttestationObject,
    AttestedCredentialData,
    AuthenticationResponse,
    AuthenticatorAssertionResponse,
    AuthenticatorAttestationResponse,
    AuthenticatorData,
    CollectedClientData,
    RegistrationResponse,
)

from pyracast.accounts.models import Membership, Tenant, WorkspaceRole
from tests.support.account_sessions import account, browser, session_user, signed_in, with_totp
from tests.support.auth_lifecycle import AUTH, PASSWORD, submit

pytestmark = pytest.mark.django_db(transaction=True, databases=["default", "auth_cache"])

MANAGE = "account/authenticators/webauthn"
LOGIN = "auth/webauthn/login"
SECOND = "auth/webauthn/authenticate"
CONFIRM = "auth/webauthn/reauthenticate"
ORIGIN = "https://testserver"


class PasskeyDevice:
    def __init__(self) -> None:
        self.key = ec.generate_private_key(ec.SECP256R1())
        self.identifier = secrets.token_bytes(32)
        self.user_handle = b""

    def enroll(self, client: Client) -> Any:
        options = client.get(AUTH + MANAGE).json()["data"]["creation_options"]["publicKey"]
        assert options["authenticatorSelection"]["residentKey"] == "required"
        assert options["authenticatorSelection"]["userVerification"] == "required"
        self.user_handle = websafe_decode(options["user"]["id"])
        public_key = CoseKey.for_name("ES256").from_cryptography_key(self.key.public_key())
        attested = AttestedCredentialData.create(Aaguid.NONE, self.identifier, public_key)
        data = AuthenticatorData.create(
            sha256(options["rp"]["id"].encode()),
            AuthenticatorData.FLAG.UP | AuthenticatorData.FLAG.UV | AuthenticatorData.FLAG.AT,
            0,
            attested,
        )
        credential = RegistrationResponse(
            raw_id=self.identifier,
            response=AuthenticatorAttestationResponse(
                client_data=CollectedClientData.create(
                    "webauthn.create", options["challenge"], ORIGIN
                ),
                attestation_object=AttestationObject.create("none", data, {}),
            ),
        )
        return submit(client, MANAGE, {"name": "My phone", "credential": dict(credential)})

    def verify(self, client: Client, path: str) -> Any:
        options = client.get(AUTH + path).json()["data"]["request_options"]["publicKey"]
        assert options["userVerification"] == "required"
        client_data = CollectedClientData.create("webauthn.get", options["challenge"], ORIGIN)
        data = AuthenticatorData.create(
            sha256(options["rpId"].encode()),
            AuthenticatorData.FLAG.UP | AuthenticatorData.FLAG.UV,
            0,
        )
        credential = AuthenticationResponse(
            raw_id=self.identifier,
            response=AuthenticatorAssertionResponse(
                client_data=client_data,
                authenticator_data=data,
                user_handle=self.user_handle,
                signature=self.key.sign(data + client_data.hash, ec.ECDSA(hashes.SHA256())),
            ),
        )
        return submit(client, path, {"credential": dict(credential)})


def remove(client: Client, key_id: int) -> Any:
    token = client.get("/api/session/").json()["csrf_token"]
    return client.delete(
        AUTH + MANAGE,
        {"authenticators": [key_id]},
        content_type="application/json",
        HTTP_X_CSRFTOKEN=token,
    )


def test_discoverable_sign_in_second_factor_step_up_and_revocation() -> None:
    user = account("owner")
    other_session = signed_in(user)
    enrolling = signed_in(user, reauthenticated=True)
    device = PasskeyDevice()
    added = device.enroll(enrolling)
    assert added.status_code == 200
    assert len(added.json()["meta"]["recovery_codes"]) == 10
    assert session_user(other_session) is None
    key_id = added.json()["data"]["id"]
    assert enrolling.get(AUTH + MANAGE).status_code == 401
    assert device.verify(enrolling, CONFIRM).status_code == 200
    second_key = PasskeyDevice().enroll(enrolling)
    assert second_key.status_code == 200
    assert "recovery_codes" not in second_key.json().get("meta", {})

    customer = browser()
    assert device.verify(customer, LOGIN).status_code == 200
    assert session_user(customer) == user.pk
    workspace = Tenant.objects.create(name="My workspace")
    Membership.objects.create(user=user, tenant=workspace, role=WorkspaceRole.ADMIN)
    other = Tenant.objects.create(name="Another customer")
    assert customer.get(f"/api/tenants/{workspace.pk}/members/").status_code == 200
    assert customer.get(f"/api/tenants/{other.pk}/members/").status_code == 403
    assert customer.get(AUTH + LOGIN).status_code == 409
    assert remove(customer, key_id).status_code == 401
    assert device.verify(customer, CONFIRM).status_code == 200

    password_login = browser()
    pending = submit(password_login, "auth/login", {"email": user.email, "password": PASSWORD})
    assert pending.status_code == 401
    assert password_login.get(AUTH + LOGIN).status_code == 409
    assert device.verify(password_login, SECOND).status_code == 200
    assert session_user(password_login) == user.pk
    assert remove(customer, key_id).status_code == 200
    assert session_user(password_login) is None
    assert device.verify(browser(), LOGIN).status_code == 400


def test_passkeys_require_fresh_proof_and_verified_customer_and_never_open_console() -> None:
    user = account("customer")
    client = signed_in(user)
    assert client.get(AUTH + MANAGE).status_code == 401
    assert submit(client, "auth/reauthenticate", {"password": PASSWORD}).status_code == 200
    device = PasskeyDevice()
    assert device.enroll(client).status_code == 200
    assert not client.get("/api/session/").json()["user"]["can_admin"]

    User.objects.filter(pk=user.pk).update(is_staff=True, is_superuser=True)
    assert device.verify(browser(), LOGIN).status_code == 400
    assert not client.get("/api/session/").json()["user"]["can_admin"]
    assert client.get(AUTH + CONFIRM).status_code == 403
    with_totp(user)
    admin = browser()
    assert (
        submit(admin, "auth/login", {"email": user.email, "password": PASSWORD}).status_code == 401
    )
    assert admin.get(AUTH + SECOND).status_code == 403
    client.force_login(User.objects.get(pk=user.pk))
    assert client.get(AUTH + MANAGE).status_code == 403

    unverified = account("unverified", verified=False)
    client.force_login(unverified)
    assert client.get(AUTH + MANAGE).status_code == 403
    assert not Authenticator.objects.filter(user=unverified).exists()


def test_anonymous_options_are_scoped_to_the_public_origin_and_attempt() -> None:
    client = browser()
    with override_settings(PYRACAST_PUBLIC_ORIGIN=""):
        assert client.get(AUTH + LOGIN).status_code == 403
    options = client.get(AUTH + LOGIN).json()["data"]["request_options"]["publicKey"]
    assert options["rpId"] == "testserver"
    assert options.get("allowCredentials", []) == []
    assert client.post(AUTH + LOGIN, {}, content_type="application/json").status_code == 403
    assert submit(client, LOGIN, {"credential": {"response": {}}}).status_code == 400
    assert submit(client, LOGIN, {}).status_code == 409
    client.get(AUTH + LOGIN)
    state = client.session
    state[STATE_SESSION_KEY]["expires"] = 0
    state.save()
    assert submit(client, LOGIN, {}).status_code == 409
    client.get(AUTH + LOGIN)
    other_origin = CollectedClientData.create(
        "webauthn.get", options["challenge"], "https://other.test"
    )
    response = submit(
        client, LOGIN, {"credential": {"response": {"clientDataJSON": other_origin.b64}}}
    )
    assert response.status_code == 400
    assert session_user(client) is None
