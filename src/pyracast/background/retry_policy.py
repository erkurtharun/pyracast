import secrets
from dataclasses import dataclass
from datetime import timedelta


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int

    def exhausted(self, attempts: int) -> bool:
        return attempts >= self.max_attempts


@dataclass(frozen=True)
class BackoffPolicy(RetryPolicy):
    longest_delay: timedelta

    def delay(self, attempts: int) -> timedelta:
        """30 s doubled for each attempt spent, at most the longest delay, plus up to 30 s."""
        doubled = min(30 * 2**attempts, self.longest_delay.total_seconds())
        return timedelta(seconds=doubled + secrets.randbelow(31))


# Collection and EPIAS jobs take their delays from the provider's answer and their windows.
PROVIDER_JOB = RetryPolicy(max_attempts=3)
CALIBRATION = RetryPolicy(max_attempts=3)
MAIL = BackoffPolicy(max_attempts=8, longest_delay=timedelta(minutes=30))
