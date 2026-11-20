import re
from collections.abc import Callable
from datetime import timedelta

import pytest
from django.contrib.admin.models import ADDITION, CHANGE, LogEntry
from django.contrib.auth.models import User
from django.core import mail
from django.test import Client
from django.utils import timezone

from pyracast.accounts.invitations import invite_workspace_member
from pyracast.accounts.models import (
    Membership,
    Tenant,
    WorkspaceInvitation,
    WorkspaceMemberRemoval,
    WorkspaceRole,
)
from pyracast.accounts.operations import reassign_workspace_admin
from pyracast.mail.errors import PermanentlyNotSentError
from pyracast.plants.sites import submit_site
from pyracast.regions.models import Region
from tests.support.api import verified_admin
from tests.support.auth_lifecycle import LONGEST_ADDRESS, OVERLONG_ACCOUNT_ADDRESS
from tests.support.concurrency import behind_open_transaction
from tests.support.outbox import deliver_mail

pytestmark = pytest.mark.django_db(transaction=True)

PRIMARY_ACTION = re.compile(r'class="btn btn--primary"[^>]*>([^<]+)<')


def workspace() -> tuple[Tenant, User, User]:
    tenant = Tenant.objects.create(name="Solar workspace")
    owner = User.objects.create_user(username="owner", email="owner@example.com")
    viewer = User.objects.create_user(username="viewer", email="viewer@example.com")
    Membership.objects.create(tenant=tenant, user=owner, role=WorkspaceRole.ADMIN)
    Membership.objects.create(tenant=tenant, user=viewer)
    return tenant, owner, viewer


def site(owner: User, tenant: Tenant, name: str = "Console site") -> Region:
    return submit_site(
        owner,
        tenant.pk,
        name=name,
        latitude=41,
        longitude=29,
        timezone="Europe/Istanbul",
        dc_capacity_mw=1.5,
    )


def workspace_paths(
    tenant: Tenant, member: User, invitation: WorkspaceInvitation, region: Region
) -> list[str]:
    base = f"/admin/workspaces/{tenant.pk}/"
    return [
        "/admin/workspaces/new/",
        base,
        f"{base}name/",
        f"{base}administrator/",
        f"{base}invitations/new/",
        f"{base}invitations/{invitation.pk}/reissue/",
        f"{base}invitations/{invitation.pk}/revocation/",
        f"{base}members/{member.pk}/removal/",
        f"{base}plants/new/",
        f"/admin/plants/{region.pk}/details/",
    ]


def signed_in(identity: str, owner: User) -> Client:
    if identity == "inactive":
        client, user = verified_admin()
        user.is_active = False
        user.save(update_fields=["is_active"])
        return client
    client = Client()
    if identity == "member":
        client.force_login(owner)
    elif identity != "anonymous":
        user = User.objects.create_user(
            username=identity, is_staff=True, is_superuser=identity == "unverified_admin"
        )
        client.force_login(user)
    return client


@pytest.mark.parametrize(
    "identity", ["anonymous", "member", "staff", "unverified_admin", "inactive"]
)
def test_workspace_console_routes_require_a_verified_platform_administrator(
    identity: str,
) -> None:
    tenant, owner, viewer = workspace()
    invitation = invite_workspace_member(owner, tenant.pk, "guest@example.com")
    region = site(owner, tenant)
    deliver_mail()
    sent = len(mail.outbox)
    client = signed_in(identity, owner)
    attempt = {
        "name": "Changed",
        "expected_name": tenant.name,
        "email": "intruder@example.com",
        "language": "en",
        "administrator": viewer.pk,
        "expected_admin_id": owner.pk,
    }
    for path in workspace_paths(tenant, viewer, invitation, region):
        assert client.get(path).status_code == 302, path
        assert client.post(path, attempt).status_code == 302, path
    assert list(Tenant.objects.values_list("name", flat=True)) == ["Solar workspace"]
    assert Membership.objects.get(user=owner).role == WorkspaceRole.ADMIN
    assert Membership.objects.filter(user=viewer).exists()
    assert WorkspaceInvitation.objects.get().state_at(timezone.now()) == "pending"
    deliver_mail()
    assert len(mail.outbox) == sent
    assert Region.objects.get().name == "Console site"


