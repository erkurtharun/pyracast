from django import forms
from django.contrib.auth.models import User
from django.db.models.functions import Lower, Trim
from django.http import HttpRequest
from django.utils.translation import gettext as _

from pyracast.accounts.invitations import pending_invitation
from pyracast.accounts.models import Membership, Tenant, UserLanguagePreference


class CompanySignupForm(forms.Form):
    company_name = forms.CharField(max_length=200, required=False)
    invitation_token = forms.CharField(max_length=128, required=False)

    def clean_company_name(self) -> str:
        name = str(self.cleaned_data.get("company_name", "")).strip()
        if (
            name
            and Tenant.objects.alias(identity=Lower(Trim("name")))
            .filter(identity=name.lower())
            .exists()
        ):
            raise forms.ValidationError(_("This workspace name is already in use."))
        return name

    def clean(self) -> dict[str, object]:
        data = super().clean() or {}
        token = data.get("invitation_token")
        if token:
            pending_invitation(str(token), str(data.get("email", "")))
        elif not data.get("company_name"):
            self.add_error("company_name", _("A workspace name is required."))
        return data

    def signup(self, request: HttpRequest, user: User) -> None:
        if not self.cleaned_data.get("invitation_token"):
            tenant = Tenant.objects.create(name=self.cleaned_data["company_name"])
            Membership.objects.create(user=user, tenant=tenant, role=Membership.Role.ADMIN)
        UserLanguagePreference.objects.create(user=user, language=request.LANGUAGE_CODE)
