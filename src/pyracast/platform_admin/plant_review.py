from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

from django import forms
from django.contrib import messages
from django.contrib.auth.models import User
from django.core.exceptions import ObjectDoesNotExist, ValidationError
from django.db.models import QuerySet
from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect
from django.template.response import TemplateResponse
from django.utils import timezone
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy
from django.views.decorators.debug import sensitive_post_parameters, sensitive_variables
from django.views.decorators.http import require_http_methods, require_POST
from rest_framework.exceptions import ValidationError as APIValidationError

from pyracast.connections.contracts import ProviderError
from pyracast.connections.models import SiteBinding
from pyracast.connections.recovery import clear_provider_cooldown
from pyracast.epias import EpiasCredentialsError, EpiasError
from pyracast.errors import DomainError, ErrorCode
from pyracast.forecasts.quota import QuotaExceededError, RequestDeferredError
from pyracast.forecasts.validation import validate_binding
from pyracast.metering.models import EpiasConnection
from pyracast.metering.operations import connect_epias, disconnect_epias
from pyracast.plants.models import Plant, PlantSubmission
from pyracast.plants.operations import configuration_is_current
from pyracast.platform_admin.forms import (
    BindingActionForm,
    EpiasConnectionForm,
    ProviderSetupForm,
    ReviewDecisionForm,
    form_data,
    validation_failure_message,
)
from pyracast.platform_admin.read_models import review_context
from pyracast.providers import adapter_for
from pyracast.regions.models import Region
from pyracast.regions.operations import approve_region

if TYPE_CHECKING:
    from pyracast.platform_admin.site import PlatformAdminSite

type Notice = tuple[int, str]
type Action = Callable[[HttpRequest, PlantPage], Notice | None]

SETUP_REFUSED = gettext_lazy(
    "The setup could not be applied. Check the values or reload if the plant or account changed."
)


@dataclass(frozen=True)
class PlantPage:
    region: Region
    binding: SiteBinding | None
    plants: QuerySet[Plant]
    plant: Plant | None
    epias_connection: EpiasConnection | None
    setup_form: ProviderSetupForm
    action_form: BindingActionForm
    epias_form: EpiasConnectionForm


def _actor(request: HttpRequest) -> User:
    return cast(User, request.user)


def _setup_initial(region: Region, binding: SiteBinding | None) -> dict[str, Any]:
    return {
        "expected_revision": region.revision,
        "expected_binding_id": binding.pk if binding else None,
        "account_mode": "existing" if binding else "new",
        "existing_account": (
            f"{binding.provider_account_id}:{binding.provider_account.credential_revision}"
            if binding
            else ""
        ),
        "site_id": binding.site_id if binding else "",
        "capacity_kw": binding.capacity_kw if binding else None,
        "dc_capacity_kw": binding.dc_capacity_kw if binding else None,
        "power_basis": binding.power_basis if binding else "ac",
        "installation_date": binding.installation_date if binding else None,
        "azimuth_degrees": binding.azimuth_degrees if binding else 0,
        "losses_percent": binding.losses_percent if binding else 0,
        "settings_verification_reference": (
            binding.settings_verification_reference if binding else ""
        ),
    }


def _action_initial(region: Region, binding: SiteBinding | None) -> dict[str, Any]:
    blocked_until = binding.provider_account.request_blocked_until if binding else None
    validated_at = binding.validated_at if binding else None
    return {
        "expected_revision": region.revision,
        "binding_id": binding.pk if binding else None,
        "credential_revision": binding.provider_account.credential_revision if binding else None,
        "expected_cooldown_until": blocked_until.isoformat() if blocked_until else "",
        "expected_validated_at": validated_at.isoformat() if validated_at else "",
    }