def test_workspace_console_routes_guard_methods_csrf_and_unknown_records() -> None:
    tenant, owner, viewer = workspace()
    invitation = invite_workspace_member(owner, tenant.pk, "guest@example.com")
    region = site(owner, tenant)
    client, _ = verified_admin(enforce_csrf_checks=True)
    paths = workspace_paths(tenant, viewer, invitation, region)
    for path in paths:
        assert client.post(path, {"name": "Changed"}).status_code == 403, path
    assert client.get("/admin/workspaces/").status_code == 200
    token = client.cookies["csrftoken"].value
    for path in paths:
        assert client.put(path, HTTP_X_CSRFTOKEN=token).status_code == 405, path
    for path in paths[5:7]:
        assert client.get(path).status_code == 405, path
    assert client.post(paths[1], HTTP_X_CSRFTOKEN=token).status_code == 405
    assert client.post("/admin/workspaces/", HTTP_X_CSRFTOKEN=token).status_code == 405
    missing = Tenant.objects.create(name="Other workspace")
    for path in (
        "/admin/workspaces/999999/",
        "/admin/workspaces/999999/name/",
        "/admin/workspaces/999999/invitations/new/",
        "/admin/workspaces/999999/plants/new/",
        f"/admin/workspaces/{tenant.pk}/members/999999/removal/",
        f"/admin/workspaces/{missing.pk}/members/{viewer.pk}/removal/",
        "/admin/plants/999999/details/",
    ):
        assert client.get(path).status_code == 404, path
    for path in (
        f"/admin/workspaces/{missing.pk}/invitations/{invitation.pk}/reissue/",
        f"/admin/workspaces/{missing.pk}/invitations/{invitation.pk}/revocation/",
    ):
        assert client.post(path, HTTP_X_CSRFTOKEN=token).status_code == 404, path
    assert tenant.name == Tenant.objects.get(pk=tenant.pk).name
    assert WorkspaceInvitation.objects.get().state_at(timezone.now()) == "pending"


def test_workspace_list_offers_creation_and_shows_each_administrator_state() -> None:
    client, operator = verified_admin()
    empty = client.get("/admin/workspaces/").content.decode()
    assert 'href="/admin/workspaces/new/"' in empty
    assert "No workspaces yet." in empty
    tenant, owner, _ = workspace()
    site(owner, tenant)
    invited = Tenant.objects.create(name="Invited workspace")
    invite_workspace_member(
        operator, invited.pk, "boss@example.com", role=WorkspaceRole.ADMIN, platform=True
    )
    Tenant.objects.create(name="Unassigned workspace")
    html = client.get("/admin/workspaces/").content.decode()
    assert owner.email in html
    assert html.count("Invitation pending") == 1
    assert html.count("No administrator") == 1
    assert '<td data-label="Plants">1</td>' in html
    assert '<td data-label="Members">2</td>' in html
    assert (
        "No workspaces match this search." in client.get("/admin/workspaces/?q=x").content.decode()
    )


def test_workspace_creation_without_an_administrator_is_logged() -> None:
    client, operator = verified_admin()
    page = client.get("/admin/workspaces/new/").content.decode()
    assert 'name="administrator_email"' in page
    assert '<option value="en" selected>' in page
    response = client.post(
        "/admin/workspaces/new/",
        {"name": "  New customer  ", "administrator_email": "", "language": "en"},
    )
    tenant = Tenant.objects.get(name="New customer")
    assert response.status_code == 302
    assert response["Location"] == f"/admin/workspaces/{tenant.pk}/"
    assert not Membership.objects.filter(tenant=tenant).exists()
    assert not WorkspaceInvitation.objects.exists()
    deliver_mail()
    assert not mail.outbox
    entry = LogEntry.objects.get()
    assert (entry.action_flag, entry.object_id, entry.user, entry.object_repr) == (
        ADDITION,
        str(tenant.pk),
        operator,
        "New customer",
    )
    html = client.get(response["Location"]).content.decode()
    assert "Workspace New customer created." in html
    assert f'href="/admin/workspaces/{tenant.pk}/invitations/new/?role=admin"' in html


