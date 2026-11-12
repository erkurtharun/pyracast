from django import forms
from django.contrib.auth.models import User
from django.http import HttpRequest

from pyracast.accounts.models import Membership, Tenant


class CompanySignupForm(forms.Form):
    company_name = forms.CharField(max_length=200)

    def signup(self, request: HttpRequest, user: User) -> None:
        tenant = Tenant.objects.create(name=self.cleaned_data["company_name"])
        Membership.objects.create(user=user, tenant=tenant)
