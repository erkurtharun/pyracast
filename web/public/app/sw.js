const NOTICE = `<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Pyracast cannot be reached | Pyracast'e ulaşılamıyor</title>
<style>
body { margin: 0; font-family: system-ui, sans-serif; background: #f6f7f9; color: #1d2430; }
main { max-width: 34rem; margin: 15vh auto; padding: 0 1rem; }
section + section { margin-top: 2rem; padding-top: 2rem; border-top: 1px solid #d6dae1; }
a { color: inherit; }
@media (prefers-color-scheme: dark) { body { background: #11151c; color: #e6e9ef; } }
</style>
</head>
<body>
<main>
<section lang="en">
<h1>Pyracast cannot be reached</h1>
<p>The server did not answer. Try again in a moment.</p>
<p><a href="">Try again</a></p>
</section>
<section lang="tr">
<h1>Pyracast'e ulaşılamıyor</h1>
<p>Sunucu yanıt vermedi. Biraz sonra tekrar deneyin.</p>
<p><a href="">Tekrar dene</a></p>
</section>
</main>
</body>
</html>
`;

const NOTICE_HEADERS = {
    "Content-Type": "text/html; charset=utf-8",
    "Cache-Control": "no-store",
    "Content-Security-Policy":
        "default-src 'none'; style-src 'unsafe-inline'; form-action 'none'; base-uri 'none'; frame-ancestors 'none'",
};

self.addEventListener("install", () => {
    self.skipWaiting();
});

self.addEventListener("activate", (event) => {
    if (self.registration.navigationPreload)
        event.waitUntil(self.registration.navigationPreload.enable());
});

self.addEventListener("fetch", (event) => {
    const { request } = event;
    if (
        request.mode === "navigate" &&
        request.method === "GET" &&
        request.url.startsWith(self.registration.scope)
    )
        event.respondWith(answer(event));
});

async function answer(event) {
    try {
        return (await event.preloadResponse) ?? (await fetch(event.request));
    } catch {
        return new Response(NOTICE, { status: 503, headers: NOTICE_HEADERS });
    }
}
