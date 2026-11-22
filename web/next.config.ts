import type { NextConfig } from "next";
import { PHASE_DEVELOPMENT_SERVER } from "next/constants";
import createNextIntlPlugin from "next-intl/plugin";

const config = (phase: string): NextConfig => ({
    output: "standalone",
    distDir: process.env.NEXT_DIST_DIR ?? ".next",
    productionBrowserSourceMaps: process.env.PLAYWRIGHT_COVERAGE === "1",
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
                source: "/app/:file([^/]+\\.(?:webmanifest|png))",
                headers: [{ key: "Cache-Control", value: "no-cache" }],
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
