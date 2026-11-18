import hashlib
import json
from functools import cache
from typing import TYPE_CHECKING, Any, cast
from zoneinfo import available_timezones

from django import forms
from django.contrib import messages
from django.contrib.auth.models import User
from django.db import models, transaction
from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy
from django.views.decorators.http import require_http_methods

from pyracast.accounts.authorization import require_platform_admin
from pyracast.accounts.models import Tenant
from pyracast.admin_forms import form_data, form_page, submitted
from pyracast.errors import ConflictError, ErrorCode
from pyracast.plants.models import (
    CONFIGURATION_PARAMETERS,
    MeasurementBasis,
    MountingType,
    Plant,
    PlantSubmission,
)
from pyracast.plants.operations import revise_plant
from pyracast.plants.site_api import SiteInput
from pyracast.plants.sites import submit_site
from pyracast.regions.models import LocationChangeStatus, Region, RegionLocationChangeRequest
from pyracast.regions.operations import canonical_coordinate, correct_location

if TYPE_CHECKING:
    from django_stubs_ext import StrOrPromise

    from pyracast.admin import PlatformAdminSite

SITE_FIELDS = frozenset(SiteInput().fields)
PARAMETERS = ("name", *CONFIGURATION_PARAMETERS)


@cache
def timezone_options() -> tuple[str, ...]:
    return tuple(sorted(available_timezones()))


def _number(
    label: StrOrPromise, *, required: bool = True, help_text: StrOrPromise = ""
) -> forms.CharField:
    return forms.CharField(
        label=label,
        required=required,
        help_text=help_text,
        widget=forms.TextInput(attrs={"inputmode": "decimal", "autocomplete": "off"}),
    )


def _choices(vocabulary: type[models.TextChoices]) -> list[tuple[str, StrOrPromise]]:
    return [(value, gettext_lazy(label)) for value, label in vocabulary.choices]


def _text(value: object) -> str:
    if value is None:
        return ""
    text = str(value)
    return text.removesuffix(".0") if isinstance(value, float) else text


class SiteForm(forms.Form):
    name = forms.CharField(label=gettext_lazy("Plant name"))
    latitude = forms.CharField(
        label=gettext_lazy("Latitude (°)"),
        widget=forms.TextInput(
            attrs={
                "inputmode": "decimal",
                "autocomplete": "off",
                "placeholder": "41.0082",
                "data-coordinate-pair": "longitude",
            }
        ),
    )
    longitude = forms.CharField(
        label=gettext_lazy("Longitude (°)"),
        widget=forms.TextInput(
            attrs={"inputmode": "decimal", "autocomplete": "off", "placeholder": "28.9784"}
        ),
    )
    timezone = forms.CharField(
        label=gettext_lazy("Timezone"),
        help_text=gettext_lazy("IANA name, for example Europe/Istanbul."),
        widget=forms.TextInput(
            attrs={"list": "timezone-options", "autocomplete": "off", "spellcheck": "false"}
        ),
    )
    dc_capacity_mw = _number(gettext_lazy("DC capacity (MW)"))
    inverter_capacity_mw = _number(
        gettext_lazy("Inverter AC capacity (MW, optional)"), required=False
    )
    export_limit_mw = _number(gettext_lazy("Grid export limit (MW, optional)"), required=False)
    measurement_basis = forms.ChoiceField(
        label=gettext_lazy("Measurement point"), choices=_choices(MeasurementBasis)
    )
    mounting_type = forms.ChoiceField(
        label=gettext_lazy("Mounting type"),
        choices=_choices(MountingType),
        widget=forms.Select(attrs={"data-mounting": ""}),
    )
    surface_tilt_degrees = _number(gettext_lazy("Tilt (°, optional)"), required=False)
    surface_azimuth_degrees = _number(
        gettext_lazy("Azimuth (°, optional)"),
        required=False,
        help_text=gettext_lazy("Clockwise from north; south is 180."),
    )
    site: dict[str, Any]

    def clean(self) -> dict[str, Any]:
        data = super().clean() or {}
        site = SiteInput(
            data={
                name: value
                for name, value in data.items()
                if name in SITE_FIELDS and value not in (None, "")
            }
        )
        if site.is_valid():
            self.site = dict(site.validated_data)
        else:
            for name, errors in site.errors.items():
                field = name if name in self.fields else None
                if field not in self.errors:
                    self.add_error(field, errors)
        return data


class PlantDetailsForm(SiteForm):
    reason = forms.CharField(
        required=False,
        label=gettext_lazy("Reason for a location correction (optional)"),
        widget=forms.Textarea(attrs={"rows": 3}),
    )
    expected_version = forms.CharField(widget=forms.HiddenInput)


