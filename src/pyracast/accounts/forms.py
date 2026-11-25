import re
import unicodedata

from django import forms
from django.contrib.auth.models import User
from django.http import HttpRequest
from django.utils.translation import gettext as _

from pyracast.accounts.invitations import pending_invitation
from pyracast.accounts.operations import register_account

# Control, format (zero-width, bidirectional) and line or paragraph separator characters.
HIDDEN_CATEGORIES = frozenset({"Cc", "Cf", "Zl", "Zp"})
# Mail clients turn these into links, so a name in the invitation mail cannot carry one.
ADDRESS_TEXT = re.compile(r"://|www\.|@|\b[a-z0-9-]+\.[a-z]{2,}\b", re.IGNORECASE | re.ASCII)


def workspace_name(value: str) -> str:
    if any(unicodedata.category(character) in HIDDEN_CATEGORIES for character in value):
        raise forms.ValidationError(
            _("The company name contains an invisible character. Type the name again.")
        )
    name = " ".join(value.split())
    if ADDRESS_TEXT.search(name):
        raise forms.ValidationError(_("Enter the company name without a web or email address."))
    return name


class AccountSignupForm(forms.Form):
    company_name = forms.CharField(max_length=200, required=False)
    invitation_token = forms.CharField(max_length=128, required=False)

    def clean_company_name(self) -> str:
        if str(self.data.get("invitation_token") or "").strip():
            return ""
        return workspace_name(str(self.cleaned_data.get("company_name", "")))

    def clean(self) -> dict[str, object]:
        data = super().clean() or {}
        token = data.get("invitation_token")
        if token:
            pending_invitation(str(token), str(data.get("email", "")))
        elif not data.get("company_name") and "company_name" not in self.errors:
            self.add_error("company_name", _("Enter the company name."))
        return data

    def signup(self, request: HttpRequest, user: User) -> None:
        register_account(
            user,
            language=request.LANGUAGE_CODE,
            workspace_name=self.cleaned_data.get("company_name") or None,
        )
