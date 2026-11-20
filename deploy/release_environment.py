import argparse
import os
import re
import sys
from collections.abc import Sequence
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from deploy import host_contract
from deploy.host_contract import HostConfig

# Compose reads release.env literally; a value outside this set could break or extend it.
VALUE = re.compile(r"[a-zA-Z0-9@.:/_+%-]+")
ORIGIN_PULL_CA = "origin-pull-ca.pem"


def render(config: HostConfig, secrets: str, images: Sequence[str], deploy: Path) -> dict[str, str]:
    values = {
        **dict(zip(host_contract.IMAGE_KEYS, images, strict=True)),
        "SITE_ADDRESS": config.domain,
        "ACME_EMAIL": config.acme_email,
        **{key: f"{secrets}/{name}" for key, name in host_contract.SECRET_FILES.items()},
        "ORIGIN_PULL": "off",
    }
    if any(not VALUE.fullmatch(value) for value in values.values()):
        raise ValueError("Invalid nonsecret deployment configuration.")
    if config.origin_pull_ca:
        stored = deploy / ORIGIN_PULL_CA
        stored.write_text(config.origin_pull_ca, encoding="utf-8")
        # The gateway reads the bind-mounted file as its own unprivileged user.
        stored.chmod(0o644)
        values |= {"ORIGIN_PULL": "required", host_contract.ORIGIN_PULL_CA_KEY: stored.as_posix()}
    return values


def main(arguments: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("host_config", type=Path)
    parser.add_argument("release_env", type=Path)
    parser.add_argument("secrets")
    parser.add_argument("images", nargs=5)
    options = parser.parse_args(arguments)
    try:
        config = host_contract.load(options.host_config)
        values = render(config, options.secrets, options.images, options.release_env.parent)
    except ValueError as error:
        print(f"Release refused before maintenance: {error}", file=sys.stderr)
        return 2
    with open(options.release_env, "w", encoding="utf-8", newline="\n") as stream:
        stream.write("".join(f"{key}={value}\n" for key, value in values.items()))
        stream.flush()
        os.fsync(stream.fileno())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
