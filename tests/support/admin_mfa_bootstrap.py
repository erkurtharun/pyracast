import re
from collections.abc import Callable
from io import StringIO
from urllib.parse import parse_qs, urlsplit

from allauth.account.models import EmailAddress
from django.contrib.auth.models import User
from django.core.management import call_command

TEST_PASSWORD = "test-only-" + "z" * 40
NEW_PASSWORD = "operator-only-" + "glacier-meadow-42"

Answer = str | Callable[[str], str]


def administrator() -> User:
    user = User.objects.create_user(
        username="operator",
        email="operator@example.com",
        password=TEST_PASSWORD,
        is_staff=True,
        is_superuser=True,
    )
    EmailAddress.objects.create(user=user, email=user.email, primary=True, verified=True)
    return user


class Terminal:
    # The operator's answers; a callable answer reads what the command has shown so far.
    def __init__(self, shown: StringIO, answers: list[Answer]) -> None:
        self.shown = shown
        self.answers = list(answers)
        self.asked = 0

    def isatty(self) -> bool:
        return False

    def readline(self) -> str:
        self.asked += 1
        if not self.answers:
            return ""
        answer = self.answers.pop(0)
        return (answer(self.shown.getvalue()) if callable(answer) else answer) + "\n"


def setup_link(shown: str) -> str:
    match = re.search(r"otpauth://\S+", shown)
    assert match is not None
    return match.group(0)


def enrollment_secret(shown: str) -> str:
    return parse_qs(urlsplit(setup_link(shown)).query)["secret"][0]


def run(command: str, answers: list[Answer], **options: object) -> tuple[str, Terminal]:
    shown = StringIO()
    terminal = Terminal(shown, answers)
    call_command(command, stdin=terminal, stdout=shown, stderr=StringIO(), **options)
    return shown.getvalue(), terminal
