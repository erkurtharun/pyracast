from io import StringIO
from typing import Any

import pytest
from django.contrib.auth.models import User
from django.core.exceptions import ObjectDoesNotExist, PermissionDenied, ValidationError
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import IntegrityError, OperationalError

from pyracast.forecasts.capacity_normalization import NormalizationOutcome
from pyracast.forecasts.management.commands import normalize_reference_capacity as command

pytestmark = pytest.mark.django_db


def test_command_routes_explicit_options(monkeypatch: pytest.MonkeyPatch) -> None:
    actor = User.objects.create_user(username="admin", is_superuser=True)
    received: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    def normalize(*args: Any, **options: Any) -> NormalizationOutcome:
        received.append((args, options))
        return NormalizationOutcome(
            3, 4, 1, 1000, 1000, (11, 12), (), {"forecasts": 2}, None, None, ()
        )

    monkeypatch.setattr(command, "normalize_reference_capacity", normalize)
    output = StringIO()
    call_command(
        "normalize_reference_capacity",
        "--username",
        "admin",
        "--tenant-id",
        "1",
        "--region-id",
        "2",
        "--source-binding-id",
        "3",
        "--target-binding-id",
        "4",
        "--reference",
        "authorization-5",
        stdout=output,
    )
    assert received == [((actor, 1, 2, 3, 4, "authorization-5"), {"dry_run": False})]
    assert "Created 2 snapshots (forecasts 2); reused 0." in output.getvalue()


@pytest.mark.parametrize("username", ["unknown", "member", "inactive"])
def test_command_requires_active_superuser(username: str) -> None:
    User.objects.create_user(username="member")
    User.objects.create_user(username="inactive", is_superuser=True, is_active=False)
    with pytest.raises(CommandError, match="Reference capacity normalization failed"):
        call_command(
            "normalize_reference_capacity",
            username=username,
            tenant_id=1,
            region_id=2,
            source_binding_id=3,
            target_binding_id=4,
            reference="authorization-5",
        )


@pytest.mark.parametrize(
    "error",
    [
        ObjectDoesNotExist("missing"),
        PermissionDenied("denied"),
        ValidationError("invalid"),
        IntegrityError("conflict"),
    ],
)
def test_command_reports_application_errors(
    monkeypatch: pytest.MonkeyPatch, error: Exception
) -> None:
    User.objects.create_user(username="admin", is_superuser=True)

    def normalize(*args: Any, **options: Any) -> NormalizationOutcome:
        raise error

    monkeypatch.setattr(command, "normalize_reference_capacity", normalize)
    with pytest.raises(CommandError, match="Reference capacity normalization failed"):
        call_command(
            "normalize_reference_capacity",
            username="admin",
            tenant_id=1,
            region_id=2,
            source_binding_id=3,
            target_binding_id=4,
            reference="authorization-5",
        )


def test_a_database_failure_other_than_a_busy_region_is_not_hidden(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    User.objects.create_user(username="admin", is_superuser=True)

    def normalize(*args: Any, **options: Any) -> NormalizationOutcome:
        raise OperationalError("connection lost")

    monkeypatch.setattr(command, "normalize_reference_capacity", normalize)
    with pytest.raises(OperationalError):
        call_command(
            "normalize_reference_capacity",
            username="admin",
            tenant_id=1,
            region_id=2,
            source_binding_id=3,
            reference="authorization-5",
        )


def test_a_run_with_nothing_to_copy_reports_the_reused_copies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    User.objects.create_user(username="admin", is_superuser=True)
    monkeypatch.setattr(
        command,
        "normalize_reference_capacity",
        lambda *args, **options: NormalizationOutcome(
            3, 4, 2, 4, 2, (), (7, 8), {}, None, None, ()
        ),
    )
    output = StringIO()

    call_command(
        "normalize_reference_capacity",
        username="admin",
        tenant_id=1,
        region_id=2,
        source_binding_id=3,
        reference="authorization-5",
        stdout=output,
    )

    assert output.getvalue().splitlines()[1] == "Created 0 snapshots; reused 2."
