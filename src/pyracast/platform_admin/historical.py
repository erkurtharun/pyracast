from dataclasses import dataclass
from hashlib import sha256
from typing import TYPE_CHECKING, Any, cast

from django import forms
from django.contrib import messages
from django.contrib.auth.models import User
from django.core import signing
from django.core.exceptions import ObjectDoesNotExist, ValidationError
from django.core.files.uploadedfile import UploadedFile
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect
from django.template.loader import render_to_string
from django.template.response import TemplateResponse
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy
from django.views.decorators.http import require_http_methods

from pyracast.connections.models import SiteBinding
from pyracast.forecasts.historical_upload import (
    MAX_UPLOAD_BYTES,
    HistoricalUpload,
    HistoricalUploadPreview,
    import_historical_upload,
    parse_historical_upload,
    preview_historical_upload,
)
from pyracast.forecasts.trust import problem_messages, reference_problems
from pyracast.plants.models import Plant
from pyracast.platform_admin.failures import wants_json
from pyracast.regions.models import Region

if TYPE_CHECKING:
    from pyracast.platform_admin.site import PlatformAdminSite

PREVIEW_SALT = "historical-upload-preview-v1"
SOURCE_LABELS = {
    "latitude": gettext_lazy("Latitude"),
    "longitude": gettext_lazy("Longitude"),
    "capacity_kw": gettext_lazy("Reference capacity"),
    "dc_capacity_kw": gettext_lazy("DC capacity"),
    "tilt_degrees": gettext_lazy("Tilt"),
    "azimuth_degrees": gettext_lazy("Azimuth"),
    "losses_percent": gettext_lazy("Losses"),
    "mounting_type": gettext_lazy("Mounting"),
    "unit": gettext_lazy("Unit"),
    "timestamp_convention": gettext_lazy("Timestamp convention"),
    "timezone": gettext_lazy("Source timezone"),
    "site_timezone": gettext_lazy("Plant timezone"),
    "site_id": gettext_lazy("Resource ID"),
    "power_basis": gettext_lazy("Power basis"),
    "period": gettext_lazy("Interval"),
    "product": gettext_lazy("Product"),
}
CHECK_LABELS = {
    "power_within_capacity": gettext_lazy("Power stays within the reference capacity"),
    "power_not_implausibly_small": gettext_lazy("Power is plausible for the reference capacity"),
    "not_imported_before": gettext_lazy("The file was not imported before"),
}
CHANGED_MEANWHILE = gettext_lazy(
    "The plant or its connection changed while saving, so nothing was saved. Reload this page."
)


class HistoricalUploadForm(forms.Form):
    action = forms.ChoiceField(required=False, choices=(("preview", "preview"), ("save", "save")))
    file = forms.FileField(
        label=gettext_lazy("Historical Solcast file"),
        widget=forms.ClearableFileInput(attrs={"accept": ".json,.csv", "data-upload-file": ""}),
    )
    source_timezone = forms.CharField(
        required=False,
        max_length=100,
        label=gettext_lazy("Source timezone, only for timestamps without an offset"),
        widget=forms.TextInput(attrs={"placeholder": "Europe/Istanbul"}),
    )
    period = forms.ChoiceField(
        required=False,
        label=gettext_lazy("Interval, only when missing from the file"),
        choices=(
            ("", gettext_lazy("Read from file")),
            ("PT30M", gettext_lazy("30 minutes")),
            ("PT60M", gettext_lazy("60 minutes")),
        ),
    )
    preview_token = forms.CharField(required=False, widget=forms.HiddenInput)
    settings_confirmed = forms.BooleanField(
        required=False,
        label=gettext_lazy(
            "This is historical model data for the selected reference configuration."
        ),
    )

    def clean_file(self) -> UploadedFile[Any]:
        upload = cast("UploadedFile[Any]", self.cleaned_data["file"])
        if upload.size is None or upload.size > MAX_UPLOAD_BYTES:
            raise ValidationError(_("Select a file no larger than 7 MiB."))
        return upload


@dataclass
class _Attempt:
    file_name: str
    identity: dict[str, Any]
    upload: HistoricalUpload
    preview: HistoricalUploadPreview
    token: str = ""


def _approved_binding(region: Region) -> SiteBinding | None:
    binding = (
        SiteBinding.objects.select_related("provider_account")
        .filter(region=region, region_revision=region.revision, pk=region.approved_binding_id or 0)
        .first()
    )
    if binding is not None:
        binding.region = region
    return binding


def _blocking_problems(binding: SiteBinding | None) -> list[str]:
    if binding is None:
        return [_("Approve the current Solcast configuration before importing.")]
    return problem_messages(reference_problems(binding))


def _configuration_identity(region: Region, binding: SiteBinding) -> dict[str, Any]:
    configurations = list(
        Plant.objects.filter(region=region)
        .order_by("pk")
        .values_list("pk", "current_configuration_id")
    )
    return {
        "region": region.pk,
        "revision": region.revision,
        "binding": binding.pk,
        "approved_binding": region.approved_binding_id,
        "configurations": [list(value) for value in configurations],
        "timezone": region.timezone,
        "reference_settings": {
            name: str(getattr(binding, name))
            for name in (
                "site_id",
                "latitude",
                "longitude",
                "capacity_kw",
                "dc_capacity_kw",
                "azimuth_degrees",
                "losses_percent",
                "power_basis",
            )
        },
    }


