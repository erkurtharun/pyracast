import { NextResponse, type NextRequest } from "next/server";
import { publicPath, publicRoute } from "@/lib/public-metadata";

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
        "base-uri 'none'",
        "form-action 'self'",
        "frame-ancestors 'none'",
        "report-uri /api/csp-reports/",
    ].join("; ");
    const route = publicRoute(request.nextUrl.pathname);
    // Read the slash from the raw URL: nextUrl may normalize it under trailingSlash.
    const requested = new URL(request.url);
    if (route && !requested.pathname.endsWith("/"))
        return NextResponse.redirect(
            new URL(
                `${publicPath(route.language, route.page)}${requested.search}`,
                request.url,
            ),
            308,
        );
    const headers = new Headers(request.headers);
    headers.delete("x-pyracast-public-language");
    if (route) headers.set("x-pyracast-public-language", route.language);
    headers.set("Content-Security-Policy", policy);
    headers.set("x-nonce", nonce);
    const response = NextResponse.next({ request: { headers } });
    response.headers.set("Content-Security-Policy", policy);
    response.headers.set("Cache-Control", "private, no-store");
    if (!route) response.headers.set("X-Robots-Tag", "noindex, nofollow");
    return response;
}

export const config = {
    matcher: [
        "/((?!api/|admin(?:/|$)|static/|_next/static|_next/image|favicon.ico|robots\\.txt$|sitemap\\.xml$|icon\\.svg$|(?:tr/)?opengraph-image|identity/|app/(?:manifest\\.webmanifest|icon-(?:192|512|512-maskable)\\.png|apple-touch-icon\\.png)$).*)",
    ],
};
