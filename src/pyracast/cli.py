import os

from django.core.management import execute_from_command_line


def main() -> None:
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "pyracast.settings")
    execute_from_command_line()
