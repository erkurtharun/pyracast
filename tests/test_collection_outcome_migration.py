import pytest

from pyracast.forecasts.models import CollectionJob, RequestAttempt
from tests.support.domain import make_system
from tests.support.migrations import forward, migrate

pytestmark = pytest.mark.django_db(transaction=True)


def test_outcome_vocabulary_migration_keeps_every_stored_row_readable() -> None:
    make_system(approved=True)
    migrate(("forecasts", "0005_collection_times_shape_check"))
    CollectionJob.objects.update(status="failed", last_error="provider_failure")
    forward()
    job = CollectionJob.objects.get()
    assert (job.status, job.last_error) == ("failed", "provider_failure")
    assert RequestAttempt.objects.get().http_status is None
    CollectionJob.objects.update(last_error="provider_access_denied")
    RequestAttempt.objects.update(http_status=403)
    assert RequestAttempt.objects.get().http_status == 403
