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
from django.utils.functional import Promise
from django.utils.translation import gettext_lazy as _

from pyracast.accounts.account_mail import mail_time
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

FOOTER = _("You receive this email because of your role in %(workspace)s.")


@dataclass(frozen=True)
class ReviewText:
    subject: Promise
    sentence: Promise
    time_label: Promise
    next_step: Promise | None = None


REVIEW_TEXTS = {
    AuditAction.REGION_APPROVED: ReviewText(
        _("%(plant)s is approved"), _("Pyracast approved the plant."), _("Approved at")
    ),
    AuditAction.REGION_REJECTED: ReviewText(
        _("%(plant)s was not approved"),
        _("Pyracast did not approve the plant."),
        _("Decided at"),
        _("Correct the details and send the plant for approval again."),
    ),
    AuditAction.REGION_SUSPENDED: ReviewText(
        _("%(plant)s is suspended"),
        _("Pyracast suspended the plant. Forecasts and forecast emails for it are stopped."),
        _("Suspended at"),
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
    details: tuple[tuple[str, str], ...] = ()
    next_step: str = ""


def plant_name(region: Region) -> str:
    name = Plant.objects.filter(region=region).order_by("pk").values_list("name", flat=True)
    return next(iter(name), region.name)


def plant_time(moment: datetime, region: Region) -> str:
    return mail_time(moment, ZoneInfo(region.timezone))


def _plant_details(plant: str, region: Region) -> tuple[tuple[str, str], ...]:
    return ((str(_("Plant")), plant), (str(_("Workspace")), region.tenant.name))


def _subject(mail: OutboundMail) -> int:
    return int(mail.context["subject_id"])


def _review(mail: OutboundMail) -> EventMessage | None:
    event_id = _subject(mail)
    event = AuditEvent.objects.filter(pk=event_id).first()
    region = Region.objects.active().select_related("tenant").filter(auditevent=event_id).first()
    if event is None or region is None:
        return None
    plant = plant_name(region)
    text = REVIEW_TEXTS[AuditAction(event.action)]
    reason = str(event.details.get("reason", ""))
    return EventMessage(
        region.tenant_id,
        region.tenant.name,
        text.subject % {"plant": plant},
        str(text.sentence),
        str(_("Open plant")),
        plant_url(region),
        (
            *_plant_details(plant, region),
            (str(text.time_label), plant_time(event.created_at, region)),
            *(((str(_("Reason")), reason),) if reason else ()),
        ),
        str(text.next_step or ""),
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
            _("You are now the workspace administrator of %(workspace)s") % values,
            str(
                _(
                    "Pyracast made you the workspace administrator and withdrew the pending "
                    "invitations. Check the members and invite again where needed."
                )
            ),
            str(_("Open members")),
            workspace_url(change.tenant_id, "members/"),
        )
    return EventMessage(
        change.tenant_id,
        change.tenant.name,
        _("You are no longer the workspace administrator of %(workspace)s") % values,
        str(
            _(
                "Pyracast handed the workspace administrator role over to another member. You "
                "are now a member of the workspace."
            )
        ),
        str(_("Open workspace")),
        workspace_url(change.tenant_id),
    )


def _purge_warning(mail: OutboundMail) -> EventMessage | None:
    """A warning outlived by a restore, or by a newer warning after the plant was trashed
    again, is not sent."""
    region = (
        Region.objects.trashed()
        .select_related("tenant")
        .filter(
            pk=_subject(mail),
            purge_started_at__isnull=True,
            purge_warning_sent_at__lte=mail.created_at,
        )
        .first()
    )
    if region is None:
        return None
    plant = plant_name(region)
    return EventMessage(
        region.tenant_id,
        region.tenant.name,
        _("%(plant)s will be deleted permanently") % {"plant": plant},
        str(_("The plant is in the recycle bin.")),
        str(_("Open recycle bin")),
        workspace_url(region.tenant_id, "trash/"),
        (
            *_plant_details(plant, region),
            (str(_("Deleted at")), plant_time(cast(datetime, region.deleted_at), region)),
            # The warning expires when the purge is due.
            (str(_("Deleted permanently at")), plant_time(mail.expires_at, region)),
        ),
        str(_("Restore the plant before then to keep it with its forecasts and history.")),
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
            "details": message.details,
            "next_step": message.next_step,
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
