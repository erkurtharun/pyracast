from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal, cast

from django.urls import reverse
from django.utils.translation import gettext as _

type HealthCategory = Literal["action", "delay", "review"]
type HealthTone = Literal["attention", "delay", "review", "healthy"]


@dataclass(frozen=True)
class HealthCheck:
    key: str
    label: str
    count: int
    help: str
    action_url: str
    action_label: str
    category: HealthCategory


@dataclass(frozen=True)
class HealthSummary:
    title: str
    tone: HealthTone
    checks: tuple[HealthCheck, ...]


def summarize_health(report: Mapping[str, object]) -> HealthSummary:
    definitions: tuple[tuple[str, str, str, str, HealthCategory, str], ...] = (
        (
            "invalid_reminder_recipients",
            _("Invalid email recipients"),
            _("Ask the workspace administrator to correct the saved recipient addresses."),
            _("Review affected plants"),
            "action",
            "plants",
        ),
        (
            "unvalidated_provider_credentials",
            _("Provider keys awaiting validation"),
            _("Validate the current private key before collections resume."),
            _("Review provider connections"),
            "action",
            "plants",
        ),
        (
            "blocked_provider_credentials",
            _("Provider keys needing attention"),
            _("Review the provider key and validate the connection after correcting it."),
            _("Review provider connections"),
            "action",
            "plants",
        ),
        (
            "regions_without_recent_forecasts",
            _("Plants without recent forecasts"),
            _("Review saved acquisition times and collection settings."),
            _("Review affected plants"),
            "action",
            "plants",
        ),
        (
            "provider_cooldowns",
            _("Provider request delays"),
            _("Requests resume automatically after the provider delay. Preserve the shared quota."),
            _("Review provider connections"),
            "delay",
            "plants",
        ),
        (
            "overdue_collections",
            _("Delayed collections"),
            _("Check the collection dispatcher and worker."),
            _("Review collections"),
            "action",
            "collections",
        ),
        (
            "interrupted_collections",
            _("Interrupted collections"),
            _("Check the dispatcher and worker; the dispatcher recovers expired leases."),
            _("Review collections"),
            "action",
            "collections",
        ),
        (
            "failed_collections",
            _("Failed collections"),
            _("Review the recorded error and the plant connection before changing settings."),
            _("Review collections"),
            "review",
            "collections",
        ),
        (
            "overdue_notifications",
            _("Delayed emails"),
            _("Check the dedicated notification worker."),
            _("Review emails"),
            "action",
            "deliveries",
        ),
        (
            "interrupted_notifications",
            _("Interrupted emails"),
            _("Review the outcome; uncertain transmission must not be resent automatically."),
            _("Review emails"),
            "action",
            "deliveries",
        ),
        (
            "failed_notifications",
            _("Email outcomes needing review"),
            _(
                "Review failed, unavailable or unknown outcomes. "
                "Reconcile unknown acceptance before resending."
            ),
            _("Review emails"),
            "review",
            "deliveries",
        ),
        (
            "missed_notifications",
            _("Missed email times"),
            _("Check notification worker availability and the delivery deadline."),
            _("Review emails"),
            "review",
            "deliveries",
        ),
        (
            "mail_authentication_failures",
            _("Email authentication failures"),
            _(
                "Review the recorded failure and ask the deployment operator "
                "to check mail credentials."
            ),
            _("Review emails"),
            "review",
            "deliveries",
        ),
        (
            "paused_notifications",
            _("Paused email times"),
            _(
                "A plant's recipient list is over the limit, empty or holds an invalid address. "
                "Ask the workspace administrator to correct it."
            ),
            _("Review emails"),
            "review",
            "deliveries",
        ),
        (
            "overdue_outbound_mail",
            _("Delayed account and invitation emails"),
            _("Check the notification worker and the dispatcher."),
            _("Review outbound emails"),
            "action",
            "mail",
        ),
        (
            "failed_outbound_mail",
            _("Account and invitation emails needing review"),
            _("Review failed or unknown outcomes; an unknown outcome is never resent."),
            _("Review outbound emails"),
            "review",
            "mail",
        ),
        (
            "unavailable_forecasts",
            _("Notifications prepared without saved forecasts"),
            _("Review the plant collection history and approved connection."),
            _("Review emails"),
            "review",
            "deliveries",
        ),
        (
            "partial_forecasts",
            _("Notifications prepared with partial forecasts"),
            _("Review saved forecast coverage; missing intervals are not zero."),
            _("Review emails"),
            "review",
            "deliveries",
        ),
        (
            "overdue_metering",
            _("Delayed production collection"),
            _("Check the production dispatcher and worker."),
            _("Review production collection"),
            "action",
            "metering",
        ),
        (
            "interrupted_metering",
            _("Interrupted production collection"),
            _("Check the dispatcher and worker; the dispatcher recovers expired leases."),
            _("Review production collection"),
            "action",
            "metering",
        ),
        (
            "failed_metering",
            _("Failed production collection"),
            _("Review the recorded error, target period and reported-production connection."),
            _("Review production collection"),
            "review",
            "metering",
        ),
    )
    checks = [
        HealthCheck(
            key=key,
            label=label,
            count=count,
            help=help_text,
            action_url=(
                f"{reverse('admin:workspace_plants')}?issue={key}"
                if target == "plants"
                else reverse("admin:notifications_outboundmail_changelist")
                if target == "mail"
                else f"{reverse('admin:workspace_operations')}?issue={key}#{target}-title"
            ),
            action_label=action_label,
            category=category,
        )
        for key, label, help_text, action_label, category, target in definitions
        if (count := cast(int, report[key])) > 0
    ]
    unhealthy_phases = cast(list[str], report["unhealthy_dispatch_phases"])
    if unhealthy_phases:
        phase_labels = {
            "collection": _("Collection"),
            "notifications": _("Notifications"),
            "calibration": _("Calibration"),
            "retention": _("Retention"),
            "metering": _("Production collection"),
        }
        checks.insert(
            0,
            HealthCheck(
                key="dispatch",
                label=_("Dispatcher phases needing attention"),
                count=len(unhealthy_phases),
                help=_("Check the dispatcher process and its last successful run: %(phases)s.")
                % {
                    "phases": ", ".join(
                        phase_labels.get(phase, phase) for phase in unhealthy_phases
                    )
                },
                action_url=f"{reverse('admin:workspace_operations')}#dispatch-title",
                action_label=_("Review dispatcher"),
                category="action",
            ),
        )
    categories = {check.category for check in checks}
    if "action" in categories:
        return HealthSummary(_("Action needed"), "attention", tuple(checks))
    if "delay" in categories:
        return HealthSummary(_("Provider delay"), "delay", tuple(checks))
    if "review" in categories:
        return HealthSummary(_("Recent outcomes need review"), "review", tuple(checks))
    return HealthSummary(_("Healthy"), "healthy", ())