def plant_details(region: Region, plant: Plant) -> dict[str, Any]:
    parameters = plant.current_configuration or PlantSubmission.objects.filter(plant=plant).first()
    return {
        "name": plant.name,
        "latitude": region.latitude,
        "longitude": region.longitude,
        "timezone": region.timezone,
        **{
            name: getattr(parameters, name) if parameters else None
            for name in CONFIGURATION_PARAMETERS
        },
    }


def details_version(region: Region, details: dict[str, Any]) -> str:
    state = json.dumps([region.revision, details], sort_keys=True, default=str)
    return hashlib.sha256(state.encode()).hexdigest()


def save_plant_details(actor: User, region_id: int, form: PlantDetailsForm) -> tuple[bool, bool]:
    require_platform_admin(actor)
    site = form.site
    with transaction.atomic():
        tenant_id = Region.objects.active().values_list("tenant_id", flat=True).get(pk=region_id)
        Tenant.objects.select_for_update().get(pk=tenant_id)
        region = Region.objects.get(pk=region_id)
        plant = Plant.objects.select_related("current_configuration").get(region=region)
        current = plant_details(region, plant)
        if details_version(region, current) != form.cleaned_data["expected_version"]:
            raise ConflictError(
                ErrorCode.CONFLICT,
                _(
                    "This plant changed after you opened this page. "
                    "Reload it to see the current details."
                ),
            )
        parameters = {name: site[name] for name in PARAMETERS}
        location = (
            canonical_coordinate(site["latitude"]),
            canonical_coordinate(site["longitude"]),
            site["timezone"],
        )
        parameters_changed = parameters != {name: current[name] for name in PARAMETERS}
        location_changed = location != (region.latitude, region.longitude, region.timezone)
        if parameters_changed:
            revise_plant(actor, tenant_id, region.pk, plant.pk, platform=True, **parameters)
        if location_changed:
            correct_location(
                actor,
                region.pk,
                expected_revision=region.revision,
                latitude=site["latitude"],
                longitude=site["longitude"],
                timezone=site["timezone"],
                reason=form.cleaned_data["reason"],
            )
    return parameters_changed, location_changed


@require_http_methods(["GET", "POST"])
def new_plant(request: HttpRequest, tenant_id: int, *, site: PlatformAdminSite) -> HttpResponse:
    tenant = get_object_or_404(Tenant, pk=tenant_id)
    form = SiteForm(
        form_data(request),
        initial={
            "timezone": "Europe/Istanbul",
            "measurement_basis": MeasurementBasis.NET_EXPORT,
            "mounting_type": MountingType.UNKNOWN,
        },
    )

    def create() -> HttpResponse:
        region = submit_site(cast(User, request.user), tenant.pk, platform=True, **form.site)
        messages.success(
            request,
            _(
                "Plant %(name)s created. Next, set up its provider connection, "
                "then validate and approve it."
            )
            % {"name": region.name},
        )
        return redirect("admin:workspace_plant", object_id=region.pk)

    return submitted(request, form, create) or form_page(
        site,
        request,
        "plant_new",
        _("Add plant"),
        {"tenant": tenant, "form": form, "timezones": timezone_options()},
    )


@require_http_methods(["GET", "POST"])
def plant_details_page(
    request: HttpRequest, object_id: int, *, site: PlatformAdminSite
) -> HttpResponse:
    region = get_object_or_404(Region.objects.active().select_related("tenant"), pk=object_id)
    plant = get_object_or_404(Plant.objects.select_related("current_configuration"), region=region)
    details = plant_details(region, plant)
    form = PlantDetailsForm(
        form_data(request),
        initial={
            **{name: _text(value) for name, value in details.items()},
            "expected_version": details_version(region, details),
        },
    )

    def save() -> HttpResponse:
        parameters_changed, location_changed = save_plant_details(
            cast(User, request.user), region.pk, form
        )
        if location_changed:
            messages.success(
                request,
                _(
                    "Plant details saved. The plant is back in review with its corrected "
                    "location. Set up its provider connection again."
                ),
            )
        elif parameters_changed:
            messages.success(request, _("Plant details saved."))
        else:
            messages.info(request, _("Nothing changed."))
        return redirect("admin:workspace_plant", object_id=region.pk)

    return submitted(request, form, save) or form_page(
        site,
        request,
        "plant_details",
        _("Edit plant details"),
        {
            "region": region,
            "plant": plant,
            "form": form,
            "timezones": timezone_options(),
            "location_request_pending": RegionLocationChangeRequest.objects.filter(
                region=region, status=LocationChangeStatus.PENDING
            ).exists(),
        },
        section="plants",
    )
