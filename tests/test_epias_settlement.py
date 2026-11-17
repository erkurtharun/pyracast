import json
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import cast

import httpx
import pytest

from pyracast.epias import AUTH_URL, BASE_URL, EpiasClient, EpiasError, EpiasPlant
from tests.support.epias import PASSWORD, PLANT, TICKET, USERNAME

SETTLEMENT = EpiasPlant(5011621, PLANT.eic, PLANT.name, PLANT.short_name)
DAY = date(2026, 9, 1)


def settlement_payload() -> dict[str, object]:
    return cast(
        dict[str, object],
        json.loads(
            (Path(__file__).parent / "fixtures" / "epias_settlement_injection.json").read_text()
        ),
    )


def test_captured_settlement_resolves_eic_in_its_own_namespace_and_preserves_hour_start() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if str(request.url) == AUTH_URL:
            return httpx.Response(201, text=TICKET)
        if request.method == "GET":
            assert str(request.url) == f"{BASE_URL}/injection-quantity-powerplant-list"
            return httpx.Response(
                200,
                json={
                    "items": [
                        {
                            "id": SETTLEMENT.id,
                            "eic": SETTLEMENT.eic,
                            "name": SETTLEMENT.name,
                            "shortName": SETTLEMENT.short_name,
                        }
                    ]
                },
            )
        assert str(request.url) == f"{BASE_URL}/injection-quantity"
        assert json.loads(request.content) == {
            "startDate": "2026-09-01T00:00:00+03:00",
            "endDate": "2026-09-01T23:00:00+03:00",
            "powerplantId": SETTLEMENT.id,
            "page": {"number": 1, "size": 100},
        }
        return httpx.Response(200, json=settlement_payload())

    with httpx.Client(transport=httpx.MockTransport(handler)) as transport:
        client = EpiasClient(transport, username=USERNAME, password=PASSWORD)
        assert client.resolve_settlement_plant(cast(str, PLANT.eic)) == SETTLEMENT
        result = client.fetch_settlement(SETTLEMENT, DAY, DAY)
    assert len(requests) == 3
    assert result.source_kind == "settlement_injection"
    assert result.end_day == DAY
    assert result.endpoint == f"{BASE_URL}/injection-quantity"
    assert len(result.readings) == 24
    assert sum(row.total or Decimal(0) for row in result.readings) == Decimal("975.57")
    for reading in result.readings:
        assert reading.period_end - reading.period_start == timedelta(hours=1)
        assert reading.period_start.utcoffset() == timedelta(hours=3)
    assert result.readings[10].period_start.isoformat() == "2026-09-01T10:00:00+03:00"
    assert result.readings[10].period_end.isoformat() == "2026-09-01T11:00:00+03:00"
    assert json.loads(result.raw_payload)["items"][10]["hour"] == 10


@pytest.mark.parametrize("mode", ["missing", "ambiguous"])
def test_settlement_resolution_never_guesses_eic(mode: str) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == AUTH_URL:
            return httpx.Response(201, text=TICKET)
        rows = [{"id": 1, "eic": "different", "name": "n", "shortName": "s"}]
        if mode == "ambiguous":
            rows = [{"id": i, "eic": PLANT.eic, "name": "n", "shortName": "s"} for i in (1, 2)]
        return httpx.Response(200, json={"items": rows})

    with httpx.Client(transport=httpx.MockTransport(handler)) as transport:
        client = EpiasClient(transport, username=USERNAME, password=PASSWORD)
        if mode == "ambiguous":
            with pytest.raises(EpiasError, match="ambiguous_epias_eic"):
                client.resolve_settlement_plant(cast(str, PLANT.eic))
        else:
            assert client.resolve_settlement_plant(cast(str, PLANT.eic)) is None


