from typing import Any, cast

from django import forms
from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.db import transaction
from django.views.decorators.debug import sensitive_variables

from pyracast.api.serializers import BindingInput
from pyracast.connections.models import ProviderAccount, SiteBinding
from pyracast.connections.operations import create_account, rotate_secret
from pyracast.regions.models import Region
from pyracast.regions.operations import configure_binding


class ProviderSetupForm(forms.Form):
    expected_revision = forms.IntegerField(min_value=1, widget=forms.HiddenInput)
    expected_binding_id = forms.IntegerField(min_value=1, required=False, widget=forms.HiddenInput)
    account_mode = forms.ChoiceField(
        label="Solcast account for this plant",
        choices=(
            ("existing", "Use an existing account (shared key and quota)"),
            ("new", "Add this plant's Solcast account"),
        ),
    )
    existing_account = forms.ChoiceField(required=False, label="Existing Solcast account")
    external_account_id = forms.CharField(
        required=False, max_length=200, label="New Solcast account identity"
    )
    api_key = forms.CharField(
        required=False,
        max_length=1024,
        strip=False,
        widget=forms.PasswordInput(render_value=False, attrs={"autocomplete": "new-password"}),
        label="Solcast API key",
        help_text="Write only. Required for a new account; leave blank to keep an existing key.",
    )
    confirm_key_rotation = forms.BooleanField(
        required=False,
        label="Confirm replacement of the selected account's key",
        help_text="A replacement affects every plant using the selected account.",
    )
    authorization_reference = forms.CharField(
        required=False,
        max_length=500,
        label="New account service authorization reference",
        help_text="Record the authorization permitting this account to be used by this service.",
    )
    daily_quota = forms.IntegerField(
        required=False,
        min_value=1,
        max_value=10,
        initial=10,
        label="New account daily request limit",
        help_text="Existing accounts retain their shared limit.",
    )
    site_id = forms.CharField(max_length=200, label="Solcast resource ID")
    capacity_kw = forms.FloatField(min_value=0, label="Reference capacity (kW)")
    dc_capacity_kw = forms.FloatField(
        min_value=0, required=False, label="Reference DC capacity (kW, optional)"
    )
    power_basis = forms.ChoiceField(
        choices=(("ac", "AC"), ("dc", "DC")), label="Reference power and capacity basis"
    )
    installation_date = forms.DateField(widget=forms.DateInput(attrs={"type": "date"}))
    azimuth_degrees = forms.FloatField(min_value=0, max_value=359.999999, initial=0)
    losses_percent = forms.FloatField(min_value=0, max_value=99.999999, initial=0)
    settings_verification_reference = forms.CharField(
        max_length=500,
        help_text="Record where the provider site's installation and model settings were checked.",
    )
    capacity_semantics_verified = forms.BooleanField(
        label="I verified a horizontal reference and matching power and capacity units/basis",
    )

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        accounts = ProviderAccount.objects.filter(provider="solcast", enabled=True).order_by(
            "provider", "external_account_id"
        )
        cast(forms.ChoiceField, self.fields["existing_account"]).choices = [
            ("", "Select an account"),
            *[
                (
                    f"{account.pk}:{account.credential_revision}",
                    f"{account.provider} · {account.external_account_id}"
                    f" · key revision {account.credential_revision}",
                )
                for account in accounts
            ],
        ]

    def clean(self) -> dict[str, Any]:
        data = super().clean() or {}
        if data.get("account_mode") == "new":
            for name in ("external_account_id", "authorization_reference", "daily_quota"):
                if not data.get(name) and name not in self.errors:
                    self.add_error(name, "This is required for a new account.")
            if not data.get("api_key"):
                self.add_error("api_key", "Enter an API key for the new account.")
        elif data.get("account_mode") == "existing" and not data.get("existing_account"):
            self.add_error("existing_account", "Select an authorized provider account.")
        if (
            data.get("account_mode") == "existing"
            and data.get("api_key")
            and not data.get("confirm_key_rotation")
        ):
            self.add_error("confirm_key_rotation", "Confirm replacing the shared account key.")
        return data

    @sensitive_variables()
    def configure(self, actor: User, region: Region) -> SiteBinding:
        data = self.cleaned_data
        with transaction.atomic():
            current = Region.objects.select_for_update().get(pk=region.pk, deleted_at__isnull=True)
            if current.revision != data["expected_revision"]:
                raise ValidationError("The plant changed. Reload this page before saving.")
            current_binding_id = (
                SiteBinding.objects.filter(region=current, region_revision=current.revision)
                .order_by("-created_at", "-pk")
                .values_list("pk", flat=True)
                .first()
            )
            if current_binding_id != data["expected_binding_id"]:
                raise ValidationError("The provider setup changed. Reload this page before saving.")
            if data["account_mode"] == "new":
                account = create_account(
                    actor,
                    provider="solcast",
                    external_account_id=data["external_account_id"],
                    api_key=data["api_key"],
                    daily_quota=data["daily_quota"],
                    authorization_reference=data["authorization_reference"],
                    require_new=True,
                )
            else:
                account_id, revision = data["existing_account"].split(":")
                account = ProviderAccount.objects.select_for_update().get(
                    pk=account_id, provider="solcast", enabled=True
                )
                if account.credential_revision != int(revision):
                    raise ValidationError("The account changed. Reload this page before saving.")
                if data["api_key"]:
                    rotate_secret(actor, account.pk, data["api_key"])
            serializer = BindingInput(
                data={
                    "provider_account_id": account.pk,
                    "site_id": data["site_id"],
                    "capacity_kw": data["capacity_kw"],
                    "dc_capacity_kw": data["dc_capacity_kw"],
                    "power_basis": data["power_basis"],
                    "capacity_semantics_verified": data["capacity_semantics_verified"],
                    "installation_date": data["installation_date"],
                    "azimuth_degrees": data["azimuth_degrees"],
                    "losses_percent": data["losses_percent"],
                    "settings_verification_reference": data["settings_verification_reference"],
                }
            )
            serializer.is_valid(raise_exception=True)
            return configure_binding(actor, current.pk, **serializer.validated_data)


class BindingActionForm(forms.Form):
    expected_revision = forms.IntegerField(min_value=1, widget=forms.HiddenInput)
    binding_id = forms.IntegerField(min_value=1, widget=forms.HiddenInput)
    credential_revision = forms.IntegerField(min_value=1, widget=forms.HiddenInput)
    expected_cooldown_until = forms.DateTimeField(required=False, widget=forms.HiddenInput)
    provider_recovered = forms.BooleanField(
        required=False,
        label="The provider has confirmed that this account can make requests again.",
    )

    def current_binding(self, region: Region) -> SiteBinding:
        data = self.cleaned_data
        binding = SiteBinding.objects.select_related("provider_account").get(
            pk=data["binding_id"], region=region, region_revision=data["expected_revision"]
        )
        if (
            region.revision != data["expected_revision"]
            or binding.provider_account.credential_revision != data["credential_revision"]
        ):
            raise ValidationError("The setup changed. Reload this page before continuing.")
        return binding
