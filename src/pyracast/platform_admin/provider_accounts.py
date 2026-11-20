from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import TYPE_CHECKING, Any, cast

from django import forms
from django.contrib import messages
from django.contrib.auth.models import User
from django.db.models import Count, F, Q
from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect
from django.utils import timezone
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy
from django.views.decorators.debug import sensitive_post_parameters, sensitive_variables
from django.views.decorators.http import require_POST, require_safe

from pyracast.connections.models import BindingHealth, Provider, ProviderAccount, SiteBinding
from pyracast.connections.operations import replace_api_key
from pyracast.connections.recovery import clear_provider_cooldown
from pyracast.forecasts.budget import remaining_forecast_requests
from pyracast.forecasts.models import RequestAttempt, RequestOutcome, RequestPurpose
from pyracast.forecasts.revalidation import stale_bindings
from pyracast.platform_admin.forms import submitted
from pyracast.regions.models import AuditEvent, RegionStatus, active_region

if TYPE_CHECKING:
    from pyracast.platform_admin.site import PlatformAdminSite


class KeyReplacementForm(forms.Form):
    expected_revision = forms.IntegerField(min_value=1, widget=forms.HiddenInput)
    api_key = forms.RegexField(
        r"\A[!-~]{1,1024}\Z",
        max_length=1024,
        strip=False,
        widget=forms.PasswordInput(render_value=False, attrs={"autocomplete": "new-password"}),
        label=gettext_lazy("New Solcast API key"),
        help_text=gettext_lazy("Write only. It is never shown again."),
        error_messages={
            "invalid": gettext_lazy("Enter an API key without spaces, up to 1024 characters.")
        },
    )
    confirm_replacement = forms.BooleanField(
        label=gettext_lazy("Replace the key for every plant that uses this account")
    )


class DelayClearanceForm(forms.Form):
    expected_revision = forms.IntegerField(min_value=1, widget=forms.HiddenInput)
    expected_blocked_until = forms.DateTimeField(required=False, widget=forms.HiddenInput)
    provider_recovered = forms.BooleanField(
        label=gettext_lazy("The provider has confirmed that this account can make requests again")
    )


@dataclass(frozen=True)
class Budget:
    quota: int
    used: int
    reserved: int

    @property
    def remaining(self) -> int:
        return max(self.quota - self.used, 0)


def _today(now: datetime) -> date:
    return now.astimezone(UTC).date()


def account_rows(now: datetime) -> list[dict[str, Any]]:
    accounts = ProviderAccount.objects.annotate(
        used_today=Count(
            "requestattempt", filter=Q(requestattempt__utc_day=_today(now)), distinct=True
        ),
        plants=Count(
            "sitebinding__region",
            filter=Q(
                sitebinding__region__deleted_at__isnull=True,
                sitebinding__region__approved_binding=F("sitebinding"),
            ),
            distinct=True,
        ),
    ).order_by("provider", "external_account_id", "pk")
    return [
        {
            "account": account,
            "plants": account.plants,
            "used_today": account.used_today,
            "remaining_today": max(account.daily_quota - account.used_today, 0),
            "key_rejected": account.authentication_failed_revision == account.credential_revision,
            "delayed": account.request_blocked_until is not None
            and account.request_blocked_until > now,
        }
        for account in accounts
    ]


def _bindings(account: ProviderAccount) -> list[dict[str, Any]]:
    bindings = (
        SiteBinding.objects.filter(
            active_region("region__"),
            provider_account=account,
            region__revision=F("region_revision"),
        )
        .select_related("region__tenant")
        .order_by("region__tenant__name", "region__name", "pk")
    )
    return [
        {
            "binding": binding,
            "region": binding.region,
            "tenant": binding.region.tenant,
            "status": RegionStatus(binding.region.status).label,
            "approved": binding.region.approved_binding_id == binding.pk,
            "current": binding.validated_credential_revision == account.credential_revision,
            "health": BindingHealth(binding.health).label,
        }
        for binding in bindings
    ]


