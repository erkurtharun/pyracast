from collections.abc import Callable
from typing import TYPE_CHECKING, Any, cast

from django import forms
from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.db import transaction
from django.http import HttpRequest, HttpResponse, QueryDict
from django.template.response import TemplateResponse
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy
from django.views.decorators.debug import sensitive_variables

from pyracast.api.serializers import BindingInput
from pyracast.connections.models import (
    MAX_DAILY_QUOTA,
    PowerBasis,
    Provider,
    ProviderAccount,
    SiteBinding,
)
from pyracast.connections.operations import create_account, rotate_secret
from pyracast.errors import DomainError, ErrorCode
from pyracast.regions.models import Region
from pyracast.regions.operations import configure_binding

if TYPE_CHECKING:
    from pyracast.admin import PlatformAdminSite

STALE_CODES = frozenset(
    {ErrorCode.CONFLICT, ErrorCode.WORKSPACE_ADMIN_CHANGED, ErrorCode.REGION_REVISION_CHANGED}
)
CONSOLE_MESSAGES = {
    ErrorCode.LOCATION_ALREADY_EXISTS: gettext_lazy(
        "This workspace already has a plant at this location."
    ),
    ErrorCode.LOCATION_CHANGE_ALREADY_PENDING: gettext_lazy(
        "A location request is waiting for review. "
        "Approve or reject it before correcting the location."
    ),
}


def form_data(request: HttpRequest) -> QueryDict | None:
    return request.POST if request.method == "POST" else None


def add_refusal(form: forms.BaseForm, error: ValidationError) -> None:
    if isinstance(error, DomainError):
        field = error.field if error.field in form.fields else None
        message = CONSOLE_MESSAGES.get(error.code, error.message)
        form.add_error(field, ValidationError(message, code=error.code))
        return
    form.add_error(None, error)


def submitted(
    request: HttpRequest, form: forms.BaseForm, action: Callable[[], HttpResponse]
) -> HttpResponse | None:
    if request.method != "POST" or not form.is_valid():
        return None
    try:
        return action()
    except ValidationError as error:
        add_refusal(form, error)
        return None


def form_page(
    site: PlatformAdminSite,
    request: HttpRequest,
    template: str,
    title: str,
    context: dict[str, Any],
    *,
    section: str = "tenant",
) -> TemplateResponse:
    form: forms.BaseForm = context["form"]
    stale = any(error.code in STALE_CODES for error in form.non_field_errors().as_data())
    return site.workspace_response(
        request,
        section,
        title,
        {**context, "stale": stale},
        status=400 if request.method == "POST" else 200,
        template=template,
    )


class EpiasConnectionForm(forms.Form):
    provider_plant_id = forms.IntegerField(min_value=1, label=gettext_lazy("EPİAŞ plant ID"))
    expected_eic = forms.RegexField(
        r"^[A-Za-z0-9-]{16}$",
        min_length=16,
        max_length=16,
        label="EIC",
        help_text=gettext_lazy("Enter the plant's 16-character EIC to confirm its identity."),
        error_messages={"invalid": gettext_lazy("Use exactly 16 letters, digits or hyphens.")},
    )


