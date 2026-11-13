from collections.abc import Iterator

import pytest
from django.db.backends.base.base import NO_DB_ALIAS, BaseDatabaseWrapper
from django.db.backends.signals import connection_created


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
