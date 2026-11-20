import json
from concurrent.futures import ThreadPoolExecutor
from io import StringIO
from pathlib import Path
from threading import Event
from typing import Any, cast

import pytest
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import OperationalError, connections, transaction
from django.test import Client
from django.urls import reverse
from psycopg.errors import LockNotAvailable

from pyracast.connections.models import ProviderAccount, SiteBinding
from pyracast.forecasts.historical_upload import (
    import_historical_upload,
    parse_historical_upload,
    preview_historical_upload,
)
from pyracast.forecasts.models import ForecastSnapshot, HistoricalReferenceImport
from pyracast.regions.models import Region
from tests.support.api import verified_admin
from tests.support.domain import System
from tests.support.historical_reference_import import system_for_import

pytestmark = pytest.mark.django_db(transaction=True)


def row(**updates: Any) -> dict[str, Any]:
    return {
        "period_end": "2026-11-07T12:30:00+03:00",
        "period": "PT30M",
        "pv_power_rooftop": 0.5,
    } | updates


def document(rows: object | None = None, **metadata: Any) -> bytes:
    return json.dumps({"estimated_actuals": [row()] if rows is None else rows} | metadata).encode()


def days(count: int) -> bytes:
    return document(
        [row(period_end=f"2026-10-{day:02d}T12:30:00+03:00") for day in range(1, count + 1)]
    )


def upload(system: System, content: bytes) -> HistoricalReferenceImport:
    return import_historical_upload(
        system.admin,
        system.tenant.pk,
        system.region.pk,
        system.binding.pk,
        content,
        "historical.json",
        settings_confirmed=True,
    )


def import_page(system: System) -> str:
    return reverse("admin:workspace_historical_import", args=(system.region.pk,))


def preview(client: Client, url: str, content: bytes, name: str = "export.json") -> dict[str, Any]:
    response = client.post(
        url,
        {"action": "preview", "file": SimpleUploadedFile(name, content)},
        headers={"Accept": "application/json"},
    )
    return cast(dict[str, Any], response.json())


def command(system: System, path: Path, *options: str) -> str:
    output = StringIO()
    call_command(
        "import_historical_reference",
        "--username",
        system.admin.username,
        "--tenant-id",
        str(system.tenant.pk),
        "--region-id",
        str(system.region.pk),
        "--file",
        str(path),
        "--reference",
        "Authorized export of the reference site",
        *options,
        stdout=output,
    )
    return output.getvalue()