def test_workspace_creation_invites_its_administrator_in_the_chosen_language() -> None:
    client, operator = verified_admin()
    response = client.post(
        "/admin/workspaces/new/",
        {"name": "Invited customer", "administrator_email": "Boss@Example.com", "language": "tr"},
        HTTP_ACCEPT_LANGUAGE="en",
    )
    tenant = Tenant.objects.get(name="Invited customer")
    invitation = WorkspaceInvitation.objects.get(tenant=tenant)
    assert (invitation.email, invitation.role, invitation.created_by) == (
        "boss@example.com",
        WorkspaceRole.ADMIN,
        operator,
    )
    deliver_mail()
    assert "çal\u0131şma alan\u0131n\u0131n yöneticisi yapar" in str(mail.outbox[-1].body)
    html = client.get(response["Location"]).content.decode()
    assert "The invitation to boss@example.com is being sent" in html
    assert "Invitation pending" in html


def test_workspace_creation_allows_a_name_another_workspace_already_has() -> None:
    client, operator = verified_admin()
    existing = Tenant.objects.create(name="Shared workspace")
    response = client.post(
        "/admin/workspaces/new/",
        {"name": " Shared workspace ", "administrator_email": "boss@example.com", "language": "en"},
    )
    assert response.status_code == 302
    created = Tenant.objects.exclude(pk=existing.pk).get()
    assert created.name == existing.name
    assert response["Location"] == f"/admin/workspaces/{created.pk}/"
    invitation = WorkspaceInvitation.objects.get()
    assert (invitation.tenant, invitation.email, invitation.role) == (
        created,
        "boss@example.com",
        WorkspaceRole.ADMIN,
    )
    entry = LogEntry.objects.get()
    assert (entry.object_id, entry.action_flag, entry.user) == (str(created.pk), ADDITION, operator)
    assert Tenant.objects.count() == 2


def test_a_blank_workspace_name_is_a_field_error_and_keeps_the_other_values() -> None:
    client, _ = verified_admin()
    response = client.post(
        "/admin/workspaces/new/",
        {"name": "   ", "administrator_email": "boss@example.com", "language": "en"},
    )
    assert response.status_code == 400
    html = response.content.decode()
    assert "This field is required." in html
    assert 'value="boss@example.com"' in html
    assert "data-error-summary" in html
    assert 'href="#id_name"' in html
    assert not Tenant.objects.exists()
    assert not WorkspaceInvitation.objects.exists()
    assert not LogEntry.objects.exists()


def test_workspace_rename_allows_a_shared_name_is_logged_and_refuses_a_stale_page() -> None:
    tenant, _, _ = workspace()
    existing = Tenant.objects.create(name="Sunny workspace")
    client, operator = verified_admin()
    url = f"/admin/workspaces/{tenant.pk}/name/"
    assert client.get(url).content.decode().count('value="Solar workspace"') == 2
    renamed = client.post(url, {"name": "Sunny workspace", "expected_name": "Solar workspace"})
    assert renamed.status_code == 302
    assert renamed["Location"] == f"/admin/workspaces/{tenant.pk}/"
    assert Tenant.objects.get(pk=tenant.pk).name == existing.name
    assert Tenant.objects.filter(name="Sunny workspace").count() == 2
    entry = LogEntry.objects.get()
    assert (entry.action_flag, entry.user, entry.change_message) == (
        CHANGE,
        operator,
        "Renamed from Solar workspace to Sunny workspace.",
    )
    assert (
        "Sunny workspace is the new workspace name."
        in client.get(renamed["Location"]).content.decode()
    )
    stale = client.post(url, {"name": "Other name", "expected_name": "Solar workspace"})
    assert stale.status_code == 400
    html = stale.content.decode()
    assert "This workspace changed after you opened this page." in html
    assert f'href="{url}"' in html
    unchanged = client.post(url, {"name": "Sunny workspace", "expected_name": "Sunny workspace"})
    assert unchanged.status_code == 302
    assert LogEntry.objects.count() == 1
    assert Tenant.objects.get(pk=tenant.pk).name == "Sunny workspace"


