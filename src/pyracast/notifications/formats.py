import re
from datetime import date, datetime, timedelta
from typing import cast

from django.utils.formats import date_format, number_format
from django.utils.translation import gettext as _
from django.utils.translation import pgettext

_TURKISH = str.maketrans("ıİşŞğĞüÜöÖçÇ", "iIsSgGuUoOcC")
_SEPARATORS = re.compile(r"[^a-z0-9]+")


def day_text(day: date) -> str:
    return date_format(day, pgettext("forecast mail day", "D j M Y"))


def subject_day(day: date) -> str:
    return date_format(day, pgettext("forecast mail subject day", "D j M"))


def long_day(day: date) -> str:
    return date_format(day, pgettext("forecast mail sentence day", "l j F Y"))


def moment_text(moment: datetime) -> str:
    return date_format(moment, pgettext("forecast mail time", "j M Y, H:i"))


def utc_offset(moment: datetime) -> str:
    minutes = round(cast(timedelta, moment.utcoffset()).total_seconds() / 60)
    hours, rest = divmod(abs(minutes), 60)
    return f"UTC{'-' if minutes < 0 else '+'}{hours}" + (f":{rest:02d}" if rest else "")


def energy_number(value: float) -> str:
    return number_format(round(value, 2) + 0.0, decimal_pos=2, force_grouping=True)


def with_edition(subject: str, edition: str | None) -> str:
    if edition is None:
        return subject
    return _("%(subject)s (%(clock)s edition)") % {"subject": subject, "clock": edition}


def file_name(plant: str, day: date, edition: str | None) -> str:
    slug = _SEPARATORS.sub("-", plant.translate(_TURKISH).lower()).strip("-")
    slug = slug[:40].strip("-") or "plant"
    clock = f"-{edition.replace(':', '')}" if edition else ""
    return f"pyracast-{slug}-{day.isoformat()}{clock}.xlsx"
