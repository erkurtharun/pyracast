import argparse
import json
import re
import sys
from collections.abc import Sequence

# The image_rescan_setting output of infra/terraform, stored as the repository variable
# GCP_IMAGE_RESCAN; each value reaches $GITHUB_OUTPUT, so none may hold a line break.
FIELDS = {
    "provider": re.compile(
        r"projects/[0-9]+/locations/global/workloadIdentityPools/[a-z0-9-]+/providers/[a-z0-9-]+"
    ),
    "service_account": re.compile(
        r"[a-z][a-z0-9-]{4,28}[a-z0-9]@[a-z0-9-]+\.iam\.gserviceaccount\.com"
    ),
    "repository": re.compile(r"[a-z0-9-]+-docker\.pkg\.dev/[a-z0-9-]+/[a-z0-9_-]+"),
}
REFUSAL = (
    "GCP_IMAGE_RESCAN must be the image_rescan_setting output of Terraform: a JSON object of "
    "provider, service_account and repository."
)


def outputs(setting: str) -> dict[str, str]:
    values = json.loads(setting)
    if not isinstance(values, dict) or set(values) != set(FIELDS):
        raise ValueError(REFUSAL)
    for name, pattern in FIELDS.items():
        if not isinstance(values[name], str) or pattern.fullmatch(values[name]) is None:
            raise ValueError(REFUSAL)
    return {name: values[name] for name in FIELDS}


def main(arguments: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Print the registry reader of the deployed images as workflow step outputs."
    )
    parser.add_argument("setting")
    options = parser.parse_args(arguments)
    try:
        values = outputs(options.setting)
    except ValueError:
        print(REFUSAL, file=sys.stderr)
        return 2
    print("".join(f"{name}={value}\n" for name, value in values.items()), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
