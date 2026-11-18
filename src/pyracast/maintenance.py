import json
import socket
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from pyracast.languages import LANGUAGE_COOKIE, Language, negotiated_language

PORT = 8080
RETRY_AFTER_SECONDS = 60
CODE = "maintenance"
# The API catalog carries the same texts for ErrorCode.MAINTENANCE; a test keeps them equal.
MESSAGES: dict[Language, str] = {
    "en": "Pyracast is being updated. Try again in a minute; your data is safe.",
    "tr": "Pyracast güncelleniyor. Bir dakika sonra yeniden deneyin; verileriniz güvende.",
}
PAGE = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="{RETRY_AFTER_SECONDS}">
<title>Pyracast maintenance</title>
<style>
body {{ margin: 0; font-family: system-ui, sans-serif; background: #f6f7f9; color: #1d2430; }}
main {{ max-width: 34rem; margin: 15vh auto; padding: 0 1rem; }}
section + section {{ margin-top: 2rem; padding-top: 2rem; border-top: 1px solid #d6dae1; }}
@media (prefers-color-scheme: dark) {{ body {{ background: #11151c; color: #e6e9ef; }} }}
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


def cookie_language(header: str) -> str | None:
    try:
        morsel = SimpleCookie(header).get(LANGUAGE_COOKIE)
    except CookieError:
        return None
    return None if morsel is None else morsel.value


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

    # Traefik's errors middleware always asks with GET: /page for frontend, /api for backend routes.
    def do_GET(self) -> None:
        if self.path == "/health":
            self.answer(200, "text/plain", b"ok")
        elif self.path == "/api" and "text/html" not in self.headers.get("Accept", ""):
            language = negotiated_language(
                cookie_language(self.headers.get("Cookie", "")),
                self.headers.get("Accept-Language", ""),
                self.headers.get("CF-IPCountry"),
            )
            self.answer(503, "application/json", api_body(language))
        else:
            self.answer(503, "text/html; charset=utf-8", PAGE)


def server(host: str, port: int) -> ThreadingHTTPServer:
    return ThreadingHTTPServer((host, port), Notice)


def main(port: int = PORT) -> None:
    with server(socket.gethostbyname(socket.gethostname()), port) as notice:
        notice.serve_forever()
