import itertools
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor

import pytest
from django.contrib.auth.models import AnonymousUser, User
from django.core.exceptions import PermissionDenied
from django.db import connection, transaction
from django.http import HttpResponseBase
from django.test import Client

from pyracast.accounts.authorization import (
    is_platform_admin,
    require_platform_admin,
    requires_admin_mfa,
    tenant_membership,
)
from pyracast.accounts.models import Membership, Tenant


def _workspace() -> tuple[Tenant, User, User, User]:
    tenant = Tenant.objects.create(name="Workspace")
    administrator = User.objects.create_user(username="administrator")
    viewer = User.objects.create_user(username="viewer")
    outsider = User.objects.create_user(username="outsider")
    Membership.objects.create(user=administrator, tenant=tenant, role=Membership.Role.ADMIN)
    Membership.objects.create(user=viewer, tenant=tenant, role=Membership.Role.VIEWER)
    return tenant, administrator, viewer, outsider


@pytest.mark.django_db
def test_tenant_membership_returns_the_active_row_with_the_required_role() -> None:
    tenant, administrator, viewer, outsider = _workspace()
    assert tenant_membership(viewer, tenant.pk).role == Membership.Role.VIEWER
    assert tenant_membership(viewer, tenant.pk, role=Membership.Role.VIEWER).user == viewer
    assert tenant_membership(administrator, tenant.pk, role=Membership.Role.ADMIN, lock=True)
    with pytest.raises(PermissionDenied, match="administrator"):
        tenant_membership(viewer, tenant.pk, role=Membership.Role.ADMIN)
    with pytest.raises(PermissionDenied, match="submit requests"):
        tenant_membership(administrator, tenant.pk, role=Membership.Role.VIEWER)
    with pytest.raises(PermissionDenied, match="not a member"):
        tenant_membership(outsider, tenant.pk)
    User.objects.filter(pk=viewer.pk).update(is_active=False)
    with pytest.raises(PermissionDenied, match="not a member"):
        tenant_membership(viewer, tenant.pk)


@pytest.mark.parametrize(
    ("active", "staff", "superuser"), list(itertools.product((True, False), repeat=3))
)
def test_platform_administrator_is_an_active_staff_superuser(
    active: bool, staff: bool, superuser: bool
) -> None:
    user = User(username="operator", is_active=active, is_staff=staff, is_superuser=superuser)
    assert is_platform_admin(user) is (active and staff and superuser)
    assert requires_admin_mfa(user) is (staff or superuser)
    if active and staff and superuser:
        require_platform_admin(user)
    else:
        with pytest.raises(PermissionDenied, match="Platform administrator"):
            require_platform_admin(user)


def test_anonymous_visitor_is_not_a_platform_administrator() -> None:
    assert not is_platform_admin(AnonymousUser())
    assert not requires_admin_mfa(AnonymousUser())


def _held_tenant[T](tenant: Tenant, call: Callable[[], T]) -> tuple[T | PermissionDenied, float]:
    def run() -> tuple[T | PermissionDenied, float]:
        started = time.monotonic()
        try:
            return call(), time.monotonic() - started
        except PermissionDenied as error:
            return error, time.monotonic() - started
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=1) as pool, transaction.atomic():
        Tenant.objects.select_for_update().get(pk=tenant.pk)
        return pool.submit(run).result(timeout=30)


@pytest.mark.django_db(transaction=True)
def test_an_outsider_is_refused_before_the_tenant_lock_is_requested() -> None:
    tenant, _, _, outsider = _workspace()
    result, elapsed = _held_tenant(
        tenant, lambda: tenant_membership(outsider, tenant.pk, role="admin", lock=True)
    )
    assert isinstance(result, PermissionDenied)
    assert elapsed < 2


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("collection", ["members", "invitations"])
@pytest.mark.parametrize(("identity", "status"), [("administrator", 200), ("outsider", 403)])
def test_workspace_lists_answer_without_waiting_for_the_tenant_lock(
    collection: str, identity: str, status: int
) -> None:
    tenant, administrator, _, outsider = _workspace()
    client = Client(raise_request_exception=False)
    client.force_login(administrator if identity == "administrator" else outsider)
    response, elapsed = _held_tenant(
        tenant, lambda: client.get(f"/api/tenants/{tenant.pk}/{collection}/")
    )
    assert isinstance(response, HttpResponseBase)
    assert response.status_code == status
    assert elapsed < 2
