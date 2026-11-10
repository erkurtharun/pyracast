import type { NextConfig } from "next";
const config: NextConfig = {
    distDir: process.env.NEXT_DIST_DIR ?? ".next",
    poweredByHeader: false,
    trailingSlash: true,
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
        return [
            {
                source: "/api/:path*/",
                destination: `${process.env.DJANGO_API_ORIGIN ?? "http://127.0.0.1:8000"}/api/:path*/`,
            },
        ];
    },
};
export default config;
