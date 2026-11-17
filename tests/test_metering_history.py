from datetime import date

import pytest
from django.contrib.auth.models import User
from django.core.exceptions import PermissionDenied, ValidationError

from pyracast.metering.history import read_production_day
from tests.support.epias_api import archive_source, connected_source

pytestmark = pytest.mark.django_db(transaction=True)
DAY = date(2020, 1, 1)


def test_production_reader_preserves_latest_source_missingness_and_zero_priority() -> None:
    _, _, connection = connected_source()
    archive_source(connection, "realtime_generation", {8: "99", 9: "99"})
    provisional = archive_source(connection, "realtime_generation", {8: "4", 10: "5"})
    archive_source(connection, "settlement_injection", {8: "88", 9: "88"})
    settlement = archive_source(connection, "settlement_injection", {8: None, 9: None, 10: "0"})

    result = read_production_day(connection.plant, actor=connection.connected_by, day=DAY)

    assert result["source_kind"] == "mixed"
    assert result["complete"] is False
    assert result["snapshot"] == settlement
    assert result["source_snapshots"] == [settlement, provisional]
    assert result["calibration_eligible"] is True
    readings = result["readings"]
    assert isinstance(readings, list)
    assert [row["total_mwh"] for row in readings][8:11] == ["4", None, "0"]
    assert [row["snapshot_id"] for row in readings][8:11] == [
        provisional.pk,
        None,
        settlement.pk,
    ]


def test_production_reader_requires_membership_and_valid_source() -> None:
    _, _, connection = connected_source()
    archive_source(connection, "realtime_generation", {10: "5"})
    outsider = User.objects.create_user("production-reader-outsider")

    with pytest.raises(PermissionDenied):
        read_production_day(connection.plant, actor=outsider, day=DAY)
    with pytest.raises(ValidationError, match="invalid_production_source"):
        read_production_day(
            connection.plant, actor=connection.connected_by, day=DAY, source_kind="invalid"
        )
