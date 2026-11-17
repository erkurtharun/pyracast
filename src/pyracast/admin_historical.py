from hashlib import sha256
from typing import TYPE_CHECKING, Any, cast

from django import forms
from django.contrib import messages
from django.contrib.auth.models import User
from django.core import signing
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import UploadedFile
from django.http import HttpRequest, HttpResponse, HttpResponseNotAllowed, JsonResponse
from django.shortcuts import get_object_or_404, redirect
from django.template.loader import render_to_string
from django.template.response import TemplateResponse
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy

from pyracast.connections.models import SiteBinding
from pyracast.forecasts.historical_upload import (
    MAX_UPLOAD_BYTES,
    import_historical_upload,
    parse_historical_upload,
    preview_historical_upload,
)
from pyracast.plants.models import Plant
from pyracast.regions.models import Region

if TYPE_CHECKING:
    from pyracast.admin import PlatformAdminSite

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


class HistoricalUploadForm(forms.Form):
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


def _configuration_identity(region: Region, binding: SiteBinding) -> dict[str, Any]:
    configurations = list(
        Plant.objects.filter(region=region, deleted_at__isnull=True)
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


def historical_import(
    site: PlatformAdminSite, request: HttpRequest, object_id: int
) -> HttpResponse:
    if request.method not in {"GET", "POST"}:
        return HttpResponseNotAllowed(("GET", "POST"))
    region = get_object_or_404(
        Region.objects.select_related("tenant"), pk=object_id, deleted_at__isnull=True
    )
    binding = (
        SiteBinding.objects.select_related("provider_account", "region")
        .filter(region=region, region_revision=region.revision, pk=region.approved_binding_id or 0)
        .first()
    )
    form = HistoricalUploadForm(request.POST or None, request.FILES or None)
    preview = None
    token = ""
    file_name = ""
    source_timezone_display = _("Timestamp offsets in file")
    action = request.POST.get("action", "preview")
    if request.method == "POST":
        if binding is None:
            form.add_error(None, _("Approve the current Solcast configuration before importing."))
        elif form.is_valid():
            file = form.cleaned_data["file"]
            content = file.read(MAX_UPLOAD_BYTES + 1)
            file_name = file.name
            source_timezone = form.cleaned_data["source_timezone"] or None
            period = form.cleaned_data["period"] or None
            identity = {
                **_configuration_identity(region, binding),
                "actor": request.user.pk,
                "session": sha256((request.session.session_key or "").encode()).hexdigest(),
                "digest": sha256(content).hexdigest(),
                "filename": file_name,
                "source_timezone": source_timezone,
                "period": period,
            }
            try:
                upload = parse_historical_upload(
                    content, file_name, source_timezone=source_timezone, period=period
                )
                source_timezone_display = upload.naive_timezone or _("Timestamp offsets in file")
                preview = preview_historical_upload(binding, upload)
                if action == "save":
                    try:
                        expected = signing.loads(
                            form.cleaned_data["preview_token"], salt=PREVIEW_SALT, max_age=1800
                        )
                    except signing.BadSignature as error:
                        raise ValidationError(
                            _("The preview expired or is invalid. Preview the file again.")
                        ) from error
                    if expected != identity:
                        raise ValidationError(
                            _("The file or selected configuration changed. Preview the file again.")
                        )
                    if not form.cleaned_data["settings_confirmed"]:
                        raise ValidationError(_("Confirm the selected reference configuration."))
                    if preview.conflicts:
                        raise ValidationError(_("Resolve the file conflicts before importing."))
                    imported = import_historical_upload(
                        cast(User, request.user),
                        region.tenant_id,
                        region.pk,
                        binding.pk,
                        content,
                        file_name,
                        source_timezone=source_timezone,
                        period=period,
                        settings_confirmed=True,
                        expected_configuration_ids=tuple(
                            (int(value[0]), value[1]) for value in expected["configurations"]
                        ),
                    )
                    messages.success(
                        request,
                        _("Historical data saved. Repeated uploads reuse the existing import."),
                    )
                    if request.headers.get("Accept") == "application/json":
                        return JsonResponse(
                            {
                                "saved": True,
                                "import_id": imported.pk,
                                "interval_count": preview.interval_count,
                            }
                        )
                    return redirect("admin:workspace_historical_import", object_id=region.pk)
                if action != "preview":
                    raise ValidationError(_("Choose an upload action."))
                token = signing.dumps(identity, salt=PREVIEW_SALT, compress=True)
            except ValidationError as error:
                form.add_error(None, error)
    context = {
        **site.each_context(request),
        "workspace_section": "plants",
        "region": region,
        "binding": binding,
        "form": form,
        "preview": preview,
        "verified_labels": [str(SOURCE_LABELS.get(key, key)) for key in preview.verified_fields]
        if preview
        else [],
        "missing_labels": [str(SOURCE_LABELS.get(key, key)) for key in preview.missing_fields]
        if preview
        else [],
        "preview_token": token,
        "file_name": file_name,
        "source_timezone": source_timezone_display,
        "title": _("Import historical Solcast data"),
    }
    status = 400 if form.errors else 200
    if request.method == "POST" and request.headers.get("Accept") == "application/json":
        return JsonResponse(
            {
                "html": render_to_string(
                    "admin/workspace/historical_preview.html", context, request
                ),
                "token": token,
                "can_save": bool(preview and not preview.conflicts and not form.errors),
            },
            status=status,
        )
    return TemplateResponse(
        request, "admin/workspace/historical_import.html", context, status=status
    )
