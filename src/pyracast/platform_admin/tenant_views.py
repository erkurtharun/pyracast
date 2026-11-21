from typing import TYPE_CHECKING, cast

from django.contrib import messages
from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.utils.translation import gettext as _
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from pyracast.accounts.invitations import (
    has_workspace_admin,
    reissue_workspace_invitation,
    revoke_workspace_invitation,
)
from pyracast.accounts.models import (
    Membership,
    Tenant,
    WorkspaceInvitation,
    WorkspaceMemberRemoval,
    WorkspaceRole,
)
from pyracast.accounts.operations import remove_workspace_member
from pyracast.plants.models import Plant
from pyracast.platform_admin.forms import form_data, form_page, submitted
from pyracast.platform_admin.tenants import (
    InvitationForm,
    WorkspaceAdministratorForm,
    WorkspaceForm,
    WorkspaceRenameForm,
    member_label,
    pending_invitations,
    report_invitation,
    tenant_list_context,
    workspace_context,
)

if TYPE_CHECKING:
    from pyracast.platform_admin.site import PlatformAdminSite


def _actor(request: HttpRequest) -> User:
    return cast(User, request.user)


def _workspace_section(tenant_id: int, section: str) -> HttpResponse:
    return redirect(f"{reverse('admin:workspace_tenant', args=(tenant_id,))}#{section}")


@require_GET
def workspaces(request: HttpRequest, *, site: PlatformAdminSite) -> HttpResponse:
    context = tenant_list_context(
        request.GET.get("q", "").strip()[:200], request.GET.get("page", "1")
    )
    return site.workspace_response(request, "tenants", _("Workspaces"), context)


@require_http_methods(["GET", "POST"])
def new_workspace(request: HttpRequest, *, site: PlatformAdminSite) -> HttpResponse:
    form = WorkspaceForm(form_data(request), initial={"language": request.LANGUAGE_CODE})

    def create() -> HttpResponse:
        tenant, invitation = form.apply(_actor(request))
        messages.success(request, _("Workspace %(name)s created.") % {"name": tenant.name})
        if invitation is not None:
            report_invitation(request, invitation)
        return redirect("admin:workspace_tenant", tenant_id=tenant.pk)

    return submitted(request, form, create) or form_page(
        site, request, "workspace_new", _("New workspace"), {"form": form}
    )


@require_GET
def workspace(request: HttpRequest, tenant_id: int, *, site: PlatformAdminSite) -> HttpResponse:
    tenant = get_object_or_404(Tenant, pk=tenant_id)
    return site.workspace_response(request, "tenant", tenant.name, workspace_context(tenant))


@require_http_methods(["GET", "POST"])
def workspace_name(
    request: HttpRequest, tenant_id: int, *, site: PlatformAdminSite
) -> HttpResponse:
    tenant = get_object_or_404(Tenant, pk=tenant_id)
    form = WorkspaceRenameForm(
        form_data(request),
        instance=Tenant.objects.get(pk=tenant.pk),
        initial={"expected_name": tenant.name},
    )

    def rename() -> HttpResponse:
        name = {"name": form.cleaned_data["name"]}
        if form.apply(_actor(request)):
            messages.success(request, _("%(name)s is the new workspace name.") % name)
        else:
            messages.info(request, _("%(name)s is already the workspace name.") % name)
        return redirect("admin:workspace_tenant", tenant_id=tenant.pk)

    return submitted(request, form, rename) or form_page(
        site, request, "workspace_name", _("Rename workspace"), {"tenant": tenant, "form": form}
    )


@require_http_methods(["GET", "POST"])
def workspace_administrator(
    request: HttpRequest, tenant_id: int, *, site: PlatformAdminSite
) -> HttpResponse:
    tenant = get_object_or_404(Tenant, pk=tenant_id)
    form = WorkspaceAdministratorForm(
        tenant, form_data(request), selected=request.GET.get("member")
    )

    def reassign() -> HttpResponse:
        label = {"member": member_label(form.cleaned_data["administrator"])}
        if form.apply(_actor(request)):
            messages.success(request, _("%(member)s is now the workspace administrator.") % label)
        else:
            messages.info(request, _("%(member)s is already the workspace administrator.") % label)
        return _workspace_section(tenant.pk, "members-title")

    return submitted(request, form, reassign) or form_page(
        site,
        request,
        "workspace_administrator",
        _("Change administrator"),
        {
            "tenant": tenant,
            "form": form,
            "has_automatic_calibration": Plant.objects.filter(
                region__tenant=tenant, automatic_calibration_enabled=True
            ).exists(),
        },
    )


