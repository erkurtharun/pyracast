import json
import time
from collections.abc import Iterator
from datetime import date, datetime, timedelta

import httpx
import pytest

from pyracast.background.deadlines import HTTP_READ_SECONDS
from pyracast.epias import AUTH_URL, TIMEZONE, EpiasClient, EpiasDay, EpiasError, EpiasPlant
from pyracast.metering.jobs import run_epias_job
from pyracast.metering.models import EpiasCollectionJob, EpiasSnapshot
from tests.support.domain import NOW
from tests.support.epias import PASSWORD, TICKET, USERNAME
from tests.support.metering_settlement import SETTLEMENT, SettlementClient, link_settlement

# The ladder lets a request start until 100 s after the job began (time limit 210 s, minus one
# 50 s provider request and one 60 s statement for the commit).
LAST_REQUEST_START_SECONDS = 100
MONTH_START, MONTH_END = date(2026, 10, 1), date(2026, 10, 31)


def month_rows() -> list[dict[str, object]]:
    start = datetime.combine(MONTH_START, datetime.min.time(), TIMEZONE)
    return [
        {"date": (start + timedelta(hours=hour)).isoformat(), "hour": hour % 24, "total": 1.5}
        for hour in range(31 * 24)
    ]


def test_a_month_stops_between_pages_once_its_deadline_has_passed() -> None:
    rows = month_rows()
    pages: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == AUTH_URL:
            return httpx.Response(201, text=TICKET)
        number = json.loads(request.content)["page"]["number"]
        pages.append(number)
        time.sleep(0.2)
        return httpx.Response(
            200,
            json={
                "items": rows[(number - 1) * 100 : number * 100],
                "page": {"number": number, "size": 100, "total": len(rows)},
            },
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as transport:
        client = EpiasClient(transport, username=USERNAME, password=PASSWORD)
        with pytest.raises(EpiasError, match="epias_deadline_exceeded") as error:
            client.fetch_settlement(
                SETTLEMENT, MONTH_START, MONTH_END, deadline=time.monotonic() + 0.1
            )
        assert error.value.retryable is True
        assert pages == [1]
        complete = client.fetch_settlement(
            SETTLEMENT, MONTH_START, MONTH_END, deadline=time.monotonic() + 60
        )
    assert len(complete.readings) == len(rows)
    assert pages == [1, *range(1, 9)]


def test_a_sign_in_that_ends_past_the_deadline_sends_no_data_request() -> None:
    pages: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == AUTH_URL:
            time.sleep(0.2)
            return httpx.Response(201, text=TICKET)
        pages.append(json.loads(request.content)["page"]["number"])
        return httpx.Response(200, json={"items": [], "page": {"number": 1, "size": 100}})

    with httpx.Client(transport=httpx.MockTransport(handler)) as transport:
        client = EpiasClient(transport, username=USERNAME, password=PASSWORD)
        with pytest.raises(EpiasError, match="epias_deadline_exceeded"):
            client.fetch_settlement(
                SETTLEMENT, MONTH_START, MONTH_END, deadline=time.monotonic() + 0.1
            )

    assert pages == []


class Dripping(httpx.SyncByteStream):
    def __iter__(self) -> Iterator[bytes]:
        yield b'{"items": [], "pad": "'
        for _ in range(10):
            yield b"x"
        yield b'"}'


def test_a_dripping_answer_ends_at_the_body_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    answers = iter(
        [httpx.Response(200, json={"items": []}), httpx.Response(200, stream=Dripping())]
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == AUTH_URL:
            return httpx.Response(201, text=TICKET)
        return next(answers)

    ticks = iter(range(0, 10_000, HTTP_READ_SECONDS))
    with httpx.Client(transport=httpx.MockTransport(handler)) as transport:
        client = EpiasClient(transport, username=USERNAME, password=PASSWORD)
        assert client.list_plants() == ()
        monkeypatch.setattr("time.monotonic", lambda: next(ticks))
        with pytest.raises(EpiasError, match="epias_request_failed") as error:
            client.list_plants()

    assert error.value.retryable is True


def test_an_oversized_answer_is_refused_without_a_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == AUTH_URL:
            return httpx.Response(201, text=TICKET)
        return httpx.Response(200, json={"items": [], "pad": "x" * 64})

    with httpx.Client(transport=httpx.MockTransport(handler)) as transport:
        client = EpiasClient(transport, username=USERNAME, password=PASSWORD)
        assert client.list_plants() == ()
        monkeypatch.setattr("pyracast.epias.MAX_RESPONSE_BYTES", 32)
        with pytest.raises(EpiasError, match="epias_response_too_large") as error:
            client.list_plants()

    assert error.value.retryable is False


@pytest.mark.django_db(transaction=True)
def test_a_job_lets_requests_start_only_inside_its_time_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    link_settlement(monkeypatch)
    job = EpiasCollectionJob.objects.filter(source_kind="settlement_injection").first()
    assert job is not None
    deadlines: list[float | None] = []

    class Interrupted(SettlementClient):
        def fetch_settlement(
            self, plant: EpiasPlant, start: date, end: date, *, deadline: float | None = None
        ) -> EpiasDay:
            deadlines.append(deadline)
            raise EpiasError("epias_deadline_exceeded")

    monkeypatch.setattr("pyracast.metering.jobs.get_epias_client", lambda: Interrupted())
    before = time.monotonic()
    assert not run_epias_job(job.pk, now=NOW)
    after = time.monotonic()

    [deadline] = deadlines
    assert deadline is not None
    assert before + LAST_REQUEST_START_SECONDS <= deadline <= after + LAST_REQUEST_START_SECONDS
    job.refresh_from_db()
    assert (job.status, job.attempts) == ("pending", 1)
    assert not EpiasSnapshot.objects.filter(job=job).exists()
