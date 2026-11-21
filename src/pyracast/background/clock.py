import time
from datetime import datetime, timedelta

from django.core.exceptions import ValidationError


class Clock:
    """The aware time a job was given, advanced by the monotonic time spent since then."""

    def __init__(self, entered: datetime) -> None:
        if entered.utcoffset() is None:
            raise ValidationError("An aware timestamp is required.")
        self.entered = entered
        self.started = time.monotonic()

    def elapsed(self) -> float:
        return time.monotonic() - self.started

    def now(self) -> datetime:
        return self.entered + timedelta(seconds=self.elapsed())