@require_http_methods(["GET", "POST"])
def new_invitation(
    request: HttpRequest, tenant_id: int, *, site: PlatformAdminSite
) -> HttpResponse:
    tenant = get_object_or_404(Tenant, pk=tenant_id)
    offers_administrator = not has_workspace_admin(tenant.pk)
    needs_administrator = (
        offers_administrator
        and not pending_invitations(tenant.pk).filter(role=WorkspaceRole.ADMIN).exists()
    )
    role = request.GET.get("role")
    if role not in WorkspaceRole.values:
        role = WorkspaceRole.ADMIN if needs_administrator else WorkspaceRole.VIEWER
    form = InvitationForm(
        form_data(request), initial={"role": role, "language": request.LANGUAGE_CODE}
    )

    def invite() -> HttpResponse:
        report_invitation(request, form.apply(_actor(request), tenant))
        return _workspace_section(tenant.pk, "invitations-title")

    return submitted(request, form, invite) or form_page(
        site,
        request,
        "invitation_new",
        _("Invite member"),
        {
            "tenant": tenant,
            "form": form,
            "offers_administrator": offers_administrator,
            "needs_administrator": needs_administrator,
        },
    )


@require_POST
def invitation_reissue(request: HttpRequest, tenant_id: int, invitation_id: int) -> HttpResponse:
    get_object_or_404(WorkspaceInvitation, pk=invitation_id, tenant_id=tenant_id)
    try:
        replacement = reissue_workspace_invitation(
            _actor(request),
            tenant_id,
            invitation_id,
            platform=True,
            language=request.LANGUAGE_CODE,
        )
    except ValidationError as refusal:
        messages.error(request, " ".join(refusal.messages))
    else:
        report_invitation(request, replacement)
    return _workspace_section(tenant_id, "invitations-title")


@require_POST
def invitation_revocation(request: HttpRequest, tenant_id: int, invitation_id: int) -> HttpResponse:
    invitation = get_object_or_404(WorkspaceInvitation, pk=invitation_id, tenant_id=tenant_id)
    email = {"email": invitation.email}
    try:
        revoked = revoke_workspace_invitation(
            _actor(request), tenant_id, invitation.pk, platform=True
        )
    except ValidationError as refusal:
        messages.error(request, " ".join(refusal.messages))
    else:
        if revoked:
            messages.success(request, _("Invitation to %(email)s revoked.") % email)
        else:
            messages.info(request, _("The invitation to %(email)s is already closed.") % email)
    return _workspace_section(tenant_id, "invitations-title")


def _report_removal(
    request: HttpRequest, tenant_id: int, label: str, *, removed: bool
) -> HttpResponse:
    member = {"member": label}
    if removed:
        messages.success(request, _("%(member)s was removed from the workspace.") % member)
    else:
        messages.info(request, _("%(member)s is not a member of the workspace.") % member)
    return _workspace_section(tenant_id, "members-title")


@require_http_methods(["GET", "POST"])
def member_removal(
    request: HttpRequest, tenant_id: int, user_id: int, *, site: PlatformAdminSite
) -> HttpResponse:
    membership = (
        Membership.objects.select_related("tenant", "user")
        .filter(tenant_id=tenant_id, user_id=user_id)
        .first()
    )
    if membership is None:
        if request.method != "POST":
            raise Http404
        removal = (
            WorkspaceMemberRemoval.objects.select_related("user")
            .filter(tenant_id=tenant_id, user_id=user_id)
            .first()
        )
        if removal is None:
            raise Http404
        return _report_removal(request, tenant_id, member_label(removal.user), removed=False)
    label = member_label(membership.user)
    is_self = membership.user_id == _actor(request).pk
    errors: list[str] = []
    if request.method == "POST":
        try:
            removed = remove_workspace_member(_actor(request), tenant_id, user_id, platform=True)
        except ValidationError as refusal:
            errors = refusal.messages
            if is_self and membership.role != WorkspaceRole.ADMIN:
                errors = [_("You cannot remove your own membership from this console.")]
        else:
            return _report_removal(request, tenant_id, label, removed=removed)
    return site.workspace_response(
        request,
        "tenant",
        _("Remove member"),
        {
            "tenant": membership.tenant,
            "member": label,
            "is_administrator": membership.role == WorkspaceRole.ADMIN,
            "is_self": is_self,
            "errors": errors,
        },
        status=400 if errors else 200,
        template="member_removal",
    )
