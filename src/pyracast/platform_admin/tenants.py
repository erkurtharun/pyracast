from typing import TYPE_CHECKING, Any, ClassVar, cast

from django import forms
from django.contrib import messages
from django.contrib.admin.models import ADDITION, CHANGE, LogEntry
from django.contrib.auth.models import User
from django.contrib.contenttypes.models import ContentType
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Count, Exists, OuterRef, QuerySet, Subquery
from django.db.models.functions import Coalesce
from django.http import HttpRequest, QueryDict
from django.utils import timezone
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy

from pyracast.accounts.authorization import require_platform_admin
from pyracast.accounts.invitations import invite_workspace_member
from pyracast.accounts.models import (
    InvitationDeliveryStatus,
    Language,
    Membership,
    Tenant,
    WorkspaceInvitation,
    WorkspaceRole,
)
from pyracast.accounts.operations import reassign_workspace_admin
from pyracast.errors import ConflictError, ErrorCode
from pyracast.plants.models import Plant
from pyracast.platform_admin.read_models import summarize_plant, workspace_regions
from pyracast.regions.models import active_region

if TYPE_CHECKING:
    from django_stubs_ext import StrPromise

    MemberChoice = forms.ModelChoiceField[User]
    TenantForm = forms.ModelForm[Tenant]
else:
    MemberChoice = forms.ModelChoiceField
    TenantForm = forms.ModelForm

DELIVERY_LABELS: dict[str, StrPromise] = {
    InvitationDeliveryStatus.PENDING: gettext_lazy("Sending"),
    InvitationDeliveryStatus.SENT: gettext_lazy("Sent"),
    InvitationDeliveryStatus.FAILED: gettext_lazy("Not sent"),
    InvitationDeliveryStatus.UNCERTAIN: gettext_lazy("Delivery unknown"),
}


def member_label(user: User) -> str:
    return user.email or user.get_username()


def pending_invitations(tenant_id: int | OuterRef) -> QuerySet[WorkspaceInvitation]:
    return WorkspaceInvitation.objects.filter(
        tenant_id=tenant_id, accepted_at__isnull=True, expires_at__gt=timezone.now()
    )


class WorkspaceMemberChoice(MemberChoice):
    def label_from_instance(self, obj: User) -> str:
        return member_label(obj)


class WorkspaceAdministratorForm(forms.Form):
    administrator = WorkspaceMemberChoice(
        queryset=User.objects.none(),
        label=gettext_lazy("Administrator"),
        help_text=gettext_lazy("Only active members can be chosen."),
    )
    expected_admin_id = forms.IntegerField(required=False, widget=forms.HiddenInput)

    def __init__(
        self, tenant: Tenant, data: QueryDict | None = None, *, selected: str | None = None
    ) -> None:
        current = (
            Membership.objects.filter(tenant=tenant, role=WorkspaceRole.ADMIN)
            .values_list("user_id", flat=True)
            .first()
        )
        super().__init__(
            data, initial={"administrator": selected or current, "expected_admin_id": current}
        )
        self.tenant = tenant
        field = cast(WorkspaceMemberChoice, self.fields["administrator"])
        field.queryset = User.objects.filter(membership__tenant=tenant, is_active=True).order_by(
            "email", "username", "pk"
        )

    def apply(self, actor: User) -> None:
        user: User = self.cleaned_data["administrator"]
        reassign_workspace_admin(
            actor,
            self.tenant.pk,
            user.pk,
            expected_admin_id=self.cleaned_data["expected_admin_id"],
        )


def _log(actor: User, tenant: Tenant, flag: int, message: str) -> None:
    LogEntry.objects.create(
        user=actor,
        content_type=ContentType.objects.get_for_model(Tenant),
        object_id=str(tenant.pk),
        object_repr=tenant.name,
        action_flag=flag,
        change_message=message,
    )


class WorkspaceNameForm(TenantForm):
    class Meta:
        model = Tenant
        fields = ("name",)
        labels: ClassVar = {"name": gettext_lazy("Workspace name")}


class WorkspaceForm(WorkspaceNameForm):
    administrator_email = forms.EmailField(
        max_length=254,
        required=False,
        label=gettext_lazy("Administrator email (optional)"),
        help_text=gettext_lazy(
            "We email this address an invitation to administer the workspace. Without one, "
            "the workspace has no administrator until you invite one."
        ),
    )
    language = forms.ChoiceField(
        choices=Language.choices, label=gettext_lazy("Invitation language")
    )

    def apply(self, actor: User) -> tuple[Tenant, WorkspaceInvitation | None]:
        require_platform_admin(actor)
        email = self.cleaned_data["administrator_email"]
        with transaction.atomic():
            self.instance.save()
            _log(actor, self.instance, ADDITION, "Created.")
            invitation = (
                invite_workspace_member(
                    actor,
                    self.instance.pk,
                    email,
                    role=WorkspaceRole.ADMIN,
                    language=self.cleaned_data["language"],
                    platform=True,
                )
                if email
                else None
            )
        return self.instance, invitation