def test_invitation_form_offers_the_administrator_role_only_without_an_administrator() -> None:
    tenant, _, _ = workspace()
    unassigned = Tenant.objects.create(name="Unassigned workspace")
    client, operator = verified_admin()
    assigned_page = client.get(f"/admin/workspaces/{tenant.pk}/invitations/new/")
    assert 'name="role"' not in assigned_page.content.decode()
    url = f"/admin/workspaces/{unassigned.pk}/invitations/new/"
    page = client.get(f"{url}?role=admin").content.decode()
    assert 'value="admin" id="id_role_1" checked' in page
    invited = client.post(url, {"email": "Boss@Example.com", "role": "admin", "language": "en"})
    assert invited.status_code == 302
    assert invited["Location"] == f"/admin/workspaces/{unassigned.pk}/#invitations-title"
    invitation = WorkspaceInvitation.objects.get(tenant=unassigned)
    assert (invitation.role, invitation.created_by) == (WorkspaceRole.ADMIN, operator)
    assert (
        "The invitation to boss@example.com is being sent"
        in client.get(invited["Location"]).content.decode()
    )

    stale = client.post(
        f"/admin/workspaces/{tenant.pk}/invitations/new/",
        {"email": "second@example.com", "role": "admin", "language": "en"},
    )
    assert stale.status_code == 400
    assert "The workspace administrator changed. Reload before saving." in stale.content.decode()
    viewer = client.post(
        f"/admin/workspaces/{tenant.pk}/invitations/new/",
        {"email": "second@example.com", "language": "tr"},
    )
    assert viewer.status_code == 302
    assert WorkspaceInvitation.objects.get(tenant=tenant).role == WorkspaceRole.VIEWER
    deliver_mail()
    assert "salt okunur erişim" in str(mail.outbox[-1].body)


def test_invitation_form_defaults_to_the_administrator_role_until_one_is_invited() -> None:
    tenant = Tenant.objects.create(name="Unassigned workspace")
    client, operator = verified_admin()
    url = f"/admin/workspaces/{tenant.pk}/invitations/new/"
    page = client.get(url).content.decode()
    assert 'value="admin" id="id_role_1" checked' in page
    assert "This workspace has no administrator. Invite the administrator first." in page

    invite_workspace_member(
        operator, tenant.pk, "boss@example.com", role=WorkspaceRole.ADMIN, platform=True
    )
    pending = client.get(url).content.decode()
    assert 'value="viewer" id="id_role_0" checked' in pending
    assert 'value="admin" id="id_role_1">' in pending
    assert "Invite the administrator first." not in pending


def test_invitation_refusals_are_form_errors_that_keep_the_values() -> None:
    tenant, _, _ = workspace()
    client, _ = verified_admin()
    url = f"/admin/workspaces/{tenant.pk}/invitations/new/"
    member = client.post(url, {"email": "VIEWER@example.com", "language": "en"})
    assert member.status_code == 400
    assert "This account already belongs to the workspace." in member.content.decode()
    assert 'value="VIEWER@example.com"' in member.content.decode()
    invalid = client.post(url, {"email": "not-an-address", "language": "en"})
    assert invalid.status_code == 400
    assert "Enter a valid email address." in invalid.content.decode()
    assert client.post(url, {"email": "guest@example.com", "language": "en"}).status_code == 302
    limited = client.post(url, {"email": "guest@example.com", "language": "en"})
    assert limited.status_code == 400
    assert "Too many invitations were sent. Try again later." in (limited.content.decode())
    assert WorkspaceInvitation.objects.count() == 1


@pytest.mark.parametrize("address", [OVERLONG_ACCOUNT_ADDRESS, LONGEST_ADDRESS], ids=["255", "320"])
def test_an_invitation_address_longer_than_an_account_address_is_a_form_error(
    address: str,
) -> None:
    tenant, _, _ = workspace()
    client, _ = verified_admin()

    invitation = client.post(
        f"/admin/workspaces/{tenant.pk}/invitations/new/", {"email": address, "language": "en"}
    )
    creation = client.post(
        "/admin/workspaces/new/",
        {"name": "Long address", "administrator_email": address, "language": "en"},
    )

    for response in (invitation, creation):
        assert response.status_code == 400
        assert "Ensure this value has at most 254 characters" in response.content.decode()
    assert not WorkspaceInvitation.objects.exists()
    assert not Tenant.objects.filter(name="Long address").exists()
    assert not LogEntry.objects.exists()


