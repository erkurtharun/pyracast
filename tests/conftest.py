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


TOOLING = frozenset(
    {
        "test_agent_graph",
        "test_api_schema_generation",
        "test_ci_images",
        "test_deployment_backup",
        "test_deployment_backup_lock",
        "test_deployment_helpers",
        "test_deployment_host",
        "test_deployment_reset",
        "test_deployment_secrets",
        "test_deployment_watchdog",
        "test_dev_dependencies",
        "test_dev_launcher",
        "test_er_diagram",
        "test_hooks",
        "test_postgres_workspace",
        "test_pytest_shard",
        "test_quality_gate",
        "test_release_images",
        "test_release_scope",
        "test_solcast_capture",
        "test_start_postgres",
        "test_start_rabbitmq",
        "test_suite_conventions",
        "test_support_images",
        "test_translations",
    }
)
SUBPROCESS = frozenset(
    {
        "test_agent_graph",
        "test_api_schema_generation",
        "test_browser",
        "test_ci_images",
        "test_deployment_helpers",
        "test_deployment_reset",
        "test_dev_dependencies",
        "test_dev_launcher",
        "test_environment",
        "test_er_diagram",
        "test_hooks",
        "test_irradiance_calibration",
        "test_package",
        "test_postgres_workspace",
        "test_pytest_shard",
        "test_release_images",
        "test_release_scope",
        "test_shutdown_process",
        "test_test_settings",
    }
)
MIGRATION = frozenset[str]()
SELECTION_SETS = {
    "tooling": TOOLING,
    "subprocess": SUBPROCESS,
    "migration": MIGRATION,
    "browser": frozenset({"test_browser"}),
    "live": frozenset({"test_epias_live", "test_live_queue"}),
}
MODULE_MARKERS = {
    module: frozenset(marker for marker, modules in SELECTION_SETS.items() if module in modules)
    for module in set[str]().union(*SELECTION_SETS.values())
}


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    for item in items:
        for marker in MODULE_MARKERS.get(item.path.stem, ()):
            item.add_marker(marker)


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
