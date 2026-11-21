from collections.abc import Callable, Iterator

import httpx
import pytest
from django.conf import LazySettings

from pyracast.epias import AUTH_URL, EpiasClient, EpiasError
from pyracast.metering import client
from pyracast.metering.jobs import claim_epias_job, pending_epias_ids, run_epias_job
from pyracast.metering.models import EpiasCollectionJob
from tests.support.domain import NOW
from tests.support.epias import PASSWORD, PLANT, TICKET, USERNAME
from tests.support.metering import connected

CREDENTIAL = "fixture-value"

Handler = Callable[[httpx.Request], httpx.Response]


@pytest.fixture
def no_cached_client() -> Iterator[None]:
    client._cached_client.cache_clear()
    yield
    client._cached_client.cache_clear()


def provider(handler: Handler, requests: list[str]) -> EpiasClient:
    def record(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        return handler(request)

    return EpiasClient(
        httpx.Client(transport=httpx.MockTransport(record)), username=USERNAME, password=PASSWORD
    )


@pytest.mark.parametrize("status", [401, 403])
def test_a_rejected_sign_in_stops_further_sign_ins_of_the_process(status: int) -> None:
    requests: list[str] = []
    rejected = provider(lambda _: httpx.Response(status), requests)

    for _ in range(2):
        with pytest.raises(EpiasError, match="epias_credentials_rejected") as error:
            rejected.fetch_day(PLANT, NOW.date())
        assert error.value.retryable is False

    assert requests == [AUTH_URL]


@pytest.mark.parametrize("status", [429, 500, 503])
def test_an_unavailable_sign_in_service_is_an_outage_tried_again(status: int) -> None:
    requests: list[str] = []
    unavailable = provider(lambda _: httpx.Response(status), requests)

    for _ in range(2):
        with pytest.raises(EpiasError, match="epias_request_failed") as error:
            unavailable.list_plants()
        assert error.value.retryable is True

    assert requests == [AUTH_URL, AUTH_URL]


@pytest.mark.django_db(transaction=True)
@pytest.mark.usefixtures("no_cached_client")
def test_missing_credentials_keep_the_attempt_and_name_their_cause(
    monkeypatch: pytest.MonkeyPatch, settings: LazySettings
) -> None:
    connected(monkeypatch)
    settings.PYRACAST_EPIAS_USERNAME = ""
    settings.PYRACAST_EPIAS_PASSWORD = ""
    job = EpiasCollectionJob.objects.order_by("pk").first()
    assert job is not None

    assert not run_epias_job(job.pk, now=NOW)

    job.refresh_from_db()
    assert (job.status, job.attempts, job.last_error) == (
        "pending",
        0,
        "epias_credentials_missing",
    )
    assert job.available_at > NOW
    assert job.lease_token is None


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize(
    ("status", "outcome"),
    [
        (401, ("pending", 0, "epias_credentials_rejected")),
        (503, ("pending", 1, "epias_provider_failure")),
    ],
)
def test_a_job_tells_rejected_credentials_from_a_provider_outage(
    monkeypatch: pytest.MonkeyPatch, status: int, outcome: tuple[str, int, str]
) -> None:
    connected(monkeypatch)
    requests: list[str] = []
    answer = provider(lambda _: httpx.Response(status), requests)
    monkeypatch.setattr("pyracast.metering.jobs.get_epias_client", lambda: answer)
    job = EpiasCollectionJob.objects.order_by("pk").first()
    assert job is not None

    assert not run_epias_job(job.pk, now=NOW)

    job.refresh_from_db()
    assert (job.status, job.attempts, job.last_error) == outcome
    assert job.available_at > NOW


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("expire", ["publication", "claim"])
@pytest.mark.parametrize("error", ["epias_credentials_missing", "epias_credentials_rejected"])
def test_a_night_that_ends_without_usable_credentials_keeps_their_cause(
    monkeypatch: pytest.MonkeyPatch, expire: str, error: str
) -> None:
    connected(monkeypatch)
    job = EpiasCollectionJob.objects.order_by("pk").first()
    assert job is not None
    EpiasCollectionJob.objects.filter(pk=job.pk).update(
        retry_until=NOW, available_at=NOW, last_error=error
    )
    other = EpiasCollectionJob.objects.exclude(pk=job.pk).order_by("pk").first()
    assert other is not None
    EpiasCollectionJob.objects.filter(pk=other.pk).update(retry_until=NOW, available_at=NOW)

    if expire == "publication":
        assert job.pk not in pending_epias_ids(NOW)
    else:
        assert claim_epias_job(job.pk, NOW) is None

    job.refresh_from_db()
    assert (job.status, job.attempts, job.last_error) == ("failed", 0, error)
    if expire == "publication":
        other.refresh_from_db()
        assert (other.status, other.last_error) == ("failed", "retry_window_expired")


@pytest.mark.usefixtures("no_cached_client")
def test_the_process_client_names_missing_credentials(settings: LazySettings) -> None:
    settings.PYRACAST_EPIAS_USERNAME = ""
    settings.PYRACAST_EPIAS_PASSWORD = CREDENTIAL

    with pytest.raises(EpiasError, match="epias_credentials_missing"):
        client.get_epias_client()


def test_a_signed_in_client_keeps_its_ticket_across_data_requests() -> None:
    requests: list[str] = []

    def answer(request: httpx.Request) -> httpx.Response:
        if str(request.url) == AUTH_URL:
            return httpx.Response(201, text=TICKET)
        return httpx.Response(200, json={"items": []})

    signed_in = provider(answer, requests)
    signed_in.list_plants()
    signed_in.list_settlement_plants()

    assert requests.count(AUTH_URL) == 1