@pytest.mark.parametrize(
    ("failure", "status", "label"),
    [
        (PermanentlyNotSentError("relay refused"), "failed", "Not sent"),
        (TimeoutError("relay silent"), "uncertain", "Delivery unknown"),
    ],
)
def test_an_undelivered_invitation_is_reported_with_its_delivery_state(
    monkeypatch: pytest.MonkeyPatch, failure: Exception, status: str, label: str
) -> None:
    tenant, _, _ = workspace()
    client, _ = verified_admin()

    def fail(message: object, *, before_send: Callable[[], bool]) -> None:
        assert before_send()
        raise failure

    monkeypatch.setattr("pyracast.notifications.outbox.send", fail)
    response = client.post(
        f"/admin/workspaces/{tenant.pk}/invitations/new/",
        {"email": "guest@example.com", "language": "en"},
    )
    deliver_mail()
    html = client.get(response["Location"]).content.decode()
    assert f'<span class="badge badge--{status}">{label}</span>' in html
    assert WorkspaceInvitation.objects.get().delivery_status == status


def test_pending_invitations_are_reissued_and_revoked_from_the_workspace_page() -> None:
    tenant, _, _ = workspace()
    client, operator = verified_admin()
    invitation = invite_workspace_member(operator, tenant.pk, "guest@example.com", platform=True)
    deliver_mail()
    base = f"/admin/workspaces/{tenant.pk}/"
    page = client.get(base).content.decode()
    assert "guest@example.com" in page
    assert f'action="{base}invitations/{invitation.pk}/reissue/"' in page
    assert f'action="{base}invitations/{invitation.pk}/revocation/"' in page
    assert '<span class="badge badge--sent">Sent</span>' in page

    limited = client.post(f"{base}invitations/{invitation.pk}/reissue/")
    assert limited["Location"] == f"{base}#invitations-title"
    assert "Too many invitations were sent." in client.get(base).content.decode()
    assert WorkspaceInvitation.objects.count() == 1
    WorkspaceInvitation.objects.update(created_at=timezone.now() - timedelta(minutes=2))
    reissued = client.post(f"{base}invitations/{invitation.pk}/reissue/")
    assert reissued["Location"] == f"{base}#invitations-title"
    invitation.refresh_from_db()
    assert invitation.state_at(timezone.now()) == "superseded"
    replacement = WorkspaceInvitation.objects.latest("pk")
    assert (replacement.email, replacement.role) == ("guest@example.com", WorkspaceRole.VIEWER)
    deliver_mail()
    assert mail.outbox[-1].to == ["guest@example.com"]
    assert "The invitation to guest@example.com is being sent" in client.get(base).content.decode()

    revoked = client.post(f"{base}invitations/{replacement.pk}/revocation/")
    assert revoked["Location"] == f"{base}#invitations-title"
    replacement.refresh_from_db()
    assert replacement.state_at(timezone.now()) == "revoked"
    html = client.get(base).content.decode()
    assert "Invitation to guest@example.com revoked." in html
    assert "No pending invitations." in html
    client.post(f"{base}invitations/{replacement.pk}/reissue/")
    assert "The invitation is invalid or expired." in client.get(base).content.decode()
    assert WorkspaceInvitation.objects.count() == 2


def test_a_resend_waiting_for_an_administrator_change_does_not_revive_the_invitation() -> None:
    tenant, owner, viewer = workspace()
    client, operator = verified_admin()
    invitation = invite_workspace_member(operator, tenant.pk, "guest@example.com", platform=True)
    WorkspaceInvitation.objects.update(created_at=timezone.now() - timedelta(minutes=2))
    deliver_mail()
    sent = len(mail.outbox)
    base = f"/admin/workspaces/{tenant.pk}/"

    _, resent = behind_open_transaction(
        lambda: reassign_workspace_admin(
            operator, tenant.pk, viewer.pk, expected_admin_id=owner.pk
        ),
        lambda: client.post(f"{base}invitations/{invitation.pk}/reissue/"),
    )

    assert resent["Location"] == f"{base}#invitations-title"
    assert Membership.objects.get(user=viewer).role == WorkspaceRole.ADMIN
    assert list(WorkspaceInvitation.objects.values_list("pk", flat=True)) == [invitation.pk]
    assert WorkspaceInvitation.objects.get().state_at(timezone.now()) == "revoked"
    deliver_mail()
    assert len(mail.outbox) == sent
    assert "The invitation is invalid or expired." in client.get(base).content.decode()


