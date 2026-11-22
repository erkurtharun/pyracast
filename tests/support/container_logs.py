from typing import Any

# Docker's log copier ships each line of a container stream as one message and cuts a line longer
# than 16 KiB into several; gcplogs (moby daemon/logger/gcplogs) writes each message as
# jsonPayload.message beside the container's name, which Docker prefixes with "/".
DOCKER_LINE_BYTES = 16 * 1024


def container_entry(message: str, container: str, project: str = "project") -> dict[str, Any]:
    return {
        "logName": f"projects/{project}/logs/gcplogs-docker-driver",
        "resource": {"type": "gce_instance"},
        "jsonPayload": {"message": message, "container": {"name": container}},
    }


def shipped(stream: str, container: str) -> list[dict[str, Any]]:
    """The entries gcplogs writes for everything a container wrote to one stream."""
    lines = stream.split("\n")
    if lines[-1] == "":
        lines.pop()
    entries = []
    for line in lines:
        encoded = line.encode()
        for start in range(0, max(len(encoded), 1), DOCKER_LINE_BYTES):
            part = encoded[start : start + DOCKER_LINE_BYTES].decode(errors="replace")
            entries.append(container_entry(part, container))
    return entries
