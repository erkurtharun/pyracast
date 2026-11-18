import json
import logging
from collections.abc import Iterator
from typing import Any
from urllib.parse import urlsplit

from django.http import HttpRequest, HttpResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

logger = logging.getLogger("pyracast.csp")
MAX_REPORT_BYTES = 16 * 1024
MAX_REPORTS = 10
# Browsers send report-uri bodies as csp-report and Reporting API batches as reports+json.
CONTENT_TYPES = frozenset({"application/csp-report", "application/reports+json"})


def _origin_only(value: object) -> str:
    if not isinstance(value, str):
        return ""
    parts = urlsplit(value)
    if parts.netloc:
        return f"{parts.scheme}://{parts.netloc}"
    return parts.scheme or value[:32]


def _path_only(value: object) -> str:
    return urlsplit(value).path if isinstance(value, str) else ""


def _violations(document: object) -> Iterator[dict[str, Any]]:
    if isinstance(document, dict) and isinstance(document.get("csp-report"), dict):
        report = document["csp-report"]
        yield {
            "directive": report.get("effective-directive"),
            "blocked": report.get("blocked-uri"),
            "document": report.get("document-uri"),
            "disposition": report.get("disposition"),
        }
    elif isinstance(document, list):
        for entry in document:
            if (
                isinstance(entry, dict)
                and entry.get("type") == "csp-violation"
                and isinstance(entry.get("body"), dict)
            ):
                body = entry["body"]
                yield {
                    "directive": body.get("effectiveDirective"),
                    "blocked": body.get("blockedURL"),
                    "document": body.get("documentURL"),
                    "disposition": body.get("disposition"),
                }


# Report URLs can carry tokens in paths and queries, so only origins and document paths are kept.
@csrf_exempt
@require_POST
def csp_report(request: HttpRequest) -> HttpResponse:
    if request.content_type in CONTENT_TYPES:
        body = request.read(MAX_REPORT_BYTES + 1)
        try:
            document = json.loads(body) if len(body) <= MAX_REPORT_BYTES else None
        except ValueError:
            document = None
        for count, violation in enumerate(_violations(document)):
            if count == MAX_REPORTS:
                break
            logger.info(
                "%s",
                json.dumps(
                    {
                        "event": "csp_violation",
                        "directive": str(violation["directive"] or ""),
                        "blocked": _origin_only(violation["blocked"]),
                        "document": _path_only(violation["document"]),
                        "disposition": str(violation["disposition"] or ""),
                    }
                ),
            )
    return HttpResponse(status=204)
