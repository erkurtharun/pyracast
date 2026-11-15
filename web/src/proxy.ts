import { NextResponse, type NextRequest } from "next/server";

export function proxy(request: NextRequest) {
    const nonce = Buffer.from(crypto.randomUUID()).toString("base64");
    const development = process.env.NODE_ENV === "development";
    const policy = [
        "default-src 'self'",
        `script-src 'self' 'nonce-${nonce}' 'strict-dynamic'${development ? " 'unsafe-eval'" : ""}`,
        "style-src 'self'",
        `style-src-elem 'self' ${development ? "'unsafe-inline'" : `'nonce-${nonce}'`}`,
        "style-src-attr 'unsafe-inline'",
        "img-src 'self' data: blob:",
        "font-src 'self'",
        `connect-src 'self'${development ? " ws:" : ""}`,
        "object-src 'none'",
        "base-uri 'self'",
        "form-action 'self'",
        "frame-ancestors 'none'",
    ].join("; ");
    const headers = new Headers(request.headers);
    const publicPath = [
        "/",
        "/tr",
        "/privacy",
        "/terms",
        "/tr/privacy",
        "/tr/terms",
    ].includes(request.nextUrl.pathname.replace(/\/$/, "") || "/");
    headers.delete("x-pyracast-public-language");
    if (publicPath)
        headers.set(
            "x-pyracast-public-language",
            request.nextUrl.pathname.startsWith("/tr") ? "tr" : "en",
        );
    headers.set("Content-Security-Policy", policy);
    headers.set("x-nonce", nonce);
    const response = NextResponse.next({ request: { headers } });
    response.headers.set("Content-Security-Policy", policy);
    response.headers.set("Cache-Control", "private, no-store");
    if (!publicPath) response.headers.set("X-Robots-Tag", "noindex, nofollow");
    return response;
}

export const config = {
    matcher: [
        "/((?!api/|admin(?:/|$)|static/|_next/static|_next/image|favicon.ico).*)",
    ],
};
