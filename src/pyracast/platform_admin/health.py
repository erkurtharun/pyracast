from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from django.urls import reverse
from django.utils.functional import Promise
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy

from pyracast.orchestration.delivery_health import HealthReport, check

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
    checked_at: datetime


@dataclass(frozen=True)
class Presentation:
    label: Promise
    help: Promise
    action_label: Promise
    category: HealthCategory


PLANTS = gettext_lazy("Review affected plants")
CONNECTIONS = gettext_lazy("Review provider connections")
COLLECTIONS = gettext_lazy("Review collections")
EMAILS = gettext_lazy("Review forecast emails")
OUTBOUND = gettext_lazy("Review outbound emails")
METERING = gettext_lazy("Review production collection")
CALIBRATIONS = gettext_lazy("Review calibrations")
RECOVERS = gettext_lazy("Check the dispatcher and worker; the dispatcher recovers expired leases.")

# The console order; every check that can make the report unhealthy has an entry.
PRESENTATION: Mapping[str, Presentation] = {
    "invalid_reminder_recipients": Presentation(
        gettext_lazy("Invalid forecast email recipients"),
        gettext_lazy("Ask the workspace administrator to correct the saved recipient addresses."),
        PLANTS,
        "action",
    ),
    "unvalidated_provider_credentials": Presentation(
        gettext_lazy("Provider keys awaiting validation"),
        gettext_lazy("Validate the current private key before collections resume."),
        CONNECTIONS,
        "action",
    ),
    "blocked_provider_credentials": Presentation(
        gettext_lazy("Provider keys needing attention"),
        gettext_lazy("Review the provider key and validate the connection after correcting it."),
        CONNECTIONS,
        "action",
    ),
    "regions_without_recent_forecasts": Presentation(
        gettext_lazy("Plants without recent forecasts"),
        gettext_lazy("Review the forecast update times and collection settings."),
        PLANTS,
        "action",
    ),
    "provider_cooldowns": Presentation(
        gettext_lazy("Provider request delays"),
        gettext_lazy(
            "Requests resume automatically after the provider delay. Preserve the shared quota."
        ),
        CONNECTIONS,
        "delay",
    ),
    "overdue_collections": Presentation(
        gettext_lazy("Delayed collections"),
        gettext_lazy("Check the collection dispatcher and worker."),
        COLLECTIONS,
        "action",
    ),
    "interrupted_collections": Presentation(
        gettext_lazy("Interrupted collections"), RECOVERS, COLLECTIONS, "action"
    ),
    "failed_collections": Presentation(
        gettext_lazy("Failed collections"),
        gettext_lazy(
            "Review the recorded error and the plant connection before changing settings."
        ),
        COLLECTIONS,
        "review",
    ),
    "overdue_notifications": Presentation(
        gettext_lazy("Delayed forecast emails"),
        gettext_lazy("Check the dedicated notification worker."),
        EMAILS,
        "action",
    ),
    "interrupted_notifications": Presentation(
        gettext_lazy("Interrupted forecast emails"),
        gettext_lazy(
            "Review the outcome; uncertain transmission must not be resent automatically."
        ),
        EMAILS,
        "action",
    ),
    "failed_notifications": Presentation(
        gettext_lazy("Forecast email outcomes needing review"),
        gettext_lazy(
            "Review failed, unavailable or unknown outcomes. "
            "Reconcile unknown acceptance before resending."
        ),
        EMAILS,
        "review",
    ),
    "missed_notifications": Presentation(
        gettext_lazy("Missed forecast email send times"),
        gettext_lazy("Check notification worker availability and the delivery deadline."),
        EMAILS,
        "review",
    ),
    "mail_authentication_failures": Presentation(
        gettext_lazy("Email authentication failures"),
        gettext_lazy(
            "Review the recorded failure and ask the deployment operator to check mail credentials."
        ),
        EMAILS,
        "review",
    ),
    "paused_notifications": Presentation(
        gettext_lazy("Paused forecast email send times"),
        gettext_lazy(
            "A plant's recipient list is over the limit, empty or holds an invalid address. "
            "Ask the workspace administrator to correct it."
        ),
        EMAILS,
        "review",
    ),
    "overdue_outbound_mail": Presentation(
        gettext_lazy("Delayed account and invitation emails"),
        gettext_lazy("Check the notification worker and the dispatcher."),
        OUTBOUND,
        "action",
    ),
    "failed_outbound_mail": Presentation(
        gettext_lazy("Failed account and invitation emails"),
        gettext_lazy(
            "Review the recorded error; the person or the workspace administrator can ask again."
        ),
        OUTBOUND,
        "review",
    ),
    "uncertain_outbound_mail": Presentation(
        gettext_lazy("Account and invitation emails with an unknown outcome"),
        gettext_lazy(
            "They may have arrived and are never resent; the person or the workspace "
            "administrator can ask again."
        ),
        OUTBOUND,
        "review",
    ),
    "unavailable_forecasts": Presentation(
        gettext_lazy("Forecast emails prepared without saved forecasts"),
        gettext_lazy("Review the plant collection history and approved connection."),
        EMAILS,
        "review",
    ),
    "partial_forecasts": Presentation(
        gettext_lazy("Forecast emails prepared with partial forecasts"),
        gettext_lazy("Review saved forecast coverage; missing intervals are not zero."),
        EMAILS,
        "review",
    ),
    "overdue_metering": Presentation(
        gettext_lazy("Delayed production collection"),
        gettext_lazy("Check the production dispatcher and worker."),
        METERING,
        "action",
    ),
    "interrupted_metering": Presentation(
        gettext_lazy("Interrupted production collection"), RECOVERS, METERING, "action"
    ),
    "failed_metering": Presentation(
        gettext_lazy("Failed production collection"),
        gettext_lazy(
            "Review the recorded error, target period and reported-production connection."
        ),
        METERING,
        "review",
    ),
    "overdue_calibrations": Presentation(
        gettext_lazy("Delayed calibrations"),
        gettext_lazy("Check the calibration worker and the dispatcher."),
        CALIBRATIONS,
        "action",
    ),
    "interrupted_calibrations": Presentation(
        gettext_lazy("Interrupted calibrations"), RECOVERS, CALIBRATIONS, "action"
    ),
    "failed_calibrations": Presentation(
        gettext_lazy("Failed calibrations"),
        gettext_lazy("Review the recorded error before requesting the calibration again."),
        CALIBRATIONS,
        "review",
    ),
}