class ProviderSetupForm(forms.Form):
    expected_revision = forms.IntegerField(min_value=1, widget=forms.HiddenInput)
    expected_binding_id = forms.IntegerField(min_value=1, required=False, widget=forms.HiddenInput)
    account_mode = forms.ChoiceField(
        label=gettext_lazy("Solcast account for this plant"),
        choices=(
            ("existing", gettext_lazy("Use an existing account (shared key and quota)")),
            ("new", gettext_lazy("Add this plant's Solcast account")),
        ),
    )
    existing_account = forms.ChoiceField(
        required=False, label=gettext_lazy("Existing Solcast account")
    )
    external_account_id = forms.CharField(
        required=False, max_length=200, label=gettext_lazy("New Solcast account identity")
    )
    api_key = forms.CharField(
        required=False,
        max_length=1024,
        strip=False,
        widget=forms.PasswordInput(render_value=False, attrs={"autocomplete": "new-password"}),
        label=gettext_lazy("Solcast API key"),
        help_text=gettext_lazy(
            "Write only. Required for a new account; leave blank to keep an existing key."
        ),
    )
    confirm_key_rotation = forms.BooleanField(
        required=False,
        label=gettext_lazy("Confirm replacement of the selected account's key"),
        help_text=gettext_lazy("A replacement affects every plant using the selected account."),
    )
    authorization_reference = forms.CharField(
        required=False,
        max_length=500,
        label=gettext_lazy("New account service authorization reference"),
        help_text=gettext_lazy(
            "Record the authorization permitting this account to be used by this service."
        ),
    )
    daily_quota = forms.IntegerField(
        required=False,
        min_value=1,
        max_value=MAX_DAILY_QUOTA,
        initial=MAX_DAILY_QUOTA,
        label=gettext_lazy("New account daily request limit"),
        help_text=gettext_lazy("Existing accounts retain their shared limit."),
    )
    site_id = forms.CharField(max_length=200, label=gettext_lazy("Solcast resource ID"))
    capacity_kw = forms.FloatField(min_value=0, label=gettext_lazy("Reference capacity (kW)"))
    dc_capacity_kw = forms.FloatField(
        min_value=0, required=False, label=gettext_lazy("Reference DC capacity (kW, optional)")
    )
    power_basis = forms.ChoiceField(
        choices=PowerBasis.choices,
        label=gettext_lazy("Reference power and capacity basis"),
    )
    installation_date = forms.DateField(
        label=gettext_lazy("Installation date"),
        widget=forms.DateInput(format="%Y-%m-%d", attrs={"type": "date"}),
    )
    azimuth_degrees = forms.FloatField(
        label=gettext_lazy("Azimuth (degrees)"), min_value=0, max_value=359.999999, initial=0
    )
    losses_percent = forms.FloatField(
        label=gettext_lazy("Losses (%)"), min_value=0, max_value=99.999999, initial=0
    )
    settings_verification_reference = forms.CharField(
        label=gettext_lazy("Settings verification reference"),
        max_length=500,
        help_text=gettext_lazy(
            "Record where the provider site's installation and model settings were checked."
        ),
    )
    capacity_semantics_verified = forms.BooleanField(
        label=gettext_lazy(
            "I verified a horizontal reference and matching power and capacity units/basis"
        ),
    )

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        accounts = ProviderAccount.objects.filter(provider=Provider.SOLCAST, enabled=True).order_by(
            "provider", "external_account_id"
        )
        cast(forms.ChoiceField, self.fields["existing_account"]).choices = [
            ("", _("Select an account")),
            *[
                (
                    f"{account.pk}:{account.credential_revision}",
                    _("%(provider)s · %(identity)s · key revision %(revision)s")
                    % {
                        "provider": account.provider,
                        "identity": account.external_account_id,
                        "revision": account.credential_revision,
                    },
                )
                for account in accounts
            ],
        ]

    def clean(self) -> dict[str, Any]:
        data = super().clean() or {}
        if data.get("account_mode") == "new":
            for name in ("external_account_id", "authorization_reference", "daily_quota"):
                if not data.get(name) and name not in self.errors:
                    self.add_error(name, _("This is required for a new account."))
            if not data.get("api_key"):
                self.add_error("api_key", _("Enter an API key for the new account."))
        elif data.get("account_mode") == "existing" and not data.get("existing_account"):
            self.add_error("existing_account", _("Select an authorized provider account."))
        if (
            data.get("account_mode") == "existing"
            and data.get("api_key")
            and not data.get("confirm_key_rotation")
        ):
            self.add_error("confirm_key_rotation", _("Confirm replacing the shared account key."))
        return data

    @sensitive_variables()
    def configure(self, actor: User, region: Region) -> SiteBinding:
        data = self.cleaned_data
        with transaction.atomic():
            current = Region.objects.select_for_update().get(pk=region.pk, deleted_at__isnull=True)
            if current.revision != data["expected_revision"]:
                raise ValidationError(_("The plant changed. Reload this page before saving."))
            current_binding_id = (
                SiteBinding.objects.filter(region=current, region_revision=current.revision)
                .values_list("pk", flat=True)
                .first()
            )
            if current_binding_id != data["expected_binding_id"]:
                raise ValidationError(
                    _("The provider setup changed. Reload this page before saving.")
                )
            if data["account_mode"] == "new":
                account = create_account(
                    actor,
                    provider=Provider.SOLCAST,
                    external_account_id=data["external_account_id"],
                    api_key=data["api_key"],
                    daily_quota=data["daily_quota"],
                    authorization_reference=data["authorization_reference"],
                    require_new=True,
                )
            else:
                account_id, revision = data["existing_account"].split(":")
                account = ProviderAccount.objects.select_for_update().get(
                    pk=account_id, provider=Provider.SOLCAST, enabled=True
                )
                if account.credential_revision != int(revision):
                    raise ValidationError(_("The account changed. Reload this page before saving."))
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
        label=gettext_lazy("The provider has confirmed that this account can make requests again."),
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
            raise ValidationError(_("The setup changed. Reload this page before continuing."))
        return binding
