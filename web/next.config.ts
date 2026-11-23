import type { NextConfig } from "next";
import { PHASE_DEVELOPMENT_SERVER } from "next/constants";
import createNextIntlPlugin from "next-intl/plugin";

const coverageBuild = process.env.PLAYWRIGHT_COVERAGE === "1";

const config = (phase: string): NextConfig => ({
    output: "standalone",
    distDir: process.env.NEXT_DIST_DIR ?? ".next",
    productionBrowserSourceMaps: coverageBuild,
    // The minifier shapes one source differently per chunk, so the measured branches would follow unrelated changes.
    ...(coverageBuild && { experimental: { turbopackMinify: false } }),
    poweredByHeader: false,
    trailingSlash: true,
    skipTrailingSlashRedirect: true,
    headers() {
        return Promise.resolve([
            {
                source: "/:path*",
                // The gateway sets nosniff, HSTS and the permissions and opener policies.
                headers: [
                    { key: "Referrer-Policy", value: "same-origin" },
                    { key: "X-Frame-Options", value: "DENY" },
                ],
            },
            {
                source: "/app/:file([^/]+\\.webmanifest)",
                headers: [{ key: "Cache-Control", value: "no-cache" }],
            },
            {
                source: "/app/icons/:file([^/]+\\.png)",
                headers: [{ key: "Cache-Control", value: "no-cache" }],
            },
            {
                source: "/app/sw.js",
                headers: [
                    { key: "Content-Type", value: "text/javascript" },
                    { key: "Cache-Control", value: "no-store" },
                    {
                        key: "Content-Security-Policy",
                        value: "default-src 'none'; connect-src 'self'",
                    },
                ],
            },
        ]);
    },
    redirects() {
        return Promise.resolve([
            {
                source: "/app/:file(manifest\\.webmanifest|sw\\.js|icons/(?:any|maskable)-(?:192|512)\\.png|icons/apple-touch-icon\\.png)/",
                destination: "/app/:file",
                permanent: true,
            },
        ]);
    },
    rewrites() {
        if (phase !== PHASE_DEVELOPMENT_SERVER) return Promise.resolve([]);
        return Promise.resolve([
            {
                source: "/admin/:path*/",
                destination: `${process.env.DJANGO_API_ORIGIN ?? "http://127.0.0.1:8000"}/admin/:path*/`,
            },
            {
                source: "/static/:path*",
                destination: `${process.env.DJANGO_API_ORIGIN ?? "http://127.0.0.1:8000"}/static/:path*`,
            },
            {
                source: "/api/auth/:path*",
                destination: `${process.env.DJANGO_API_ORIGIN ?? "http://127.0.0.1:8000"}/api/auth/:path*`,
            },
            {
                source: "/api/:path*/",
                destination: `${process.env.DJANGO_API_ORIGIN ?? "http://127.0.0.1:8000"}/api/:path*/`,
            },
        ]);
    },
});
const withNextIntl = createNextIntlPlugin();
export default (phase: string) => withNextIntl(config(phase));
