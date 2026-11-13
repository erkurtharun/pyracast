import type { NextConfig } from "next";
import { PHASE_DEVELOPMENT_SERVER } from "next/constants";

const config = (phase: string): NextConfig => ({
    output: "standalone",
    distDir: process.env.NEXT_DIST_DIR ?? ".next",
    productionBrowserSourceMaps: process.env.PLAYWRIGHT_COVERAGE === "1",
    poweredByHeader: false,
    trailingSlash: true,
    skipTrailingSlashRedirect: true,
    async headers() {
        return [
            {
                source: "/:path*",
                headers: [
                    { key: "X-Content-Type-Options", value: "nosniff" },
                    {
                        key: "Referrer-Policy",
                        value: "strict-origin-when-cross-origin",
                    },
                    { key: "X-Frame-Options", value: "DENY" },
                    {
                        key: "Permissions-Policy",
                        value: "camera=(), microphone=(), geolocation=()",
                    },
                ],
            },
        ];
    },
    async rewrites() {
        if (phase !== PHASE_DEVELOPMENT_SERVER) return [];
        return [
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
        ];
    },
});
export default config;
