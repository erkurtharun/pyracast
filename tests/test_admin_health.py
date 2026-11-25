from dataclasses import replace

import pytest
from django.urls import reverse
from django.utils.translation import override

from pyracast.forecasts.models import DispatcherPhase
from pyracast.orchestration.delivery_health import CHECKS, HealthReport, PhaseState
from pyracast.platform_admin.health import HealthCategory, summarize_health
from tests.support.delivery_health import NOW

COUNTERS = [
    ("invalid_reminder_recipients", "action", "plants"),
    ("unvalidated_provider_credentials", "action", "plants"),
    ("blocked_provider_credentials", "action", "plants"),
    ("regions_without_recent_forecasts", "action", "plants"),
    ("provider_cooldowns", "delay", "plants"),
    ("overdue_collections", "action", "collections"),
    ("interrupted_collections", "action", "collections"),
    ("failed_collections", "review", "collections"),
    ("overdue_notifications", "action", "deliveries"),
    ("interrupted_notifications", "action", "deliveries"),
    ("failed_notifications", "review", "deliveries"),
    ("missed_notifications", "review", "deliveries"),
    ("mail_authentication_failures", "review", "deliveries"),
    ("paused_notifications", "review", "deliveries"),
    ("unavailable_forecasts", "review", "deliveries"),
    ("partial_forecasts", "review", "deliveries"),
    ("overdue_outbound_mail", "action", "mail"),
    ("failed_outbound_mail", "review", "mail"),
    ("uncertain_outbound_mail", "review", "uncertain_mail"),
    ("overdue_metering", "action", "metering"),
    ("interrupted_metering", "action", "metering"),
    ("failed_metering", "review", "metering"),
    ("overdue_calibrations", "action", "calibrations"),
    ("interrupted_calibrations", "action", "calibrations"),
    ("failed_calibrations", "review", "calibrations"),
]


def healthy_report(**counts: int) -> HealthReport:
    return HealthReport(
        checked_at=NOW,
        phases=tuple(
            PhaseState(phase, NOW, NOW, "", None, None, healthy=True) for phase in DispatcherPhase
        ),
        counts={
            **{item.key: 0 for item in CHECKS},
            "saved_forecast_notifications": 9,
            **counts,
        },
        requests=(),
    )


@pytest.mark.parametrize(("key", "category", "target"), COUNTERS)
def test_each_health_reason_has_its_count_guidance_and_filtered_destination(
    key: str, category: HealthCategory, target: str
) -> None:
    with override("en"):
        summary = summarize_health(healthy_report(**{key: 3}))
    assert len(summary.checks) == 1
    check = summary.checks[0]
    assert (check.key, check.count, check.category) == (key, 3, category)
    assert check.label
    assert check.help
    assert check.action_label
    if target == "plants":
        assert check.action_url == f"{reverse('admin:workspace_plants')}?issue={key}"
    elif target == "mail":
        assert check.action_url == reverse("admin:notifications_outboundmail_changelist")
    elif target == "uncertain_mail":
        assert check.action_url == (
            f"{reverse('admin:notifications_outboundmail_changelist')}?status__exact=uncertain"
        )
    else:
        assert check.action_url == (
            f"{reverse('admin:workspace_operations')}?issue={key}#{target}-title"
        )
    assert summary.tone == ("attention" if category == "action" else category)


def test_healthy_summary_has_no_attention_reasons_and_keeps_the_check_time() -> None:
    with override("en"):
        summary = summarize_health(healthy_report())
    assert summary.title == "Healthy"
    assert summary.tone == "healthy"
    assert summary.checks == ()
    assert summary.checked_at == NOW


def test_dispatcher_reason_names_affected_phases_and_links_to_their_status() -> None:
    report = healthy_report()
    report = replace(
        report,
        phases=tuple(
            replace(state, healthy=state.phase not in {"collection", "metering"})
            for state in report.phases
        ),
    )
    with override("en"):
        summary = summarize_health(report)
    assert summary.tone == "attention"
    assert len(summary.checks) == 1
    check = summary.checks[0]
    assert (check.key, check.count, check.category) == ("dispatch", 2, "action")
    assert check.help == (
        "Check the dispatcher process and its last successful run: Collection, Metering."
    )
    assert check.action_url == f"{reverse('admin:workspace_operations')}#dispatch-title"


@pytest.mark.parametrize(
    ("raised", "tone", "title"),
    [
        (("failed_metering", "partial_forecasts"), "review", "Recent outcomes need review"),
        (("failed_metering", "provider_cooldowns"), "delay", "Provider delay"),
        (
            ("provider_cooldowns", "overdue_notifications", "partial_forecasts"),
            "attention",
            "Action needed",
        ),
    ],
)
def test_summary_prioritizes_current_action_without_hiding_other_reasons(
    raised: tuple[str, ...], tone: str, title: str
) -> None:
    report = healthy_report(**dict.fromkeys(raised, 1))
    with override("en"):
        summary = summarize_health(report)
    assert summary.tone == tone
    assert summary.title == title
    assert {check.key for check in summary.checks} == set(raised)
    assert all(check.count == 1 for check in summary.checks)
    assert report.healthy is False


def test_prepared_notification_reason_does_not_claim_the_message_was_sent() -> None:
    with override("en"):
        check = summarize_health(healthy_report(partial_forecasts=1)).checks[0]
    assert check.label == "Forecast emails prepared with partial forecasts"
