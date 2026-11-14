from django import forms
from django.contrib.auth.models import User
from django.http import HttpRequest

from pyracast.accounts.language import LANGUAGE_COOKIE, explicit_language
from pyracast.accounts.models import Membership, Tenant, UserLanguagePreference


class CompanySignupForm(forms.Form):
    company_name = forms.CharField(max_length=200)

    def signup(self, request: HttpRequest, user: User) -> None:
        tenant = Tenant.objects.create(name=self.cleaned_data["company_name"])
        Membership.objects.create(user=user, tenant=tenant)
        language = explicit_language(request.COOKIES.get(LANGUAGE_COOKIE))
        if language is not None:
            UserLanguagePreference.objects.create(user=user, language=language)
