import logging

import pytest

from pyracast.connections.contracts import ProviderError
from pyracast.forecasts.jobs import collect
from pyracast.forecasts.models import CollectionJob, RequestAttempt
from tests.support.domain import NOW, FakeAdapter, make_system
from tests.support.jobs import calculate


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("failed", [False, True])
def test_provider_outcome_logs_correlation_without_exception_details(
    failed: bool, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    make_system(approved=True)
    monkeypatch.setattr(logging.getLogger("pyracast"), "propagate", True)
    caplog.set_level(logging.INFO, logger="pyracast.forecasts.jobs")

    def outcome() -> None:
        if failed:
            raise ProviderError("private-provider-detail")

    assert (
        collect(CollectionJob.objects.get().pk, FakeAdapter(outcome), calculate, NOW) is not failed
    )
    attempt = RequestAttempt.objects.get(purpose="forecast")
    assert str(attempt.correlation_id) in caplog.text
    assert ("failed" if failed else "succeeded") in caplog.text
    assert "private-provider-detail" not in caplog.text