class WorkspaceRenameForm(WorkspaceNameForm):
    expected_name = forms.CharField(widget=forms.HiddenInput, strip=False)

    def apply(self, actor: User) -> bool:
        require_platform_admin(actor)
        expected, name = self.cleaned_data["expected_name"], self.cleaned_data["name"]
        with transaction.atomic():
            tenant = Tenant.objects.select_for_update().get(pk=self.instance.pk)
            if tenant.name != expected:
                raise ConflictError(
                    ErrorCode.CONFLICT,
                    _(
                        "This workspace changed after you opened this page. "
                        "Reload it to see the current name."
                    ),
                )
            if name == expected:
                return False
            tenant.name = name
            tenant.save(update_fields=("name",))
            _log(actor, tenant, CHANGE, f"Renamed from {expected} to {name}.")
        return True


class InvitationForm(forms.Form):
    email = forms.EmailField(max_length=254, label=gettext_lazy("Email address"))
    role = forms.ChoiceField(
        required=False,
        widget=forms.RadioSelect,
        choices=(
            (WorkspaceRole.VIEWER, gettext_lazy("Viewer: sees plants, forecasts and production")),
            (
                WorkspaceRole.ADMIN,
                gettext_lazy("Administrator: manages plants, members and settings"),
            ),
        ),
        label=gettext_lazy("Role"),
        help_text=gettext_lazy("Offered while the workspace has no administrator."),
    )
    language = forms.ChoiceField(
        choices=Language.choices, label=gettext_lazy("Invitation language")
    )

    def apply(self, actor: User, tenant: Tenant) -> WorkspaceInvitation:
        return invite_workspace_member(
            actor,
            tenant.pk,
            self.cleaned_data["email"],
            role=self.cleaned_data["role"] or WorkspaceRole.VIEWER,
            language=self.cleaned_data["language"],
            platform=True,
        )


def report_invitation(request: HttpRequest, invitation: WorkspaceInvitation) -> None:
    messages.success(
        request,
        _("The invitation to %(email)s is being sent; its state appears in the list.")
        % {"email": invitation.email},
    )


def tenant_list_context(query: str, page: str) -> dict[str, Any]:
    administrator = Membership.objects.filter(tenant_id=OuterRef("pk"), role=WorkspaceRole.ADMIN)
    tenants = Tenant.objects.annotate(
        member_count=Count("membership", distinct=True),
        plant_count=Count("region__plant", filter=active_region("region__"), distinct=True),
        administrator_email=Subquery(administrator.values("user__email")[:1]),
        administrator_username=Subquery(administrator.values("user__username")[:1]),
        administrator_invited=Exists(
            pending_invitations(OuterRef("pk")).filter(role=WorkspaceRole.ADMIN)
        ),
    ).order_by("name", "pk")
    if query:
        tenants = tenants.filter(name__icontains=query)
    return {"page_obj": Paginator(tenants, 20).get_page(page), "q": query}


def workspace_context(tenant: Tenant) -> dict[str, Any]:
    members = list(
        Membership.objects.filter(tenant=tenant)
        .select_related("user")
        .order_by("role", "user__email", "user__username", "pk")
    )
    administrator = next(
        (member.user for member in members if member.role == WorkspaceRole.ADMIN), None
    )
    invitations = list(pending_invitations(tenant.pk).order_by("-created_at", "-pk"))
    capacities = dict(
        Plant.objects.filter(region__tenant=tenant)
        .annotate(
            capacity=Coalesce("current_configuration__dc_capacity_mw", "submission__dc_capacity_mw")
        )
        .values_list("region_id", "capacity")
    )
    return {
        "tenant": tenant,
        "administrator": member_label(administrator) if administrator else "",
        "administrator_invitation": next(
            (
                invitation.email
                for invitation in invitations
                if invitation.role == WorkspaceRole.ADMIN
            ),
            "",
        ),
        "members": [
            {"user": member.user, "label": member_label(member.user), "role": member.role}
            for member in members
        ],
        "invitations": [
            {"invitation": invitation, "delivery": DELIVERY_LABELS[invitation.delivery_status]}
            for invitation in invitations
        ],
        "plants": [
            {
                "summary": summarize_plant(region),
                "capacity_mw": capacities.get(region.pk),
            }
            for region in workspace_regions().filter(tenant=tenant).order_by("name", "pk")
        ],
    }
