from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from functools import partial
from typing import cast
from zoneinfo import ZoneInfo

from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.template.loader import render_to_string
from django.utils import translation
from django.utils.formats import date_format
from django.utils.translation import gettext_lazy as _

from pyracast.accounts.models import (
    Membership,
    OutboundMail,
    OutboundMailError,
    OutboundMailKind,
    WorkspaceAdminChange,
)
from pyracast.notifications.links import plant_url, workspace_url
from pyracast.plants.models import Plant
from pyracast.regions.models import AuditAction, AuditEvent, Region

FOOTER = _("You receive this email because of your role in %(workspace)s on Pyracast.")
REVIEW_TEXTS = {
    AuditAction.REGION_APPROVED: (
        _("%(plant)s is approved"),
        _(
            "Pyracast support approved %(plant)s in %(workspace)s on %(time)s (%(zone)s). "
            "Forecasts start with the next collection."
        ),
    ),
    AuditAction.REGION_REJECTED: (
        _("%(plant)s was not approved"),
        _(
            "Pyracast support did not approve %(plant)s in %(workspace)s on %(time)s "
            "(%(zone)s). Correct the plant's details and resubmit it for review."
        ),
    ),
    AuditAction.REGION_SUSPENDED: (
        _("%(plant)s is suspended"),
        _(
            "Pyracast support suspended %(plant)s in %(workspace)s on %(time)s (%(zone)s). "
            "Forecast collection and reminders are paused."
        ),
    ),
}


@dataclass(frozen=True)
class EventMessage:
    tenant_id: int
    workspace: str
    subject: str
    sentence: str
    action_label: str
    action_url: str
    reason: str = ""


def plant_name(region: Region) -> str:
    name = Plant.objects.filter(region=region).order_by("pk").values_list("name", flat=True)
    return next(iter(name), region.name)


def plant_time(moment: datetime, region: Region) -> str:
    return date_format(moment.astimezone(ZoneInfo(region.timezone)), "j M Y H:i")


def _subject(mail: OutboundMail) -> int:
    return int(mail.context["subject_id"])


def _review(mail: OutboundMail) -> EventMessage | None:
    event_id = _subject(mail)
    event = AuditEvent.objects.filter(pk=event_id).first()
    region = Region.objects.active().select_related("tenant").filter(auditevent=event_id).first()
    if event is None or region is None:
        return None
    values = {
        "plant": plant_name(region),
        "workspace": region.tenant.name,
        "time": plant_time(event.created_at, region),
        "zone": region.timezone,
    }
    subject, sentence = REVIEW_TEXTS[AuditAction(event.action)]
    return EventMessage(
        region.tenant_id,
        region.tenant.name,
        subject % values,
        sentence % values,
        str(_("Open plant")),
        plant_url(region),
        str(event.details.get("reason", "")),
    )


def _role(mail: OutboundMail, *, assigned: bool) -> EventMessage | None:
    change = WorkspaceAdminChange.objects.select_related("tenant").filter(pk=_subject(mail)).first()
    if change is None:
        return None
    values = {"workspace": change.tenant.name}
    if assigned:
        return EventMessage(
            change.tenant_id,
            change.tenant.name,
            _("You now administer %(workspace)s") % values,
            _(
                "Pyracast support made you the administrator of %(workspace)s. Automatic "
                "calibration was turned off for its plants and open invitations were cancelled; "
                "review the members and invite again where needed."
            )
            % values,
            str(_("Open members")),
            workspace_url(change.tenant_id, "members/"),
        )
    return EventMessage(
        change.tenant_id,
        change.tenant.name,
        _("Your administrator role in %(workspace)s moved") % values,
        _(
            "Pyracast support moved the administrator role of %(workspace)s to another member. "
            "You keep read-only access."
        )
        % values,
        str(_("Open workspace")),
        workspace_url(change.tenant_id),
    )


def _purge_warning(mail: OutboundMail) -> EventMessage | None:
    """A warning outlived by a restore, or by a newer warning after the plant was trashed
    again, is not sent."""
    region = (
        Region.objects.select_related("tenant")
        .filter(
            pk=_subject(mail),
            deleted_at__isnull=False,
            purge_started_at__isnull=True,
            purge_warning_sent_at__lte=mail.created_at,
        )
        .first()
    )
    if region is None:
        return None
    values = {
        "plant": plant_name(region),
        "workspace": region.tenant.name,
        "trashed": plant_time(cast(datetime, region.deleted_at), region),
        # The warning expires when the purge is due.
        "time": plant_time(mail.expires_at, region),
        "zone": region.timezone,
    }
    return EventMessage(
        region.tenant_id,
        region.tenant.name,
        _("%(plant)s will be deleted permanently") % values,
        _(
            "%(plant)s has been in the recycle bin of %(workspace)s since %(trashed)s. It will "
            "be deleted permanently on %(time)s (%(zone)s) with its forecasts and history. "
            "Restore it before then to keep it."
        )
        % values,
        str(_("Open recycle bin")),
        workspace_url(region.tenant_id, "trash/"),
    )


COMPOSERS: dict[str, Callable[[OutboundMail], EventMessage | None]] = {
    OutboundMailKind.PLANT_APPROVED: _review,
    OutboundMailKind.PLANT_REJECTED: _review,
    OutboundMailKind.PLANT_SUSPENDED: _review,
    OutboundMailKind.WORKSPACE_ADMIN_ASSIGNED: partial(_role, assigned=True),
    OutboundMailKind.WORKSPACE_ADMIN_RELEASED: partial(_role, assigned=False),
    OutboundMailKind.PURGE_WARNING: _purge_warning,
}


def _still_member(tenant_id: int, address: str) -> bool:
    return Membership.objects.filter(
        tenant_id=tenant_id, user__is_active=True, user__email__iexact=address
    ).exists()


def compose_event_mail(mail: OutboundMail) -> EmailMultiAlternatives | OutboundMailError:
    """Renders the intent from the row it names, so a reported item that is gone or a
    recipient who left the workspace receives nothing."""
    with translation.override(mail.language):
        message = COMPOSERS[mail.kind](mail)
        if message is None:
            return OutboundMailError.SUBJECT_UNAVAILABLE
        if not _still_member(message.tenant_id, mail.recipient):
            return OutboundMailError.RECIPIENT_UNAVAILABLE
        context = {
            "title": message.subject,
            "sentence": message.sentence,
            "reason": message.reason,
            "action_label": message.action_label,
            "action_url": message.action_url,
            "footer": FOOTER % {"workspace": message.workspace},
        }
        email = EmailMultiAlternatives(
            " ".join(message.subject.split()),
            render_to_string("notifications/workspace_event.txt", context).strip(),
            settings.DEFAULT_FROM_EMAIL,
            [mail.recipient],
            headers={"X-Pyracast-Mail": mail.kind},
        )
        email.attach_alternative(
            render_to_string("notifications/workspace_event.html", context), "text/html"
        )
    return email
