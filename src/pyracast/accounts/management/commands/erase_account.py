from typing import Any

from django.contrib.auth.models import User
from django.core.management.base import BaseCommand, CommandError, CommandParser

from pyracast.accounts.erasure import AdministersWorkspaceError, erase_account


class Command(BaseCommand):
    help = (
        "Erase an account by anonymizing it in place. Without --execute it is a dry run that "
        "reports what would change. Erasure cannot be undone."
    )

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--account", type=int, required=True, help="The account id.")
        parser.add_argument("--execute", action="store_true")

    def handle(self, *args: Any, **options: Any) -> None:
        account_id, execute = options["account"], options["execute"]
        try:
            erasure = erase_account(account_id, execute=execute)
        except User.DoesNotExist:
            raise CommandError(f"Account {account_id} does not exist.") from None
        except AdministersWorkspaceError as error:
            raise CommandError(str(error)) from None
        self.stdout.write(
            f"Erased account {account_id}:"
            if execute
            else f"Dry run: erasing account {account_id} would change:"
        )
        for label, count in erasure.changes.items():
            self.stdout.write(f"  {label}: {count}")
        if not execute:
            self.stdout.write(
                "Nothing was changed. Erasure cannot be undone: take a backup, then run again "
                "with --execute."
            )
