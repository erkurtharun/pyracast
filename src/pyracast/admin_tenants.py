from typing import TYPE_CHECKING, Any, cast

from django import forms
from django.contrib.auth.models import User
from django.core.paginator import Paginator
from django.db.models import Count, OuterRef, Subquery
from django.http import QueryDict
from django.utils.translation import gettext_lazy as _

from pyracast.accounts.models import Membership, Tenant
from pyracast.accounts.operations import reassign_workspace_admin

if TYPE_CHECKING:
    MemberChoice = forms.ModelChoiceField[User]
else:
    MemberChoice = forms.ModelChoiceField


class WorkspaceMemberChoice(MemberChoice):
    def label_from_instance(self, obj: User) -> str:
        return obj.email or obj.get_username()


class WorkspaceAdministratorForm(forms.Form):
    administrator = WorkspaceMemberChoice(queryset=User.objects.none(), label=_("Administrator"))
    expected_admin_id = forms.IntegerField(required=False, widget=forms.HiddenInput)

    def __init__(self, tenant: Tenant, data: QueryDict | None = None) -> None:
        current = (
            Membership.objects.filter(tenant=tenant, role="admin")
            .values_list("user_id", flat=True)
            .first()
        )
        super().__init__(data, initial={"administrator": current, "expected_admin_id": current})
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


def tenant_list_context(query: str, page: str) -> dict[str, Any]:
    administrator = Membership.objects.filter(tenant_id=OuterRef("pk"), role="admin")
    tenants = Tenant.objects.annotate(
        member_count=Count("membership"),
        administrator_email=Subquery(administrator.values("user__email")[:1]),
        administrator_username=Subquery(administrator.values("user__username")[:1]),
    ).order_by("name", "pk")
    if query:
        tenants = tenants.filter(name__icontains=query)
    return {"page_obj": Paginator(tenants, 20).get_page(page), "q": query}
