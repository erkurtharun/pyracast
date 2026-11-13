from pathlib import Path

from dotenv import load_dotenv


def load_environment() -> None:
    load_dotenv(Path.cwd() / ".env", override=False, interpolate=False)
