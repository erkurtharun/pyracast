import itertools

import pytest
from allauth.account.internal.flows.login import record_authentication
from allauth.mfa.models import Authenticator
from django.contrib.auth.models import User
from django.core.exceptions import PermissionDenied
from django.test import Client, RequestFactory

from pyracast.plants.approval import ReviewDecision, decide_review
from pyracast.regions.models import Region

pytestmark = pytest.mark.django_db

ROLES = list(itertools.product((True, False), repeat=4))


def _signed_in(user: User, *, mfa: bool) -> Client:
    client = Client()
    client.force_login(user)
    if mfa:
        authenticator = Authenticator.objects.create(user=user, type="totp", data={})
        request = RequestFactory().get("/admin/")
        request.user = user
        request.session = client.session
        record_authentication(request, user, method="mfa", type="totp", id=authenticator.pk)
        request.session.save()
    return client


def _operation_allowed(user: User) -> bool:
    try:
        decide_review(user, 0, ReviewDecision.SUSPEND)
    except PermissionDenied:
        return False
    except Region.DoesNotExist:
        return True
    raise AssertionError("A missing region was transitioned.")


@pytest.mark.parametrize(("active", "staff", "superuser", "mfa"), ROLES)
def test_console_session_and_operations_agree_on_the_platform_administrator(
    active: bool, staff: bool, superuser: bool, mfa: bool
) -> None:
    user = User.objects.create_user(
        username="operator", is_active=True, is_staff=staff, is_superuser=superuser
    )
    client = _signed_in(user, mfa=mfa)
    if not active:
        User.objects.filter(pk=user.pk).update(is_active=False)
        user.refresh_from_db()
    administrator = active and staff and superuser
    session_user = client.get("/api/session/").json()["user"]
    assert (client.get("/admin/").status_code == 200) is (administrator and mfa)
    assert bool(session_user and session_user["can_admin"]) is (administrator and mfa)
    assert _operation_allowed(user) is administrator
    if session_user is not None:
        assert session_user["mfa_required"] is (staff or superuser)