def test_member_removal_states_its_consequences_and_keeps_the_administrator() -> None:
    tenant, owner, viewer = workspace()
    Tenant.objects.filter(pk=tenant.pk).update(name="Solar Enerji A.Ş.")
    client, operator = verified_admin()
    url = f"/admin/workspaces/{tenant.pk}/members/{viewer.pk}/removal/"
    page = client.get(url).content.decode()
    assert "<dt>Member</dt><dd>viewer@example.com</dd>" in page
    assert "<dt>Workspace</dt><dd>Solar Enerji A.Ş.</dd>" in page
    assert "A.Ş.." not in page
    for consequence in (
        "Their access to this workspace ends at once.",
        "They no longer receive email reminders for this workspace.",
        "Pending invitations to their addresses expire.",
    ):
        assert consequence in page
    assert ">Remove member</button>" in page
    removed = client.post(url)
    assert removed["Location"] == f"/admin/workspaces/{tenant.pk}/#members-title"
    assert not Membership.objects.filter(user=viewer).exists()
    assert WorkspaceMemberRemoval.objects.get().actor == operator
    html = client.get(removed["Location"]).content.decode()
    assert "viewer@example.com was removed from the workspace." in html
    assert client.get(url).status_code == 404

    administrator = f"/admin/workspaces/{tenant.pk}/members/{owner.pk}/removal/"
    page = client.get(administrator).content.decode()
    assert "<dt>Member</dt><dd>owner@example.com</dd>" in page
    assert "The administrator cannot be removed." in page
    assert ">Remove member</button>" not in page
    refused = client.post(administrator)
    assert refused.status_code == 400
    assert "The workspace administrator cannot be removed." in refused.content.decode()
    assert Membership.objects.get(user=owner).role == WorkspaceRole.ADMIN


def settings_section(html: str) -> str:
    return html[html.index('<h2 id="settings-title"') :]


def test_workspace_page_orders_its_sections_and_points_to_the_next_step() -> None:
    client, operator = verified_admin()
    fresh = Tenant.objects.create(name="Fresh workspace")
    base = f"/admin/workspaces/{fresh.pk}/"
    html = client.get(base).content.decode()
    assert html.count("<h1") == 1
    positions = [
        html.index(f'<h2 id="{section}-title"')
        for section in ("plants", "members", "invitations", "settings")
    ]
    assert positions == sorted(positions)
    for section in ("plants", "members", "invitations", "settings"):
        assert f'aria-labelledby="{section}-title"' in html
    for empty in (
        "No plants yet.",
        "No members yet. Invite the customer's administrator first.",
        "No pending invitations. Invite people under Members.",
    ):
        assert empty in html
    assert PRIMARY_ACTION.findall(html) == ["Add plant", "Invite member"]
    assert f'href="{base}plants/new/"' in html
    settings = settings_section(html)
    assert f'href="{base}name/">Rename</a>' in settings
    assert f'href="{base}invitations/new/?role=admin">Invite an administrator</a>' in settings

    invite_workspace_member(
        operator, fresh.pk, "boss@example.com", role=WorkspaceRole.ADMIN, platform=True
    )
    invited = client.get(base).content.decode()
    assert "boss@example.com" in settings_section(invited)
    assert "Invitation pending" in settings_section(invited)
    assert "?role=admin" not in invited
    assert (
        "No members yet. The administrator appears here after accepting the invitation." in invited
    )

    tenant, owner, viewer = workspace()
    region = site(owner, tenant)
    base = f"/admin/workspaces/{tenant.pk}/"
    html = client.get(base).content.decode()
    assert "Console site" in html
    assert "1.5 MW" in html
    assert "Awaiting approval" in html
    assert "Not configured" in html
    assert html.count(f'href="/admin/plants/{region.pk}/"') == 1
    assert owner.email in html
    assert viewer.email in html
    assert "Never" in html
    assert f"/members/{viewer.pk}/removal/" in html
    assert f"/members/{owner.pk}/removal/" not in html
    assert f"/administrator/?member={viewer.pk}" in html
    assert PRIMARY_ACTION.findall(html) == ["Add plant", "Invite member"]
    settings = settings_section(html)
    assert f'href="{base}name/">Rename</a>' in settings
    assert f'href="{base}administrator/">Change administrator</a>' in settings