@pytest.mark.parametrize(
    "changes",
    [
        {"hour": "0"},
        {"hour": True},
        {"hour": 24},
        {"powerplantId": 3154},
        {"total": -1},
        {"sun": -1},
    ],
)
def test_settlement_rejects_invalid_hour_identity_and_energy(changes: dict[str, object]) -> None:
    value = {
        "items": [{"date": "2026-09-01T00:00:00+03:00", "hour": 0, "total": 0, **changes}],
        "page": {"number": 1, "size": 100, "total": 1},
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return (
            httpx.Response(201, text=TICKET)
            if str(request.url) == AUTH_URL
            else httpx.Response(200, json=value)
        )

    with (
        httpx.Client(transport=httpx.MockTransport(handler)) as transport,
        pytest.raises(EpiasError),
    ):
        EpiasClient(transport, username=USERNAME, password=PASSWORD).fetch_settlement(
            SETTLEMENT, DAY, DAY
        )


def test_month_pages_preserve_missing_values_and_zero_without_inventing_rows() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == AUTH_URL:
            return httpx.Response(201, text=TICKET)
        page = json.loads(request.content)["page"]["number"]
        rows = [
            {"date": "2026-09-01T00:00:00+03:00", "hour": 0, "total": 0},
            {"date": "2026-09-02T10:00:00+03:00", "hour": 10, "total": None},
            {"date": "2026-09-30T23:00:00+03:00", "hour": 23},
        ]
        return httpx.Response(
            200, json={"items": [rows[page - 1]], "page": {"number": page, "size": 1, "total": 3}}
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as transport:
        client = EpiasClient(transport, username=USERNAME, password=PASSWORD, page_size=1)
        result = client.fetch_settlement(SETTLEMENT, DAY, date(2026, 9, 30))
        assert [r.total for r in result.readings] == [Decimal(0), None, None]
        for end in (date(2026, 8, 31), date(2026, 10, 2)):
            with pytest.raises(EpiasError, match="invalid_epias_period"):
                client.fetch_settlement(SETTLEMENT, DAY, end)


def test_full_31_day_settlement_month_is_bounded_to_eight_pages() -> None:
    pages: list[int] = []
    fixture_rows = cast(list[dict[str, object]], settlement_payload()["items"])
    rows = [
        {**row, "date": f"2026-10-{day:02}T{hour:02}:00:00+03:00"}
        for day in range(1, 32)
        for hour, row in enumerate(fixture_rows)
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == AUTH_URL:
            return httpx.Response(201, text=TICKET)
        body = json.loads(request.content)
        assert body["startDate"] == "2026-10-01T00:00:00+03:00"
        assert body["endDate"] == "2026-10-31T23:00:00+03:00"
        number = body["page"]["number"]
        pages.append(number)
        return httpx.Response(
            200,
            json={
                "items": rows[(number - 1) * 100 : number * 100],
                "page": {"number": number, "size": 100, "total": 744},
            },
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as transport:
        result = EpiasClient(transport, username=USERNAME, password=PASSWORD).fetch_settlement(
            SETTLEMENT,
            date(2026, 10, 1),
            date(2026, 10, 31),
        )
    assert pages == list(range(1, 9))
    assert len(result.readings) == 744
    assert sum(r.total or Decimal(0) for r in result.readings) == Decimal("30242.67")


def test_captured_unpublished_settlement_accepts_empty_first_page_with_explicit_null_total() -> (
    None
):
    payload = json.loads(
        (Path(__file__).parent / "fixtures" / "epias_settlement_unpublished.json").read_text()
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == AUTH_URL:
            return httpx.Response(201, text=TICKET)
        return httpx.Response(200, json=payload)

    with httpx.Client(transport=httpx.MockTransport(handler)) as transport:
        result = EpiasClient(transport, username=USERNAME, password=PASSWORD).fetch_settlement(
            SETTLEMENT,
            date(2026, 10, 1),
            date(2026, 10, 31),
        )
    assert result.readings == ()
    assert result.source_kind == "settlement_injection"
    assert json.loads(result.raw_payload)["items"] == []


@pytest.mark.parametrize(
    "invalid", ["realtime", "nonempty", "number", "size", "missing_total", "later_page"]
)
def test_unknown_pagination_totals_are_not_silently_zeroed(invalid: str) -> None:
    page_size = 1 if invalid == "later_page" else 100

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == AUTH_URL:
            return httpx.Response(201, text=TICKET)
        number = json.loads(request.content)["page"]["number"]
        page: dict[str, object] = {"number": number, "size": page_size, "total": None}
        items: list[dict[str, object]] = []
        if invalid == "later_page" and number == 1:
            page["total"] = 2
        if invalid == "nonempty" or (invalid == "later_page" and number == 1):
            items = [{"date": "2026-09-01T00:00:00+03:00", "hour": 0, "total": 0}]
        if invalid == "number":
            page["number"] = 2
        if invalid == "size":
            page["size"] = 99
        if invalid == "missing_total":
            del page["total"]
        return httpx.Response(200, json={"items": items, "page": page})

    with httpx.Client(transport=httpx.MockTransport(handler)) as transport:
        client = EpiasClient(transport, username=USERNAME, password=PASSWORD, page_size=page_size)
        if invalid == "realtime":
            with pytest.raises(EpiasError):
                client.fetch_day(PLANT, DAY)
        else:
            with pytest.raises(EpiasError):
                client.fetch_settlement(SETTLEMENT, DAY, DAY)