def action_url(key: str) -> str:
    scope = check(key).scope
    if scope == "plants":
        return f"{reverse('admin:workspace_plants')}?issue={key}"
    if key == "uncertain_outbound_mail":
        return f"{reverse('admin:notifications_outboundmail_changelist')}?status__exact=uncertain"
    if scope == "mail":
        return reverse("admin:notifications_outboundmail_changelist")
    return f"{reverse('admin:workspace_operations')}?issue={key}#{scope}-title"


def presented(key: str, count: int) -> HealthCheck:
    presentation = PRESENTATION[key]
    return HealthCheck(
        key=key,
        label=str(presentation.label),
        count=count,
        help=str(presentation.help),
        action_url=action_url(key),
        action_label=str(presentation.action_label),
        category=presentation.category,
    )


def summarize_health(report: HealthReport) -> HealthSummary:
    checks = [presented(key, report.counts[key]) for key in PRESENTATION if report.counts[key] > 0]
    unhealthy_phases = [state for state in report.phases if not state.healthy]
    if unhealthy_phases:
        checks.insert(
            0,
            HealthCheck(
                key="dispatch",
                label=_("Dispatcher phases needing attention"),
                count=len(unhealthy_phases),
                help=_("Check the dispatcher process and its last successful run: %(phases)s.")
                % {"phases": ", ".join(str(state.phase.label) for state in unhealthy_phases)},
                action_url=f"{reverse('admin:workspace_operations')}#dispatch-title",
                action_label=_("Review dispatcher"),
                category="action",
            ),
        )
    if missing := report.missing_queues:
        checks.insert(
            0,
            HealthCheck(
                key="missing_queues",
                label=_("Missing message queues"),
                count=len(missing),
                help=_(
                    "The broker has not declared %(queues)s, so nothing consumes its messages. "
                    "Check that its worker runs."
                )
                % {"queues": ", ".join(missing)},
                action_url=f"{reverse('admin:workspace_operations')}#queues-title",
                action_label=_("Review queues"),
                category="action",
            ),
        )
    if report.broker_unreachable:
        checks.insert(
            0,
            HealthCheck(
                key="broker",
                label=_("Message broker not answering"),
                count=1,
                help=_(
                    "The dispatcher renews its heartbeat only after the message broker answers "
                    "and is restarted otherwise. Check the message broker."
                ),
                action_url=f"{reverse('admin:workspace_operations')}#dispatch-title",
                action_label=_("Review dispatcher"),
                category="action",
            ),
        )
    categories = {item.category for item in checks}
    if "action" in categories:
        return HealthSummary(_("Action needed"), "attention", tuple(checks), report.checked_at)
    if "delay" in categories:
        return HealthSummary(_("Provider delay"), "delay", tuple(checks), report.checked_at)
    if "review" in categories:
        return HealthSummary(
            _("Recent outcomes need review"), "review", tuple(checks), report.checked_at
        )
    return HealthSummary(_("Healthy"), "healthy", (), report.checked_at)
