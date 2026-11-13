from io import StringIO
from typing import Any

import pytest
from django.contrib.auth.models import User
from django.core.exceptions import ObjectDoesNotExist, PermissionDenied, ValidationError
from django.core.management import call_command
from django.core.management.base import CommandError

from pyracast.forecasts.management.commands import normalize_reference_capacity as command

pytestmark = pytest.mark.django_db


def test_command_routes_explicit_options(monkeypatch: pytest.MonkeyPatch) -> None:
    actor = User.objects.create_user(username="admin", is_superuser=True)
    received: list[tuple[Any, ...]] = []

    def normalize(*args: Any) -> tuple[int, ...]:
        received.append(args)
        return (11, 12)

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
    assert received == [(actor, 1, 2, 3, 4, "authorization-5")]
    assert "2 snapshots" in output.getvalue()


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
    "error", [ObjectDoesNotExist("missing"), PermissionDenied("denied"), ValidationError("invalid")]
)
def test_command_reports_application_errors(
    monkeypatch: pytest.MonkeyPatch, error: Exception
) -> None:
    User.objects.create_user(username="admin", is_superuser=True)

    def normalize(*args: Any) -> tuple[int, ...]:
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
