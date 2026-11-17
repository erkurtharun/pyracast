import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError
from datetime import UTC, date
from decimal import Decimal
from pathlib import Path
from threading import Barrier, Event
from typing import cast
from urllib.parse import parse_qs

import httpx
import pytest

from pyracast.epias import AUTH_URL, BASE_URL, EpiasClient, EpiasDay, EpiasError, EpiasPlant
from tests.support.epias import PASSWORD, PLANT, TICKET, USERNAME

DAY = date(2026, 11, 9)


def payload() -> dict[str, object]:
    return cast(
        dict[str, object],
        json.loads(
            (Path(__file__).parent / "fixtures" / "epias_realtime_generation.json").read_text(
                encoding="utf-8"
            )
        ),
    )


def row(**changes: object) -> dict[str, object]:
    return {"date": "2026-11-09T00:00:00+03:00", "hour": "00:00", "total": 0, **changes}


def response_for(items: object, **page: object) -> dict[str, object]:
    return {
        "items": items,
        "page": {"number": 1, "size": 100, "total": 1, **page},
    }


def fetch(value: object) -> EpiasDay:
    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == AUTH_URL:
            return httpx.Response(201, text=TICKET)
        return httpx.Response(200, json=value)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        return EpiasClient(client, username=USERNAME, password=PASSWORD).fetch_day(PLANT, DAY)


def test_documented_shape_fixture_preserves_bounds_missingness_and_provenance() -> None:
    value = payload()
    cast(list[dict[str, object]], value["items"])[0]["ignored"] = "not archived"
    result = fetch(value)
    assert result.plant == PLANT
    assert result.day == DAY
    assert result.endpoint == f"{BASE_URL}/realtime-generation"
    assert result.collected_at.tzinfo is UTC
    assert [(reading.total, reading.sun) for reading in result.readings] == [
        (Decimal(0), Decimal(0)),
        (None, None),
        (Decimal("7.125"), Decimal("7.125")),
    ]
    assert result.readings[0].timestamp.isoformat() == "2026-11-09T00:00:00+03:00"
    assert result.readings[0].hour == "00:00"
    assert result.unit == "MWh"
    assert "ignored" not in result.raw_payload
    assert json.loads(result.raw_payload)["items"][2]["total"] == 7.125
    with pytest.raises(FrozenInstanceError):
        result.day = DAY  # type: ignore[misc]


def test_requests_use_fixed_hosts_explicit_timeouts_form_login_and_reusable_tgt() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.extensions["timeout"] == {
            "connect": 5,
            "read": 20,
            "write": 20,
            "pool": 20,
        }
        if str(request.url) == AUTH_URL:
            assert request.method == "POST"
            assert request.headers["Accept"] == "text/plain"
            assert parse_qs(request.content.decode()) == {
                "username": [USERNAME],
                "password": [PASSWORD],
            }
            return httpx.Response(201, text=TICKET)
        assert request.headers["TGT"] == TICKET
        if request.method == "GET":
            assert str(request.url) == f"{BASE_URL}/powerplant-list"
            return httpx.Response(
                200,
                json={
                    "items": [
                        {
                            "id": PLANT.id,
                            "eic": PLANT.eic,
                            "name": PLANT.name,
                            "shortName": PLANT.short_name,
                        }
                    ]
                },
            )
        assert str(request.url) == f"{BASE_URL}/realtime-generation"
        assert json.loads(request.content) == {
            "startDate": "2026-11-09T00:00:00+03:00",
            "endDate": "2026-11-09T23:00:00+03:00",
            "powerPlantId": PLANT.id,
            "page": {"number": 1, "size": 100},
        }
        return httpx.Response(200, json=payload())

    with httpx.Client(transport=httpx.MockTransport(handler), timeout=100) as client:
        adapter = EpiasClient(client, username=USERNAME, password=PASSWORD)
        assert adapter.list_plants() == (PLANT,)
        adapter.fetch_day(PLANT, DAY)
        assert USERNAME not in repr(adapter)
        assert PASSWORD not in repr(adapter)
    assert len(requests) == 3


