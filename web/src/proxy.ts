import { NextResponse, type NextRequest } from "next/server";
import { isLanguage } from "@/i18n/language";
import {
    defaultLanguage,
    languageCookie,
    languageCookieOptions,
    languageHeader,
    negotiatedLanguage,
} from "@/i18n/request-language";
import { publicPath, publicRoute } from "@/lib/public-metadata";

const languageInputs = "Accept-Language, Cookie, CF-IPCountry";

function contentSecurityPolicy(nonce: string, development: boolean) {
    return [
        "default-src 'self'",
        `script-src 'self' 'nonce-${nonce}' 'strict-dynamic'${development ? " 'unsafe-eval'" : ""}`,
        "style-src 'self'",
        `style-src-elem 'self' ${development ? "'unsafe-inline'" : `'nonce-${nonce}'`}`,
        "style-src-attr 'unsafe-inline'",
        "img-src 'self' data: blob:",
        "font-src 'self'",
        `connect-src 'self'${development ? " ws:" : ""}`,
        "worker-src 'self'",
        "object-src 'none'",
        "base-uri 'none'",
        "form-action 'self'",
        "frame-ancestors 'none'",
        "report-uri /api/csp-reports/",
    ].join("; ");
}

function languageRedirect(request: NextRequest, path: string, search: string) {
    const response = NextResponse.redirect(
        new URL(`${path}${search}`, request.url),
        307,
    );
    response.headers.set("Cache-Control", "private, no-store");
    response.headers.set("Vary", languageInputs);
    return response;
}

function withoutChoice(search: string) {
    const query = new URLSearchParams(search);
    query.delete("language");
    return query.size ? `?${query}` : "";
}

export function proxy(request: NextRequest) {
    const nonce = Buffer.from(crypto.randomUUID()).toString("base64");
    const development = process.env.NODE_ENV === "development";
    const policy = contentSecurityPolicy(nonce, development);
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
    const choice = requested.searchParams.get("language");
    if (route && isLanguage(choice)) {
        const response = languageRedirect(
            request,
            publicPath(choice, route.page),
            withoutChoice(requested.search),
        );
        response.cookies.set(
            languageCookie,
            choice,
            languageCookieOptions(!development),
        );
        return response;
    }
    const language = negotiatedLanguage(
        request.cookies.get(languageCookie)?.value,
        request.headers.get("accept-language") ?? "",
        request.headers.get("cf-ipcountry"),
    );
    if (route?.language === defaultLanguage && language !== defaultLanguage)
        return languageRedirect(
            request,
            publicPath(language, route.page),
            requested.search,
        );
    const headers = new Headers(request.headers);
    headers.set(languageHeader, route?.language ?? language);
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
        "/((?!api/|admin(?:/|$)|static/|_next/static|_next/image|favicon.ico|robots\\.txt$|sitemap\\.xml$|icon\\.svg$|(?:tr/)?opengraph-image|identity/|app/(?:manifest\\.webmanifest|sw\\.js|icons/(?:(?:any|maskable)-(?:192|512)|apple-touch-icon)\\.png)$).*)",
    ],
};