def account_context(account: ProviderAccount, now: datetime) -> dict[str, Any]:
    attempts = RequestAttempt.objects.filter(provider_account=account, utc_day=_today(now))
    ledger: list[dict[str, Any]] = [
        {
            "purpose": purpose,
            "outcome": outcome,
            "count": count,
            "purpose_label": RequestPurpose(purpose).label,
            "outcome_label": RequestOutcome(outcome).label,
        }
        for purpose, outcome, count in attempts.values_list("purpose", "outcome")
        .annotate(count=Count("pk"))
        .order_by("purpose", "outcome")
    ]
    return {
        "account": account,
        "bindings": _bindings(account),
        "budget": Budget(
            account.daily_quota,
            sum(row["count"] for row in ledger),
            remaining_forecast_requests(account, now),
        ),
        "ledger": ledger,
        "key_rejected": account.authentication_failed_revision == account.credential_revision,
        "fingerprint_missing": account.provider == Provider.SOLCAST
        and bool(account.encrypted_api_key)
        and account.api_key_fingerprint is None,
        "delayed_until": account.request_blocked_until
        if account.request_blocked_until and account.request_blocked_until > now
        else None,
        "waiting_plants": stale_bindings(account).count(),
        "events": AuditEvent.objects.filter(provider_account=account)
        .select_related("actor")
        .order_by("-created_at", "-pk")[:20],
    }


def _actor(request: HttpRequest) -> User:
    return cast(User, request.user)


def _account_page(
    request: HttpRequest,
    site: PlatformAdminSite,
    account: ProviderAccount,
    *,
    key_form: KeyReplacementForm | None = None,
    delay_form: DelayClearanceForm | None = None,
) -> HttpResponse:
    blocked_until = account.request_blocked_until
    revision = {"expected_revision": account.credential_revision}
    return site.workspace_response(
        request,
        "provider_accounts",
        f"{account.get_provider_display()} · {account.external_account_id}",
        {
            **account_context(account, timezone.now()),
            "key_form": key_form or KeyReplacementForm(initial=revision),
            "delay_form": delay_form
            or DelayClearanceForm(
                initial={
                    **revision,
                    "expected_blocked_until": blocked_until.isoformat() if blocked_until else "",
                }
            ),
        },
        status=400 if request.method == "POST" else 200,
        template="provider_account",
    )


@require_safe
def accounts(request: HttpRequest, *, site: PlatformAdminSite) -> HttpResponse:
    return site.workspace_response(
        request,
        "provider_accounts",
        _("Provider accounts"),
        {"accounts": account_rows(timezone.now())},
    )


@require_safe
def account(request: HttpRequest, account_id: int, *, site: PlatformAdminSite) -> HttpResponse:
    return _account_page(request, site, get_object_or_404(ProviderAccount, pk=account_id))


@sensitive_variables()
@sensitive_post_parameters("api_key")
@require_POST
def key_replacement(
    request: HttpRequest, account_id: int, *, site: PlatformAdminSite
) -> HttpResponse:
    account = get_object_or_404(ProviderAccount, pk=account_id, provider=Provider.SOLCAST)
    form = KeyReplacementForm(request.POST)

    @sensitive_variables()
    def replace() -> HttpResponse:
        replace_api_key(
            _actor(request),
            account.pk,
            form.cleaned_data["api_key"],
            expected_revision=form.cleaned_data["expected_revision"],
        )
        messages.success(
            request,
            _(
                "API key replaced. The account's plants are revalidated automatically "
                "within the daily request budget."
            ),
        )
        return redirect("admin:workspace_provider_account", account_id=account.pk)

    return submitted(request, form, replace) or _account_page(request, site, account, key_form=form)


@require_POST
def delay_clearance(
    request: HttpRequest, account_id: int, *, site: PlatformAdminSite
) -> HttpResponse:
    account = get_object_or_404(ProviderAccount, pk=account_id)
    form = DelayClearanceForm(request.POST)

    def clear() -> HttpResponse:
        if clear_provider_cooldown(
            _actor(request),
            account.pk,
            form.cleaned_data["expected_revision"],
            expected_blocked_until=form.cleaned_data["expected_blocked_until"],
        ):
            messages.success(
                request,
                _(
                    "Provider wait cleared. "
                    "Queued collection can resume within the remaining quota."
                ),
            )
        else:
            messages.info(request, _("There is no provider wait to clear."))
        return redirect("admin:workspace_provider_account", account_id=account.pk)

    return submitted(request, form, clear) or _account_page(request, site, account, delay_form=form)