def test_the_command_and_the_console_share_one_importer(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    system = system_for_import(monkeypatch)
    content = days(2)
    (tmp_path / "export.json").write_bytes(content)
    monkeypatch.chdir(tmp_path)

    output = command(system, Path("export.json"), "--confirm")

    record = HistoricalReferenceImport.objects.get()
    assert f"Historical reference import {record.pk}" in output
    client, _ = verified_admin()
    checked = preview(client, import_page(system), content, name="renamed.json")
    assert checked["can_save"] is False
    assert checked["token"] == ""
    assert "already imported" in checked["html"]
    assert HistoricalReferenceImport.objects.count() == 1


def test_the_command_imports_nothing_without_confirmation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    system = system_for_import(monkeypatch)
    (tmp_path / "export.json").write_bytes(days(1))
    monkeypatch.chdir(tmp_path)

    with pytest.raises(CommandError, match="--confirm"):
        command(system, Path("export.json"))

    assert not HistoricalReferenceImport.objects.exists()


def test_the_command_reads_only_files_inside_the_working_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    system = system_for_import(monkeypatch)
    outside = tmp_path / "outside.json"
    outside.write_bytes(days(1))
    inside = tmp_path / "inside"
    inside.mkdir()
    monkeypatch.chdir(inside)

    with pytest.raises(CommandError, match="working directory"):
        command(system, outside, "--confirm")

    assert not HistoricalReferenceImport.objects.exists()


@pytest.mark.parametrize(
    "content",
    [
        b'{"estimated_actuals": [], "estimated_actuals": []}',
        b'{"estimated_actuals": [{"period_end": "2026-11-07T12:30:00+03:00", '
        b'"period": "PT30M", "pv_power_rooftop": NaN}]}',
        b"[" * 100_000,
    ],
    ids=["duplicate-key", "nonfinite", "nesting"],
)
def test_the_command_refuses_a_malformed_export(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, content: bytes
) -> None:
    system = system_for_import(monkeypatch)
    (tmp_path / "export.json").write_bytes(content)
    monkeypatch.chdir(tmp_path)

    with pytest.raises(CommandError, match="Historical reference import failed"):
        command(system, Path("export.json"), "--confirm")

    assert not HistoricalReferenceImport.objects.exists()


def test_an_import_holds_no_lock_that_collection_or_reads_wait_for(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system = system_for_import(monkeypatch)
    writing, resume = Event(), Event()
    manager = ForecastSnapshot.objects

    def paused(name: str) -> Any:
        original = getattr(manager, name)

        def write(*args: Any, **kwargs: Any) -> Any:
            if not writing.is_set():
                writing.set()
                assert resume.wait(timeout=10)
            return original(*args, **kwargs)

        return write

    monkeypatch.setattr(manager, "create", paused("create"))
    monkeypatch.setattr(manager, "bulk_create", paused("bulk_create"))

    def importing() -> int:
        try:
            return upload(system, days(20)).pk
        finally:
            connections.close_all()

    with ThreadPoolExecutor(1) as pool:
        imported = pool.submit(importing)
        try:
            assert writing.wait(timeout=10), imported.exception(timeout=0)
            with transaction.atomic():
                Region.objects.select_for_update(nowait=True).get(pk=system.region.pk)
                ProviderAccount.objects.select_for_update(nowait=True).get(pk=system.account.pk)
        finally:
            resume.set()
        record = imported.result(timeout=30)

    assert ForecastSnapshot.objects.filter(job__reference_import_id=record).count() == 20


def test_an_interval_before_the_provider_archive_starts_is_refused() -> None:
    with pytest.raises(ValidationError, match="archive"):
        parse_historical_upload(
            document([row(period_end="2006-12-31T23:30:00+00:00")]), "history.json"
        )


def test_text_with_a_null_character_is_refused() -> None:
    with pytest.raises(ValidationError):
        parse_historical_upload(document(site_id="resource\u0000"), "history.json")


def test_power_above_the_reference_capacity_is_reported_as_a_conflict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system = system_for_import(monkeypatch)
    binding = SiteBinding.objects.get(pk=system.binding.pk)

    checked = preview_historical_upload(
        binding,
        parse_historical_upload(document([row(pv_power_rooftop=500)]), "history.json"),
    )
    plausible = preview_historical_upload(
        binding, parse_historical_upload(document(), "history.json")
    )

    assert any("above the reference capacity" in conflict for conflict in checked.conflicts)
    assert dict(checked.checks)["power_within_capacity"] is False
    assert dict(plausible.checks)["power_within_capacity"] is True
    assert dict(plausible.checks)["not_imported_before"] is True


def test_a_stored_day_keeps_only_the_identity_of_its_import(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system = system_for_import(monkeypatch)

    record = upload(system, days(2))

    for snapshot in ForecastSnapshot.objects.filter(job__reference_import=record):
        assert snapshot.raw_payload["historical_import"] == {
            "id": record.pk,
            "sha256": record.content_sha256,
        }


def test_the_import_page_lists_each_unmet_prerequisite_and_offers_no_form(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system = system_for_import(monkeypatch)
    SiteBinding.objects.filter(pk=system.binding.pk).update(
        power_basis="dc", settings_verification_reference=""
    )
    client, _ = verified_admin()

    page = client.get(import_page(system))
    refused = client.post(
        import_page(system),
        {"action": "preview", "file": SimpleUploadedFile("export.json", days(1))},
    )

    content = page.content.decode()
    assert page.status_code == 200
    assert 'type="file"' not in content
    assert "AC basis is required" in content
    assert "site settings have not been checked" in content
    assert refused.status_code == 400
    assert not HistoricalReferenceImport.objects.exists()


def test_a_preview_with_conflicts_offers_no_token(monkeypatch: pytest.MonkeyPatch) -> None:
    system = system_for_import(monkeypatch)
    client, _ = verified_admin()

    checked = preview(client, import_page(system), document(capacity_kw=2))

    assert checked["can_save"] is False
    assert checked["token"] == ""


def test_a_busy_plant_answers_a_retry_with_the_page_fragment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system = system_for_import(monkeypatch)
    client, _ = verified_admin()
    checked = preview(client, import_page(system), days(1))
    busy = OperationalError("lock timeout")
    busy.__cause__ = LockNotAvailable()

    def locked(*args: object, **kwargs: object) -> None:
        raise busy

    monkeypatch.setattr("pyracast.platform_admin.historical.import_historical_upload", locked)
    response = client.post(
        import_page(system),
        {
            "action": "save",
            "file": SimpleUploadedFile("export.json", days(1)),
            "preview_token": checked["token"],
            "settings_confirmed": "on",
        },
        headers={"Accept": "application/json"},
    )

    assert response.status_code == 503
    assert response.headers["Retry-After"] == "1"
    body = response.json()
    assert "Try again in a moment" in body["html"]
    assert body["can_save"] is False
