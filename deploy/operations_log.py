import json
import sys
import urllib.request
from typing import Any


def metadata(path: str) -> str:
    request = urllib.request.Request(
        "http://metadata.google.internal/computeMetadata/v1/" + path,
        headers={"Metadata-Flavor": "Google"},
    )
    with urllib.request.urlopen(request, timeout=10) as response:  # noqa: S310
        return str(response.read().decode())


def access_token() -> str:
    return str(json.loads(metadata("instance/service-accounts/default/token"))["access_token"])


def report(event: str, severity: str, **details: object) -> None:
    payload = {"event": event, **details}
    print(json.dumps(payload), file=sys.stderr, flush=True)
    try:
        project = metadata("project/project-id")
        body: dict[str, Any] = {
            "logName": f"projects/{project}/logs/pyracast-operations",
            "resource": {
                "type": "gce_instance",
                "labels": {
                    "project_id": project,
                    "instance_id": metadata("instance/id"),
                    "zone": metadata("instance/zone").rsplit("/", 1)[-1],
                },
            },
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
    except Exception:
        print('{"event":"pyracast_operations_log_failed"}', file=sys.stderr, flush=True)
