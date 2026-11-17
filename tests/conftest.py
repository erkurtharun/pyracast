import logging
import os
from collections.abc import Iterator

import pytest
from django.db.backends.base.base import NO_DB_ALIAS, BaseDatabaseWrapper
from django.db.backends.signals import connection_created
from hypothesis import settings

pytest.register_assert_rewrite("tests.support")

# The gate must be reproducible across runs and workers; `explore` is for local searches.
settings.register_profile("gate", derandomize=True, deadline=None, database=None)
settings.register_profile("explore", max_examples=2000, deadline=None)
settings.load_profile(os.environ.get("HYPOTHESIS_PROFILE", "gate"))


@pytest.fixture
def pyracast_logs(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> pytest.LogCaptureFixture:
    # The "pyracast" logger does not propagate, so caplog sees its records only through this.
    monkeypatch.setattr(logging.getLogger("pyracast"), "propagate", True)
    caplog.set_level(logging.INFO, logger="pyracast")
    return caplog


@pytest.fixture(name="django_db_modify_db_settings", scope="session")
def database_maintenance_timeout(django_db_modify_db_settings: None) -> Iterator[None]:
    def set_maintenance_deadline(connection: BaseDatabaseWrapper, **kwargs: object) -> None:
        if connection.vendor == "postgresql" and connection.alias == NO_DB_ALIAS:
            with connection.cursor() as cursor:
                cursor.execute("SET statement_timeout = '300s'")

    # Keep the receiver installed through pytest-django's CREATE and DROP DATABASE.
    connection_created.connect(set_maintenance_deadline, weak=False)
    try:
        yield
    finally:
        connection_created.disconnect(set_maintenance_deadline)