def test_workspace_console_pages_render_in_turkish_without_mail_or_provider_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tenant, owner, viewer = workspace()
    region = site(owner, tenant)
    client, operator = verified_admin()
    invite_workspace_member(operator, tenant.pk, "guest@example.com", platform=True)

    def unexpected(*args: object, **kwargs: object) -> None:
        pytest.fail("A console page sent mail or called a provider on GET")

    monkeypatch.setattr("pyracast.notifications.outbox.send", unexpected)
    monkeypatch.setattr("pyracast.platform_admin.plant_review.adapter_for", unexpected)
    base = f"/admin/workspaces/{tenant.pk}/"
    expectations = {
        "/admin/workspaces/": "Yeni çal\u0131şma alan\u0131",
        "/admin/workspaces/new/": "Çal\u0131şma alan\u0131 oluştur",
        base: "Santral ekle",
        f"{base}name/": "Çal\u0131şma alan\u0131n\u0131 yeniden adland\u0131r",
        f"{base}administrator/": "Yöneticiyi değiştir",
        f"{base}invitations/new/": "Davet gönder",
        f"{base}members/{viewer.pk}/removal/": "Üyeyi ç\u0131kar",
        f"{base}plants/new/": "Santral ekle",
        f"/admin/plants/{region.pk}/details/": "Santral bilgilerini düzenle",
    }
    for path, text in expectations.items():
        response = client.get(path, HTTP_ACCEPT_LANGUAGE="tr")
        assert response.status_code == 200, path
        html = response.content.decode()
        assert '<html lang="tr">' in html, path
        assert text in html, path
        assert html.count("<h1") == 1, path

    page = client.get(base, HTTP_ACCEPT_LANGUAGE="tr").content.decode()
    assert ">İnceleme</th>" in page
    assert ">Gönderim</th>" in page
    assert "Hiç oturum açmad\u0131" in page
    assert "Teslimat" not in page
    for path, header in (
        ("/admin/plants/", '<th scope="col">İnceleme</th>'),
        (f"/admin/plants/{region.pk}/", "<dt>İnceleme</dt>"),
    ):
        assert header in client.get(path, HTTP_ACCEPT_LANGUAGE="tr").content.decode(), path


@pytest.mark.parametrize(
    ("language", "notice"),
    [
        ("en", "You cannot remove your own membership from this console."),
        ("tr", "Bu konsoldan kendi üyeliğinizi kald\u0131ramazs\u0131n\u0131z."),
    ],
)
def test_the_console_keeps_the_signed_in_membership_and_explains_self_removal(
    language: str,
    notice: str,
) -> None:
    tenant, _, _ = workspace()
    client, operator = verified_admin()
    Membership.objects.create(tenant=tenant, user=operator)
    url = f"/admin/workspaces/{tenant.pk}/members/{operator.pk}/removal/"
    responses = (
        client.get(url, HTTP_ACCEPT_LANGUAGE=language),
        client.post(url, HTTP_ACCEPT_LANGUAGE=language),
    )
    assert [response.status_code for response in responses] == [200, 400]
    for response in responses:
        html = response.content.decode()
        assert notice in html
        assert 'class="btn btn--danger"' not in html
        assert ">Remove member</button>" not in html
        assert "The workspace administrator cannot be removed." not in html
        assert f'href="/admin/workspaces/{tenant.pk}/"' in html
    assert Membership.objects.get(tenant=tenant, user=operator).role == WorkspaceRole.VIEWER
    assert not WorkspaceMemberRemoval.objects.exists()


def test_hidden_workspace_form_errors_explain_how_to_reload() -> None:
    tenant, _, _ = workspace()
    client, _ = verified_admin()
    url = f"/admin/workspaces/{tenant.pk}/name/"
    response = client.post(url, {"name": "New name"})
    assert response.status_code == 400
    html = response.content.decode()
    assert "data-error-summary" in html
    assert f'<li>This field is required. <a href="{url}">Reload this page</a></li>' in html
    assert 'href="#id_expected_name"' not in html
    assert Tenant.objects.get(pk=tenant.pk).name == "Solar workspace"
    assert not LogEntry.objects.exists()


def test_workspace_tables_leave_keyboard_focus_to_their_interactive_controls() -> None:
    tenant, owner, _ = workspace()
    region = site(owner, tenant)
    client, operator = verified_admin()
    invite_workspace_member(operator, tenant.pk, "guest@example.com", platform=True)
    html = client.get(f"/admin/workspaces/{tenant.pk}/").content.decode()
    for section in ("plants", "members", "invitations"):
        assert f'<div class="table-wrap" role="region" aria-labelledby="{section}-title">' in html
    assert f'href="/admin/plants/{region.pk}/"' in html
    assert ">Resend<" in html
    assert ">Revoke<" in html
