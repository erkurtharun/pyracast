import json
import logging
from collections.abc import Iterator, Mapping
from typing import IO, Any
from urllib.parse import urlsplit

from django.http import HttpResponse
from rest_framework.exceptions import ParseError
from rest_framework.parsers import BaseParser
from rest_framework.request import Request
from rest_framework.views import APIView

from pyracast.errors import DomainError, ErrorCode

logger = logging.getLogger("pyracast.csp")
MAX_REPORT_BYTES = 16 * 1024
MAX_REPORTS = 10


def _origin(value: object) -> str:
    if not isinstance(value, str):
        return ""
    try:
        parts = urlsplit(value)
        port = parts.port
    except ValueError:
        return ""
    if parts.hostname:
        host = f"[{parts.hostname}]" if ":" in parts.hostname else parts.hostname
        return f"{parts.scheme}://{host}" + ("" if port is None else f":{port}")
    return parts.scheme or value[:32]


def _first_segment(value: object) -> str:
    try:
        path = urlsplit(value).path if isinstance(value, str) else ""
    except ValueError:
        return ""
    if not path.startswith("/"):
        return ""
    segment = path.split("/", 2)[1][:32]
    return f"/{segment}/" if segment else "/"


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


class _ReportParser(BaseParser):
    def parse(
        self,
        stream: IO[Any],
        media_type: str | None = None,
        parser_context: Mapping[str, Any] | None = None,
    ) -> Any:
        body = stream.read(MAX_REPORT_BYTES + 1)
        if len(body) > MAX_REPORT_BYTES:
            raise DomainError(ErrorCode.REQUEST_TOO_LARGE, status=413)
        try:
            return json.loads(body)
        except ValueError as error:
            raise ParseError from error


# Browsers send report-uri bodies as csp-report and Reporting API batches as reports+json.
class _CSPReportParser(_ReportParser):
    media_type = "application/csp-report"


class _ReportingParser(_ReportParser):
    media_type = "application/reports+json"


# Report URLs can carry credentials and tokens, so only the blocked origin and the document's
# first path segment are kept.
class CSPReportView(APIView):
    authentication_classes = ()
    parser_classes = (_CSPReportParser, _ReportingParser)
    schema = None

    def post(self, request: Request) -> HttpResponse:
        for count, violation in enumerate(_violations(request.data)):
            if count == MAX_REPORTS:
                break
            logger.info(
                "%s",
                json.dumps(
                    {
                        "event": "csp_violation",
                        "directive": str(violation["directive"] or "")[:64],
                        "blocked": _origin(violation["blocked"])[:128],
                        "document": _first_segment(violation["document"]),
                        "disposition": str(violation["disposition"] or "")[:32],
                    }
                ),
            )
        return HttpResponse(status=204)