def _page(request: HttpRequest, region_id: int, action: str | None) -> PlantPage:
    region = get_object_or_404(Region.objects.active().select_related("tenant"), pk=region_id)
    binding = (
        SiteBinding.objects.select_related("provider_account")
        .filter(region=region, region_revision=region.revision)
        .first()
    )
    plants = Plant.objects.filter(region=region).select_related(
        "current_configuration__binding__region"
    )
    plant = plants.first()
    connection = EpiasConnection.objects.filter(plant=plant).first() if plant else None
    data = form_data(request)
    return PlantPage(
        region=region,
        binding=binding,
        plants=plants,
        plant=plant,
        epias_connection=connection,
        setup_form=ProviderSetupForm(
            data if action == "configure" else None, initial=_setup_initial(region, binding)
        ),
        action_form=BindingActionForm(
            None if action in {"configure", "epias_connect", "epias_disconnect"} else data,
            initial=_action_initial(region, binding),
        ),
        epias_form=EpiasConnectionForm(
            data if action == "epias_connect" else None,
            prefix="epias",
            initial={
                "provider_plant_id": connection.provider_plant_id if connection else None,
                "expected_eic": connection.eic if connection else "",
            },
        ),
    )


def _applied(form: forms.Form, operation: Callable[[], Notice | None]) -> Notice | None:
    if not form.is_valid():
        return None
    try:
        return operation()
    except RequestDeferredError as error:
        form.add_error(
            None,
            _("Update the private API key before validating this connection again.")
            if error.available_at is None
            else _("The provider is limiting requests. Wait before validating again."),
        )
    except QuotaExceededError:
        form.add_error(
            None, _("The provider's request budget is unavailable. Try validation later.")
        )
    except ProviderError as error:
        form.add_error(None, validation_failure_message(error))
    except ObjectDoesNotExist, ValidationError, APIValidationError:
        form.add_error(None, str(SETUP_REFUSED))
    return None


@sensitive_variables()
def configure(request: HttpRequest, page: PlantPage) -> Notice | None:
    form = page.setup_form

    @sensitive_variables()
    def save() -> Notice:
        saved = form.configure(_actor(request), page.region)
        if saved.pk != form.cleaned_data["expected_binding_id"]:
            return messages.SUCCESS, _(
                "Private provider setup saved. Validate the connection before approval."
            )
        if form.cleaned_data["api_key"]:
            return messages.SUCCESS, _(
                "API key replaced. Validate the connection to resume collection."
            )
        return messages.INFO, _("Nothing changed.")

    return _applied(form, save)


def validate(request: HttpRequest, page: PlantPage) -> Notice | None:
    form = page.action_form

    def run() -> Notice | None:
        current = form.current_binding(page.region)
        # The page names the validation it saw, so a repeated submit spends no second request.
        if current.validated_at != form.cleaned_data["expected_validated_at"]:
            form.add_error(
                None,
                _(
                    "This connection was validated after this page opened. "
                    "Reload the page before validating again."
                ),
            )
            return None
        validate_binding(_actor(request), current.pk, adapter_for(current), timezone.now())
        return messages.SUCCESS, _(
            "The current provider connection is validated. The plant can now be approved."
        )

    return _applied(form, run)


def approve(request: HttpRequest, page: PlantPage) -> Notice | None:
    form = page.action_form

    def run() -> Notice:
        current = form.current_binding(page.region)
        approve_region(_actor(request), page.region.pk, current.pk, timezone.now())
        return messages.SUCCESS, _("Plant approved. The first saved forecast is being prepared.")

    return _applied(form, run)


def clear_cooldown(request: HttpRequest, page: PlantPage) -> Notice | None:
    form = page.action_form
    form.fields["provider_recovered"].required = True

    def run() -> Notice:
        current = form.current_binding(page.region)
        clear_provider_cooldown(
            _actor(request),
            current.provider_account_id,
            form.cleaned_data["credential_revision"],
            expected_blocked_until=form.cleaned_data["expected_cooldown_until"],
        )
        return messages.SUCCESS, _(
            "Provider wait cleared. Queued collection can resume within the remaining quota."
        )

    return _applied(form, run)


def unknown(request: HttpRequest, page: PlantPage) -> None:
    if page.action_form.is_valid():
        page.action_form.add_error(None, str(SETUP_REFUSED))


