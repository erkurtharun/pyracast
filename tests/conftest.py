import logging
import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from django.conf import settings as django_settings
from django.core.cache import caches
from django.db.backends.base.base import NO_DB_ALIAS, BaseDatabaseWrapper
from django.db.backends.signals import connection_created
from django.utils.log import configure_logging
from hypothesis import settings

pytest.register_assert_rewrite("tests.support")

# The gate must be reproducible across runs and workers; `explore` is for local searches.
settings.register_profile("gate", derandomize=True, deadline=None, database=None)
settings.register_profile("explore", max_examples=2000, deadline=None)
settings.load_profile(os.environ.get("HYPOTHESIS_PROFILE", "gate"))


E2E = Path(__file__).resolve().parent / "e2e"
TOOLING = frozenset(
    {
        "test_agent_graph",
        "test_api_schema_generation",
        "test_boot_script",
        "test_calibration_cli",
        "test_ci_artifacts",
        "test_ci_images",
        "test_ci_result",
        "test_container_log_lines",
        "test_deadline_ladder",
        "test_deployment_backup",
        "test_deployment_backup_lock",
        "test_deployment_bootstrap",
        "test_deployment_checks",
        "test_deployment_entry_points",
        "test_deployment_helpers",
        "test_deployment_host",
        "test_deployment_previous_helper",
        "test_deployment_rehearsal",
        "test_deployment_release_state",
        "test_deployment_reset",
        "test_deployment_secrets",
        "test_deployment_watchdog",
        "test_dev_dependencies",
        "test_dev_launcher",
        "test_edge_gate",
        "test_er_diagram",
        "test_gmail_authorization",
        "test_hooks",
        "test_host_contract",
        "test_host_secrets",
        "test_image_scans",
        "test_infrastructure_settings",
        "test_npm_audit",
        "test_operations_contracts",
        "test_operations_events",
        "test_postgres_workspace",
        "test_probe_origin",
        "test_probe_origin_pull",
        "test_pytest_shard",
        "test_quality_gate",
        "test_release_authorization",
        "test_release_environment",
        "test_release_images",
        "test_release_scope",
        "test_release_unit",
        "test_release_workflow",
        "test_shutdown_process",
        "test_solcast_capture",
        "test_start_postgres",
        "test_start_rabbitmq",
        "test_suite_conventions",
        "test_tracker_geometry_evaluation",
        "test_translations",
        "test_version_pins",
    }
)
SUBPROCESS = frozenset(
    {
        "test_agent_graph",
        "test_api_schema_generation",
        "test_ci_images",
        "test_container_log_lines",
        "test_deployment_helpers",
        "test_deployment_previous_helper",
        "test_deployment_reset",
        "test_dev_dependencies",
        "test_dev_launcher",
        "test_environment",
        "test_er_diagram",
        "test_hooks",
        "test_irradiance_calibration",
        "test_operations_contracts",
        "test_package",
        "test_postgres_workspace",
        "test_pytest_shard",
        "test_release_authorization",
        "test_release_images",
        "test_release_scope",
        "test_shutdown_process",
        "test_solcast_capture",
        "test_test_settings",
    }
)
MIGRATION = frozenset(
    {
        "test_calibration_requests",
        "test_calibration_wait_migration",
        "test_collection_outcome_migration",
        "test_collection_times_migration",
        "test_delivery_metadata_migration",
        "test_epias_revision_migration",
        "test_invariant_migrations",
        "test_invitation_lifecycle_migration",
        "test_invitation_link_migration",
        "test_invitation_role_migration",
        "test_ledger_invitation_and_recipient_migrations",
        "test_location_request_migration",
        "test_plant_parameters_migration",
        "test_provider_key_fingerprint_migration",
        "test_purge_warning_migration",
        "test_recipient_role_migration",
        "test_reminder_recipient_migration",
        "test_schema_naming_migrations",
        "test_submission_change_time_migration",
        "test_vocabulary_migrations",
        "test_weather_removal_migration",
        "test_workspace_name_migration",
    }
)
# Modules that drive every writer of the tables with stored invariants. After each passing test
# of these modules the rows are checked with raw queries written independently of the CHECK and
# trigger definitions.
INVARIANT_AUDIT = frozenset(
    {
        "test_account_mail_outbox",
        "test_account_rotation",
        "test_admin_health_queries",
        "test_admin_health_views",
        "test_admin_plant_console",
        "test_admin_setup",
        "test_admin_workspace_management",
        "test_archive_invariants",
        "test_audit_details",
        "test_calibration_automation",
        "test_calibration_invariants",
        "test_calibration_preparation",
        "test_calibration_runtime",
        "test_capacity_normalization",
        "test_capacity_normalization_boundaries",
        "test_change_request_boundaries",
        "test_change_request_concurrency",
        "test_change_request_constraints",
        "test_change_requests",
        "test_collection_calendar_windows",
        "test_collection_resilience",
        "test_collection_review_regressions",
        "test_collection_schedule",
        "test_company_registration",
        "test_epias_api",
        "test_historical_reference_import",
        "test_historical_upload",
        "test_incident_calibration",
        "test_invitation_acceptance",
        "test_invitation_lifecycle",
        "test_invitation_limits",
        "test_job_lease_invariants",
        "test_jobs",
        "test_location_correction",
        "test_mail_outcomes",
        "test_metering",
        "test_metering_nightly",
        "test_metering_settlement",
        "test_nightly_solcast",
        "test_notification_group_boundaries",
        "test_notification_groups",
        "test_notification_preparation",
        "test_notification_recipient_history",
        "test_notification_revocations",
        "test_notification_transmission",
        "test_notifications",
        "test_outbound_mail",
        "test_plant_operations",
        "test_plant_training",
        "test_platform_workspace_authority",
        "test_production_changes",
        "test_provider_account_invariants",
        "test_provider_identity",
        "test_provider_recovery",
        "test_provider_retry",
        "test_provider_snapshot_restore",
        "test_region_editing",
        "test_reminder_boundary",
        "test_reminder_recipient_shape",
        "test_revalidation_schedule",
        "test_secret_reencryption",
        "test_shared_reminder_schedule",
        "test_social_accounts",
        "test_transactional_mail",
        "test_trash_operations",
        "test_workspace_admin_invitations",
        "test_workspace_http_failures",
        "test_workspace_ledger_constraints",
        "test_workspace_member_removal",
        "test_workspaces",
    }
)
SELECTION_SETS = {
    "tooling": TOOLING,
    "subprocess": SUBPROCESS,
    "migration": MIGRATION,
    "browser": frozenset(path.stem for path in E2E.glob("test_*.py")),
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


@pytest.hookimpl(wrapper=True)
def pytest_runtest_call(item: pytest.Item) -> Iterator[None]:
    # Runs after the test body and before fixture teardown flushes its database.
    yield
    if item.path.stem in INVARIANT_AUDIT and item.get_closest_marker("django_db"):
        # Imported here: the support module loads models, which needs Django set up.
        from tests.support.invariants import stored_violations

        assert stored_violations() == {}


@pytest.fixture(autouse=True)
def fresh_throttle_counters() -> None:
    caches["throttle"].clear()


@pytest.fixture
def restored_logging() -> Iterator[None]:
    # Request it before `capsys`: the handlers it rebuilds must bind the streams capsys restores.
    yield
    configure_logging(django_settings.LOGGING_CONFIG, django_settings.LOGGING)


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