def _attempt(
    request: HttpRequest, region: Region, binding: SiteBinding, form: HistoricalUploadForm
) -> _Attempt:
    file = form.cleaned_data["file"]
    content = file.read(MAX_UPLOAD_BYTES + 1)
    source_timezone = form.cleaned_data["source_timezone"] or None
    period = form.cleaned_data["period"] or None
    upload = parse_historical_upload(
        content, file.name, source_timezone=source_timezone, period=period
    )
    return _Attempt(
        file.name,
        {
            **_configuration_identity(region, binding),
            "actor": request.user.pk,
            "session": sha256((request.session.session_key or "").encode()).hexdigest(),
            "digest": sha256(content).hexdigest(),
            "filename": file.name,
            "source_timezone": source_timezone,
            "period": period,
        },
        upload,
        preview_historical_upload(binding, upload),
    )


def _save(
    request: HttpRequest,
    region: Region,
    binding: SiteBinding,
    form: HistoricalUploadForm,
    attempt: _Attempt,
) -> HttpResponse:
    try:
        expected = signing.loads(
            form.cleaned_data["preview_token"], salt=PREVIEW_SALT, max_age=1800
        )
    except signing.BadSignature as error:
        raise ValidationError(
            _("The preview expired or is invalid. Preview the file again.")
        ) from error
    if expected != attempt.identity:
        raise ValidationError(
            _("The file or selected configuration changed. Preview the file again.")
        )
    if not form.cleaned_data["settings_confirmed"]:
        raise ValidationError(_("Confirm the selected reference configuration."))
    if attempt.preview.conflicts and attempt.preview.previous_import is None:
        raise ValidationError(_("Resolve the file conflicts before importing."))
    record, created = import_historical_upload(
        cast(User, request.user),
        region.tenant_id,
        region.pk,
        binding.pk,
        attempt.upload,
        attempt.file_name,
        settings_confirmed=True,
        expected_configuration_ids=tuple(
            (int(value[0]), value[1]) for value in expected["configurations"]
        ),
    )
    if wants_json(request):
        return JsonResponse(
            {
                "saved": True,
                "created": created,
                "import_id": record.pk,
                "interval_count": record.interval_count,
            }
        )
    messages.success(
        request,
        _("Historical data saved.") if created else _("This file was already imported."),
    )
    return redirect("admin:workspace_historical_import", region_id=region.pk)


def _render(
    request: HttpRequest,
    site: PlatformAdminSite,
    region: Region,
    binding: SiteBinding | None,
    form: HistoricalUploadForm,
    problems: list[str],
    attempt: _Attempt | None,
    *,
    status: int,
) -> HttpResponse:
    preview = attempt.preview if attempt else None
    upload = attempt.upload if attempt else None
    context = {
        **site.each_context(request),
        "workspace_section": "plants",
        "region": region,
        "binding": binding,
        "problems": problems,
        "form": form,
        "preview": preview,
        "verified_labels": [str(SOURCE_LABELS.get(key, key)) for key in preview.verified_fields]
        if preview
        else [],
        "missing_labels": [str(SOURCE_LABELS.get(key, key)) for key in preview.missing_fields]
        if preview
        else [],
        "checks": [(CHECK_LABELS[name], passed) for name, passed in preview.checks]
        if preview
        else [],
        "preview_token": attempt.token if attempt else "",
        "file_name": attempt.file_name if attempt else "",
        "source_timezone": (upload.naive_timezone if upload else None)
        or _("Timestamp offsets in file"),
        "title": _("Import historical Solcast data"),
    }
    if request.method == "POST" and wants_json(request):
        return JsonResponse(
            {
                "html": render_to_string(
                    "admin/workspace/historical_preview.html", context, request
                ),
                "token": context["preview_token"],
                "can_save": bool(context["preview_token"]),
            },
            status=status,
        )
    return TemplateResponse(
        request, "admin/workspace/historical_import.html", context, status=status
    )


@require_http_methods(["GET", "POST"])
def historical_import(
    request: HttpRequest, region_id: int, *, site: PlatformAdminSite
) -> HttpResponse:
    region = get_object_or_404(Region.objects.active().select_related("tenant"), pk=region_id)
    binding = _approved_binding(region)
    problems = _blocking_problems(binding)
    form = HistoricalUploadForm(request.POST or None, request.FILES or None)
    attempt = None
    status = 200
    if request.method == "POST":
        status = 400
        if binding is None or problems:
            form.add_error(None, problems)
        elif form.is_valid():
            try:
                attempt = _attempt(request, region, binding, form)
                if form.cleaned_data["action"] == "save":
                    return _save(request, region, binding, form, attempt)
                status = 200
                if not attempt.preview.conflicts:
                    attempt.token = signing.dumps(
                        attempt.identity, salt=PREVIEW_SALT, compress=True
                    )
            except ValidationError as error:
                form.add_error(None, error)
            except ObjectDoesNotExist:
                form.add_error(None, str(CHANGED_MEANWHILE))
                status = 409
    return _render(request, site, region, binding, form, problems, attempt, status=status)
