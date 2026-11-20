from collections.abc import Callable
from dataclasses import fields
from typing import TYPE_CHECKING, Any, cast

from django import forms
from django.core.exceptions import ValidationError
from django.http import HttpRequest, HttpResponse, QueryDict
from django.template.response import TemplateResponse
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy
from django.views.decorators.debug import sensitive_variables

from pyracast.accounts.inputs import SecondFactorReauthenticateInput
from pyracast.connections.contracts import ProviderError
from pyracast.connections.models import (
    MAX_DAILY_QUOTA,
    SITE_ID_PATTERN,
    PowerBasis,
    Provider,
    ProviderAccount,
    SiteBinding,
)
from pyracast.connections.setup import BindingSpec, ExistingAccount, NewAccount
from pyracast.errors import DomainError, ErrorCode
from pyracast.notifications.operations import MAX_RECIPIENT_LIMIT
from pyracast.plants.approval import ReviewSnapshot
from pyracast.regions.models import Region, RegionStatus
from pyracast.regions.operations import SITE_ID_REFUSED

if TYPE_CHECKING:
    from pyracast.platform_admin.site import PlatformAdminSite

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
_REJECTED_KEY = gettext_lazy(
    "The provider rejected the API key. Enter the current key for this account, "
    "then validate again."
)
_UNREADABLE_ANSWER = gettext_lazy(
    "The provider's answer could not be read. Try validating again later."
)
_UNSUPPORTED_ACCOUNT = gettext_lazy(
    "This account cannot be validated here. Select a Solcast account."
)
_VALIDATION_FAILED = gettext_lazy(
    "Connection validation failed. Check the private credentials and site settings."
)
_PROVIDER_ANSWERED_WITH_ERROR = gettext_lazy(
    "The provider answered with an error. Try validating again later."
)
_REFUSED_REQUEST = gettext_lazy(
    "The provider refused the request. Check the resource ID and site settings, "
    "then validate again."
)
_FAILURES_WITHOUT_STATUS = {
    "provider_request_failed": gettext_lazy(
        "The provider could not be reached. Try validating again in a few minutes."
    ),
    "invalid_site_id": SITE_ID_REFUSED,
    "invalid_provider_secret": gettext_lazy(
        "The saved API key cannot be read. Enter the key again, then validate."
    ),
    "invalid_provider_endpoint": _UNREADABLE_ANSWER,
    "invalid_provider_schema": _UNREADABLE_ANSWER,
    "invalid_provider_interval": _UNREADABLE_ANSWER,
    "unsafe_provider_response": _UNREADABLE_ANSWER,
    "provider_response_too_large": _UNREADABLE_ANSWER,
    "fixture_account_required": _UNSUPPORTED_ACCOUNT,
    "fixture_provider_disabled": _UNSUPPORTED_ACCOUNT,
    "unsupported_provider": _UNSUPPORTED_ACCOUNT,
}
_FAILURES_BY_STATUS = {
    401: _REJECTED_KEY,
    403: _REJECTED_KEY,
    404: gettext_lazy(
        "The provider found no site with this resource ID for this API key. Check the "
        "resource ID and that the key belongs to the account that owns the site."
    ),
    429: gettext_lazy("The provider is limiting requests. Wait before validating again."),
}


def validation_failure_message(error: ProviderError) -> str:
    status = error.status_code
    if status is None:
        message = _FAILURES_WITHOUT_STATUS.get(error.code, _VALIDATION_FAILED)
    elif status >= 500:
        message = _PROVIDER_ANSWERED_WITH_ERROR
    else:
        message = _FAILURES_BY_STATUS.get(status, _REFUSED_REQUEST)
    return str(message)


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


BINDING_FIELDS = tuple(field.name for field in fields(BindingSpec))


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
        help_text=gettext_lazy("Write only. An existing account's key is replaced on its page."),
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
    site_id = forms.RegexField(
        rf"\A{SITE_ID_PATTERN}\Z",
        max_length=200,
        label=gettext_lazy("Solcast resource ID"),
        error_messages={"invalid": SITE_ID_REFUSED},
    )
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
        return data

    @sensitive_variables()
    def account(self) -> NewAccount | ExistingAccount:
        data = self.cleaned_data
        if data["account_mode"] == "new":
            return NewAccount(
                external_account_id=data["external_account_id"],
                api_key=data["api_key"],
                daily_quota=data["daily_quota"],
                authorization_reference=data["authorization_reference"],
            )
        account_id, revision = data["existing_account"].split(":")
        return ExistingAccount(int(account_id), int(revision))

    def binding(self) -> BindingSpec:
        return BindingSpec(**{name: self.cleaned_data[name] for name in BINDING_FIELDS})


class BindingActionForm(forms.Form):
    expected_revision = forms.IntegerField(min_value=1, widget=forms.HiddenInput)
    binding_id = forms.IntegerField(min_value=1, widget=forms.HiddenInput)
    credential_revision = forms.IntegerField(min_value=1, widget=forms.HiddenInput)
    expected_validated_at = forms.DateTimeField(required=False, widget=forms.HiddenInput)
    expected_status = forms.ChoiceField(
        choices=RegionStatus.choices, required=False, widget=forms.HiddenInput
    )
    declaration = forms.CharField(max_length=64, required=False, widget=forms.HiddenInput)

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


class ConfirmationForm(SecondFactorReauthenticateInput):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.fields["code"].label = gettext_lazy("Authenticator or recovery code")


class WorkspaceSettingsForm(forms.Form):
    max_recipients = forms.IntegerField(
        min_value=1, max_value=MAX_RECIPIENT_LIMIT, label=gettext_lazy("Recipients per plant")
    )
    expected_max_recipients = forms.IntegerField(min_value=1, widget=forms.HiddenInput)


class ReviewDecisionForm(forms.Form):
    expected_revision = forms.IntegerField(min_value=1, widget=forms.HiddenInput)
    expected_status = forms.ChoiceField(choices=RegionStatus.choices, widget=forms.HiddenInput)
    location_change_id = forms.IntegerField(min_value=1, required=False, widget=forms.HiddenInput)
    action = forms.ChoiceField(
        choices=tuple(
            (name, name) for name in ("reject", "suspend", "approve_location", "reject_location")
        ),
        widget=forms.HiddenInput,
    )
    decision_reason = forms.CharField(
        required=False,
        max_length=500,
        label=gettext_lazy("Note for the customer (optional)"),
        widget=forms.Textarea(attrs={"rows": 2, "maxlength": 500}),
    )

    def seen(self) -> ReviewSnapshot:
        return ReviewSnapshot(
            status=self.cleaned_data["expected_status"],
            revision=self.cleaned_data["expected_revision"],
        )
