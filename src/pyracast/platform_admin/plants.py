import hashlib
import json
import re
from decimal import Decimal
from functools import cache
from typing import TYPE_CHECKING, Any, cast

from django import forms
from django.contrib import messages
from django.contrib.auth.models import User
from django.db import transaction
from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy
from django.views.decorators.http import require_http_methods
from rest_framework import serializers

from pyracast.accounts.authorization import require_platform_admin
from pyracast.accounts.models import Tenant
from pyracast.errors import ConflictError, ErrorCode
from pyracast.plants.models import (
    CONFIGURATION_PARAMETERS,
    MeasurementBasis,
    MountingType,
    Plant,
)
from pyracast.plants.operations import revise_plant
from pyracast.plants.parameters import declaration, declared_parameters
from pyracast.plants.site_api import SiteInput
from pyracast.plants.sites import submit_site
from pyracast.platform_admin.forms import form_data, form_page, submitted
from pyracast.regions.models import LocationChangeStatus, Region, RegionLocationChangeRequest
from pyracast.regions.operations import available_zones, canonical_coordinate, correct_location

if TYPE_CHECKING:
    from django_stubs_ext import StrOrPromise

    from pyracast.platform_admin.site import PlatformAdminSite

SITE_FIELDS = frozenset(SiteInput().fields)


@cache
def timezone_options() -> tuple[str, ...]:
    return tuple(sorted(available_zones()))


def _number(
    label: StrOrPromise, *, required: bool = True, help_text: StrOrPromise = ""
) -> forms.CharField:
    return forms.CharField(
        label=label,
        required=required,
        help_text=help_text,
        widget=forms.TextInput(attrs={"inputmode": "decimal", "autocomplete": "off"}),
    )


# The workspace's coordinate fields read a value the same way (web site-location.ts
# coordinateValue); the bounds stay with RegionInput and its messages.
COORDINATE_NUMBER = re.compile(r"-?(?:0|[1-9]\d*)(?:[.,]\d+)?", re.ASCII)


def coordinate_number(text: str) -> float:
    if not COORDINATE_NUMBER.fullmatch(text):
        raise forms.ValidationError(
            serializers.FloatField.default_error_messages["invalid"], code="invalid"
        )
    return float(text.replace(",", "."))


def _text(value: object) -> str:
    if value is None:
        return ""
    text = str(value)
    if not isinstance(value, float):
        return text
    # The form's number rule refuses an exponent, which str() writes below 1e-4 and from 1e16.
    if "e" in text:
        text = format(Decimal(text), "f")
    return text.removesuffix(".0")


class SiteForm(forms.Form):
    name = forms.CharField(label=gettext_lazy("Plant name"))
    latitude = forms.CharField(
        label=gettext_lazy("Latitude (°)"),
        widget=forms.TextInput(
            attrs={
                "autocomplete": "off",
                "placeholder": "41.0082",
                "data-coordinate-pair": "longitude",
            }
        ),
    )
    longitude = forms.CharField(
        label=gettext_lazy("Longitude (°)"),
        widget=forms.TextInput(attrs={"autocomplete": "off", "placeholder": "28.9784"}),
    )
    timezone = forms.CharField(
        label=gettext_lazy("Time zone"),
        help_text=gettext_lazy("IANA name, for example Europe/Istanbul."),
        widget=forms.TextInput(
            attrs={"list": "timezone-options", "autocomplete": "off", "spellcheck": "false"}
        ),
    )
    dc_capacity_mw = _number(gettext_lazy("Panel capacity (MWp)"))
    inverter_capacity_mw = _number(
        gettext_lazy("Inverter capacity (MW AC, optional)"), required=False
    )
    export_limit_mw = _number(gettext_lazy("Grid export limit (MW, optional)"), required=False)
    measurement_basis = forms.ChoiceField(
        label=gettext_lazy("Meter point"), choices=MeasurementBasis.choices
    )
    mounting_type = forms.ChoiceField(
        label=gettext_lazy("Mounting"),
        choices=MountingType.choices,
        widget=forms.Select(attrs={"data-mounting": ""}),
    )
    surface_tilt_degrees = _number(gettext_lazy("Panel tilt (°, optional)"), required=False)
    surface_azimuth_degrees = _number(
        gettext_lazy("Panel azimuth (°, optional)"),
        required=False,
        help_text=gettext_lazy("Clockwise from north; south is 180."),
    )
    site: SiteInput

    def clean_latitude(self) -> float:
        return coordinate_number(self.cleaned_data["latitude"])

    def clean_longitude(self) -> float:
        return coordinate_number(self.cleaned_data["longitude"])

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
            self.site = site
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
    record = declaration(plant)
    return {
        "name": plant.name,
        "latitude": region.latitude,
        "longitude": region.longitude,
        "timezone": region.timezone,
        **{name: getattr(record, name) if record else None for name in CONFIGURATION_PARAMETERS},
    }