def test_tgt_expires_and_is_reauthenticated_without_automatic_http_retries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = 0.0
    monkeypatch.setattr("pyracast.epias.time.monotonic", lambda: now)
    logins: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == AUTH_URL:
            logins.append(request.method)
            return httpx.Response(201, text=TICKET)
        return httpx.Response(200, json={"items": []})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        adapter = EpiasClient(client, username=USERNAME, password=PASSWORD)
        assert adapter.list_plants() == ()
        now = 7140
        assert adapter.list_plants() == ()
    assert logins == ["POST", "POST"]


def test_pagination_preserves_sorted_unique_readings_and_bounds_page_count() -> None:
    numbers: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == AUTH_URL:
            return httpx.Response(201, text=TICKET)
        number = json.loads(request.content)["page"]["number"]
        numbers.append(number)
        hour = 2 - number
        return httpx.Response(
            200,
            json=response_for(
                [row(date=f"2026-11-09T0{hour}:00:00+03:00", hour=f"0{hour}:00")],
                number=number,
                size=1,
                total=2,
            ),
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = EpiasClient(client, username=USERNAME, password=PASSWORD, page_size=1).fetch_day(
            PLANT, DAY
        )
    assert numbers == [1, 2]
    assert [reading.timestamp.hour for reading in result.readings] == [0, 1]


def test_empty_and_absent_energy_are_preserved() -> None:
    assert fetch(response_for([], total=0)).readings == ()
    result = fetch(response_for([row()]))
    assert result.readings[0].sun is None


@pytest.mark.parametrize(("username", "password"), [("", PASSWORD), (USERNAME, "")])
def test_credentials_are_required(username: str, password: str) -> None:
    with httpx.Client() as client, pytest.raises(EpiasError, match="credentials"):
        EpiasClient(client, username=username, password=password)


@pytest.mark.parametrize("size", [0, 101, True])
def test_page_size_is_bounded(size: int) -> None:
    with httpx.Client() as client, pytest.raises(EpiasError, match="page_size"):
        EpiasClient(client, username=USERNAME, password=PASSWORD, page_size=size)


@pytest.mark.parametrize("value", [None, [], {}, {"page": []}, response_for(None)])
def test_malformed_schema_is_rejected(value: object) -> None:
    with pytest.raises(EpiasError):
        fetch(value)


@pytest.mark.parametrize("value", [-1, True, 1.5, None])
def test_invalid_page_integers_are_rejected(value: object) -> None:
    with pytest.raises(EpiasError, match="schema"):
        fetch(response_for([row()], total=value))


@pytest.mark.parametrize(
    "changes",
    [{"number": 2}, {"size": 1}, {"total": 25}, {"total": 2}, {"total": 0}],
)
def test_inconsistent_pagination_is_rejected(changes: dict[str, object]) -> None:
    with pytest.raises(EpiasError, match="pagination"):
        fetch(response_for([row()], **changes))


def test_changing_total_between_pages_is_rejected() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == AUTH_URL:
            return httpx.Response(201, text=TICKET)
        number = json.loads(request.content)["page"]["number"]
        return httpx.Response(
            200, json=response_for([row()], number=number, size=1, total=3 - number)
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        adapter = EpiasClient(client, username=USERNAME, password=PASSWORD, page_size=1)
        with pytest.raises(EpiasError, match="pagination"):
            adapter.fetch_day(PLANT, DAY)


@pytest.mark.parametrize(
    "changes",
    [
        {"date": None},
        {"date": ""},
        {"date": "bad"},
        {"date": "2026-11-09T00:00:00"},
        {"date": "2026-11-08T23:00:00+03:00"},
        {"date": "2026-11-10T00:00:00+03:00"},
        {"date": "2026-11-09T00:30:00+03:00"},
        {"date": "2026-11-09T00:00:01+03:00"},
        {"date": "2026-11-09T00:00:00.000001+03:00"},
        {"hour": "01:00"},
        {"powerPlantId": 3155},
        {"powerPlantId": True},
        {"total": "1"},
        {"total": True},
        {"total": -1},
        {"total": float("nan")},
        {"total": float("inf")},
        {"sun": -1},
    ],
)
def test_invalid_intervals_energy_and_wrong_plants_are_rejected(changes: dict[str, object]) -> None:
    # json.dumps in httpx intentionally refuses nonfinite JSON; a raw fixture exercises it instead.
    if isinstance(changes.get("total"), float):
        encoded = json.dumps(response_for([row(**changes)]))
        with pytest.raises(EpiasError, match="energy"):
            fetch_text(encoded)
    else:
        with pytest.raises(EpiasError):
            fetch(response_for([row(**changes)]))


def test_duplicate_or_overlapping_intervals_are_rejected() -> None:
    with pytest.raises(EpiasError, match="duplicate"):
        fetch(response_for([row(), row()], total=2))
    assert fetch(response_for([row(powerPlantId=3154)])).readings[0].total == Decimal(0)


def fetch_text(text: str) -> EpiasDay:
    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == AUTH_URL:
            return httpx.Response(201, text=TICKET)
        return httpx.Response(200, text=text)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        return EpiasClient(client, username=USERNAME, password=PASSWORD).fetch_day(PLANT, DAY)


@pytest.mark.parametrize("text", ["not json", "{", '"x"'])
def test_invalid_json_is_rejected(text: str) -> None:
    with pytest.raises(EpiasError, match="schema"):
        fetch_text(text)


@pytest.mark.parametrize("secret", [USERNAME, PASSWORD, TICKET])
def test_echoed_credentials_ticket_or_nested_metadata_never_escape(secret: str) -> None:
    value = payload()
    value["unsafe"] = {"nested": [secret]}
    with pytest.raises(EpiasError, match="unsafe_epias_response") as error:
        fetch(value)
    assert secret not in str(error.value)
    assert error.value.__cause__ is None


@pytest.mark.parametrize("status", [302, 401, 403, 429, 500, 503])
@pytest.mark.parametrize("login", [True, False])
def test_http_errors_are_redacted_and_never_follow_redirects_or_retry(
    status: int, login: bool
) -> None:
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        if str(request.url) == AUTH_URL and not login:
            return httpx.Response(201, text=TICKET)
        return httpx.Response(status, text=PASSWORD, headers={"Location": "https://wrong.invalid"})

    with httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True) as client:
        adapter = EpiasClient(client, username=USERNAME, password=PASSWORD)
        with pytest.raises(EpiasError, match="request_failed") as error:
            adapter.list_plants()
    assert PASSWORD not in str(error.value)
    assert error.value.__cause__ is None
    assert len(requests) == (1 if login else 2)


def test_network_failures_are_redacted() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout(PASSWORD, request=request)

    with (
        httpx.Client(transport=httpx.MockTransport(handler)) as client,
        pytest.raises(EpiasError, match="request_failed") as error,
    ):
        EpiasClient(client, username=USERNAME, password=PASSWORD).list_plants()
    assert PASSWORD not in str(error.value)
    assert error.value.__cause__ is None


@pytest.mark.parametrize("ticket", ["", "bad", "TGT-has whitespace", "TGT-" + "x" * 4091])
def test_invalid_tickets_are_redacted(ticket: str) -> None:
    with (
        httpx.Client(
            transport=httpx.MockTransport(lambda request: httpx.Response(201, text=ticket))
        ) as client,
        pytest.raises(EpiasError, match="ticket"),
    ):
        EpiasClient(client, username=USERNAME, password=PASSWORD).list_plants()


@pytest.mark.parametrize(
    "items",
    [None, {}, [None], [{}], [{"id": 0, "eic": "e", "name": "n", "shortName": "s"}]],
)
def test_invalid_plant_list_is_rejected(items: object) -> None:
    assert_plants_error({"items": items})


@pytest.mark.parametrize("missing_eic", [{"eic": None}, {}])
def test_plant_list_preserves_missing_eic_without_hiding_identifiable_plants(
    missing_eic: dict[str, object],
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == AUTH_URL:
            return httpx.Response(201, text=TICKET)
        return httpx.Response(
            200,
            json={
                "items": [
                    {"id": 2213, "name": "ABATEKS TEKSTIL", "shortName": "ABATEKS", **missing_eic},
                    {
                        "id": PLANT.id,
                        "eic": PLANT.eic,
                        "name": PLANT.name,
                        "shortName": PLANT.short_name,
                    },
                ]
            },
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        plants = EpiasClient(client, username=USERNAME, password=PASSWORD).list_plants()
    assert plants == (EpiasPlant(2213, None, "ABATEKS TEKSTIL", "ABATEKS"), PLANT)


@pytest.mark.parametrize("eic", ["", " ", 123, True])
def test_nonmissing_invalid_eic_remains_rejected(eic: object) -> None:
    assert_plants_error({"items": [{"id": 2213, "eic": eic, "name": "n", "shortName": "s"}]})


def assert_plants_error(value: object) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == AUTH_URL:
            return httpx.Response(201, text=TICKET)
        return httpx.Response(200, json=value)

    with (
        httpx.Client(transport=httpx.MockTransport(handler)) as client,
        pytest.raises(EpiasError),
    ):
        EpiasClient(client, username=USERNAME, password=PASSWORD).list_plants()


def test_duplicate_and_excessive_plant_lists_are_rejected() -> None:
    plant = {"id": 1, "eic": "e", "name": "n", "shortName": "s"}
    assert_plants_error({"items": [plant, plant]})
    assert_plants_error({"items": [plant] * 100_001})


@pytest.mark.parametrize("id", [0, -1, True])
def test_invalid_requested_plant_rejects_before_http(id: int) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        pytest.fail("Invalid plants must not issue HTTP requests")

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        adapter = EpiasClient(client, username=USERNAME, password=PASSWORD)
        with pytest.raises(EpiasError):
            adapter.fetch_day(EpiasPlant(id, "e", "n", "s"), DAY)


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        ("60", 60),
        ("999999999", 999999999),
        ("bad", None),
        ("-1", None),
        ("0" * 129, None),
        ("Mon, 09 Nov 2026 00:00:00 GMT", 0),
        ("Mon, 09 Nov 2026 00:00:00", None),
    ],
)
def test_retry_after_is_redacted_parsed_and_bounded(header: str, expected: int | None) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, text=PASSWORD, headers={"Retry-After": header})

    with (
        httpx.Client(transport=httpx.MockTransport(handler)) as client,
        pytest.raises(EpiasError) as error,
    ):
        EpiasClient(client, username=USERNAME, password=PASSWORD).list_plants()
    assert error.value.retry_after_seconds == expected
    assert PASSWORD not in str(error.value)


def test_json_escaped_credentials_and_secret_keys_are_rejected() -> None:
    escaped = "".join(f"\\u{ord(character):04x}" for character in PASSWORD)
    with pytest.raises(EpiasError, match="unsafe"):
        fetch_text('{"secret": "' + escaped + '"}')
    with pytest.raises(EpiasError, match="unsafe"):
        fetch({PASSWORD: "echoed secret key"})


def test_parallel_requests_share_one_tgt_refresh() -> None:
    barrier = Barrier(2)
    login_entered = Event()
    release_login = Event()
    logins: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == AUTH_URL:
            logins.append(request.method)
            login_entered.set()
            assert release_login.wait(5)
            return httpx.Response(201, text=TICKET)
        return httpx.Response(200, json={"items": []})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        adapter = EpiasClient(client, username=USERNAME, password=PASSWORD)

        def run() -> tuple[EpiasPlant, ...]:
            barrier.wait(timeout=5)
            return adapter.list_plants()

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(run) for _ in range(2)]
            assert login_entered.wait(5)
            release_login.set()
            assert [future.result(timeout=5) for future in futures] == [(), ()]
    assert logins == ["POST"]
