import functools
import json
import sys
import urllib.request
from http.client import HTTPException
from typing import Any

# The link-local metadata server answers in milliseconds; a longer wait only delays reports.
METADATA_TIMEOUT_SECONDS = 2


def metadata(path: str) -> str:
    request = urllib.request.Request(
        "http://metadata.google.internal/computeMetadata/v1/" + path,
        headers={"Metadata-Flavor": "Google"},
    )
    with urllib.request.urlopen(request, timeout=METADATA_TIMEOUT_SECONDS) as response:  # noqa: S310
        return str(response.read().decode())


def access_token() -> str:
    payload = json.loads(metadata("instance/service-accounts/default/token"))
    token = payload.get("access_token") if isinstance(payload, dict) else None
    if not isinstance(token, str) or not token:
        raise ValueError("Invalid instance access token response.")
    return token


@functools.cache
def log_resource() -> tuple[str, dict[str, Any]]:
    project = metadata("project/project-id")
    return project, {
        "type": "gce_instance",
        "labels": {
            "project_id": project,
            "instance_id": metadata("instance/id"),
            "zone": metadata("instance/zone").rsplit("/", 1)[-1],
        },
    }


def report(event: str, severity: str, **details: object) -> None:
    payload = {"event": event, **details}
    print(json.dumps(payload), file=sys.stderr, flush=True)
    try:
        project, resource = log_resource()
        body: dict[str, Any] = {
            "logName": f"projects/{project}/logs/pyracast-operations",
            "resource": resource,
            "entries": [{"severity": severity, "jsonPayload": payload}],
        }
        request = urllib.request.Request(
            "https://logging.googleapis.com/v2/entries:write",
            data=json.dumps(body).encode(),
            headers={
                "Authorization": f"Bearer {access_token()}",
                "Content-Type": "application/json",
            },
        )
        with urllib.request.urlopen(request, timeout=15):  # noqa: S310
            pass
    except (OSError, ValueError, HTTPException):
        print('{"event":"pyracast_operations_log_failed"}', file=sys.stderr, flush=True)