def _epias(page: PlantPage, operation: Callable[[Plant], object], done: str) -> Notice | None:
    form = page.epias_form
    try:
        if page.plant is None:
            raise DomainError(ErrorCode.CONFIGURATION_UNAVAILABLE)
        operation(page.plant)
    except EpiasError as error:
        form.add_error(
            None,
            _("EPİAŞ service credentials are unavailable. Contact the deployment operator.")
            if isinstance(error, EpiasCredentialsError)
            else _("EPİAŞ is currently unavailable. Try connecting again later."),
        )
    except (ObjectDoesNotExist, ValidationError) as error:
        form.add_error(
            None,
            _("The EPİAŞ plant ID and EIC did not match. Check both identifiers.")
            if isinstance(error, DomainError)
            and error.code == ErrorCode.EPIAS_PLANT_IDENTITY_MISMATCH
            else _(
                "The plant configuration is unavailable or changed. "
                "Approve its current setup and reload before connecting."
            ),
        )
    else:
        return messages.SUCCESS, done
    return None


def epias_connect(request: HttpRequest, page: PlantPage) -> Notice | None:
    form = page.epias_form
    if not form.is_valid():
        return None
    return _epias(
        page,
        lambda plant: connect_epias(plant.pk, actor=_actor(request), **form.cleaned_data),
        _("EPİAŞ connection saved. Initial production collection is queued."),
    )


def epias_disconnect(request: HttpRequest, page: PlantPage) -> Notice | None:
    return _epias(
        page,
        lambda plant: disconnect_epias(plant.pk, actor=_actor(request)),
        _("EPİAŞ connection disconnected. Saved production remains available."),
    )


ACTIONS: dict[str, Action] = {
    "configure": configure,
    "validate": validate,
    "approve": approve,
    "clear_cooldown": clear_cooldown,
    "epias_connect": epias_connect,
    "epias_disconnect": epias_disconnect,
}


def _can_approve(binding: SiteBinding | None) -> bool:
    return bool(
        binding
        and binding.validated_at
        and binding.validated_credential_revision == binding.provider_account.credential_revision
        and binding.provider_account.enabled
        and binding.provider_account.authorized_at is not None
        and (
            binding.provider_account.provider != "solcast"
            or (binding.settings_verification_reference and binding.installation_date)
        )
    )


def _render(request: HttpRequest, site: PlatformAdminSite, page: PlantPage) -> TemplateResponse:
    binding = page.binding
    plant = page.plant
    return TemplateResponse(
        request,
        "admin/regions/region/setup.html",
        {
            **site.each_context(request),
            **review_context(page.region.pk),
            "workspace_section": "plants",
            "title": _("Review and set up %(name)s") % {"name": page.region.name},
            "region": page.region,
            "plants": page.plants,
            "submissions": PlantSubmission.objects.filter(plant__in=page.plants).select_related(
                "plant", "submitted_by"
            ),
            "binding": binding,
            "active_provider_delay": bool(
                binding
                and binding.provider_account.request_blocked_until
                and binding.provider_account.request_blocked_until > timezone.now()
            ),
            "setup_form": page.setup_form,
            "action_form": page.action_form,
            "epias_form": page.epias_form,
            "epias_connection": page.epias_connection,
            "can_connect_epias": bool(
                plant
                and plant.current_configuration
                and configuration_is_current(plant.current_configuration)
            ),
            "can_approve": _can_approve(binding),
        },
        status=400 if request.method == "POST" else 200,
    )


@sensitive_variables()
@sensitive_post_parameters("api_key")
@require_http_methods(["GET", "POST"])
def plant(request: HttpRequest, region_id: int, *, site: PlatformAdminSite) -> HttpResponse:
    action = request.POST.get("action") if request.method == "POST" else None
    page = _page(request, region_id, action)
    if request.method == "POST":
        notice = ACTIONS.get(action or "", unknown)(request, page)
        if notice is not None:
            messages.add_message(request, *notice)
            return redirect("admin:workspace_plant", region_id=page.region.pk)
    return _render(request, site, page)


@require_POST
def decision(request: HttpRequest, region_id: int) -> HttpResponse:
    get_object_or_404(Region.objects.active(), pk=region_id)
    form = ReviewDecisionForm(request.POST)
    if form.is_valid():
        try:
            form.apply(_actor(request), region_id)
        except DomainError as error:
            messages.error(request, " ".join(error.messages))
        except ObjectDoesNotExist, ValidationError:
            messages.error(
                request,
                _("The review could not be applied. Reload and review the current plant."),
            )
        else:
            messages.success(request, _("Plant review saved."))
    else:
        messages.error(
            request, _("Choose a review action and reload the current plant before continuing.")
        )
    return redirect("admin:workspace_plant", region_id=region_id)