def details_version(region: Region, details: dict[str, Any]) -> str:
    """The review status is part of the version: an approval keeps the revision and the
    values but turns an edit of the submission into a live configuration."""
    state = json.dumps([region.revision, region.status, details], sort_keys=True, default=str)
    return hashlib.sha256(state.encode()).hexdigest()


def save_plant_details(actor: User, region_id: int, form: PlantDetailsForm) -> tuple[bool, bool]:
    require_platform_admin(actor)
    site = form.site
    with transaction.atomic():
        tenant_id = Region.objects.active().values_list("tenant_id", flat=True).get(pk=region_id)
        Tenant.objects.select_for_update().get(pk=tenant_id)
        region = Region.objects.active().select_for_update().get(pk=region_id)
        plant = (
            Plant.objects.select_for_update(of=("self",))
            .select_related("current_configuration", "submission")
            .get(region=region)
        )
        if (
            details_version(region, plant_details(region, plant))
            != (form.cleaned_data["expected_version"])
        ):
            raise ConflictError(
                ErrorCode.CONFLICT,
                _(
                    "This plant changed after you opened this page. "
                    "Reload it to see the current details."
                ),
            )
        parameters = site.parameters()
        location = site.location()
        parameters_changed = parameters != declared_parameters(plant)
        location_changed = (
            canonical_coordinate(location["latitude"]),
            canonical_coordinate(location["longitude"]),
            location["timezone"],
        ) != (region.latitude, region.longitude, region.timezone)
        if parameters_changed:
            revise_plant(
                actor,
                tenant_id,
                region.pk,
                plant.pk,
                parameters,
                expected_revision=plant.parameters_revision,
                platform=True,
            )
        if location_changed:
            correct_location(
                actor,
                region.pk,
                expected_revision=region.revision,
                reason=form.cleaned_data["reason"],
                **location,
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
        region = submit_site(
            cast(User, request.user),
            tenant.pk,
            form.site.parameters(),
            platform=True,
            **form.site.location(),
        )
        messages.success(
            request,
            _(
                "Plant %(name)s created. Next, set up its provider connection, "
                "then validate and approve it."
            )
            % {"name": region.name},
        )
        return redirect("admin:workspace_plant", region_id=region.pk)

    return submitted(request, form, create) or form_page(
        site,
        request,
        "plant_new",
        _("Add plant"),
        {"tenant": tenant, "form": form, "timezones": timezone_options()},
    )


@require_http_methods(["GET", "POST"])
def plant_details_page(
    request: HttpRequest, region_id: int, *, site: PlatformAdminSite
) -> HttpResponse:
    region = get_object_or_404(Region.objects.active().select_related("tenant"), pk=region_id)
    plant = get_object_or_404(
        Plant.objects.select_related("current_configuration", "submission"), region=region
    )
    details = plant_details(region, plant)
    form = PlantDetailsForm(
        form_data(request),
        initial={
            **{name: _text(value) for name, value in details.items()},
            "expected_version": details_version(region, details),
        },
    )

    def save() -> HttpResponse:
        try:
            parameters_changed, location_changed = save_plant_details(
                cast(User, request.user), region.pk, form
            )
        except (Tenant.DoesNotExist, Region.DoesNotExist, Plant.DoesNotExist) as missing:
            raise Http404 from missing
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
        return redirect("admin:workspace_plant", region_id=region.pk)

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
