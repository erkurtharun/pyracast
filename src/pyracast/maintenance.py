import json
import socket
from http.cookies import _unquote
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from pyracast.languages import LANGUAGE_COOKIE, Language, negotiated_language

PORT = 8080
RETRY_AFTER_SECONDS = 60
CODE = "maintenance"
TOO_LARGE_CODE = "request_too_large"
# The API catalog carries the same texts for ErrorCode.MAINTENANCE; a test keeps them equal.
MESSAGES: dict[Language, str] = {
    "en": "Pyracast is being updated. Try again in a minute; your data is safe.",
    "tr": "Pyracast güncelleniyor. Bir dakika sonra yeniden deneyin; verileriniz güvende.",
}
# The gateway's refusal of a body over its limit, catalogued as ErrorCode.REQUEST_TOO_LARGE.
TOO_LARGE_MESSAGES: dict[Language, str] = {
    "en": "The request is too large to be accepted.",
    "tr": "İstek kabul edilemeyecek kadar büyük.",
}
PAGE = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="{RETRY_AFTER_SECONDS}">
<title>Pyracast maintenance</title>
<style>
body {{ margin: 0; font-family: system-ui, sans-serif; background: #f9f8f3; color: #1d2430; }}
main {{ max-width: 34rem; margin: 15vh auto; padding: 0 1rem; }}
section + section {{ margin-top: 2rem; padding-top: 2rem; border-top: 1px solid #d6dae1; }}
@media (prefers-color-scheme: dark) {{ body {{ background: #051e1f; color: #f4f2e8; }} }}
</style>
</head>
<body>
<main>
<section lang="en">
<h1>Pyracast is being updated</h1>
<p>The service is back in a few minutes. Your data is safe; this page reloads by itself.</p>
</section>
<section lang="tr">
<h1>Pyracast güncelleniyor</h1>
<p>Hizmet birkaç dakika içinde geri gelecek. Verileriniz güvende; bu sayfa kendini yeniler.</p>
</section>
</main>
</body>
</html>
""".encode()


def api_body(language: Language) -> bytes:
    return json.dumps(
        {"code": CODE, "message": MESSAGES[language], "retry_after": RETRY_AFTER_SECONDS}
    ).encode()


def too_large_body(language: Language) -> bytes:
    return json.dumps({"code": TOO_LARGE_CODE, "message": TOO_LARGE_MESSAGES[language]}).encode()


def cookie_language(header: str) -> str | None:
    # Django's parse_cookie: every ;-separated pair counts, the last of a name wins, values are
    # unquoted alike.
    language = None
    for chunk in header.split(";"):
        key, separator, value = chunk.partition("=")
        if separator and key.strip() == LANGUAGE_COOKIE:
            language = _unquote(value.strip())
    return language


class Notice(BaseHTTPRequestHandler):
    def answer(self, status: int, content_type: str, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if status == 503:
            self.send_header("Retry-After", str(RETRY_AFTER_SECONDS))
        self.end_headers()
        self.wfile.write(body)

    def language(self) -> Language:
        return negotiated_language(
            cookie_language(self.headers.get("Cookie", "")),
            self.headers.get("Accept-Language", ""),
            self.headers.get("CF-IPCountry"),
        )

    # Traefik's errors middleware always asks with GET: /page for frontend, /api for backend
    # routes, /api/413 for an API body over the gateway's limit.
    def do_GET(self) -> None:
        if self.path == "/health":
            self.answer(200, "text/plain", b"ok")
        elif self.path == "/api/413":
            self.answer(413, "application/json", too_large_body(self.language()))
        elif self.path == "/api" and "text/html" not in self.headers.get("Accept", ""):
            self.answer(503, "application/json", api_body(self.language()))
        else:
            self.answer(503, "text/html; charset=utf-8", PAGE)


def server(host: str, port: int) -> ThreadingHTTPServer:
    return ThreadingHTTPServer((host, port), Notice)


def main(port: int = PORT) -> None:
    with server(socket.gethostbyname(socket.gethostname()), port) as notice:
        notice.serve_forever()
