import { expect, test } from "@playwright/test";
import {
    PHASE_DEVELOPMENT_SERVER,
    PHASE_PRODUCTION_BUILD,
    PHASE_PRODUCTION_SERVER,
} from "next/constants";
import config from "../next.config";

test("only the development server proxies Django pages, assets and APIs", async () => {
    const development = config(PHASE_DEVELOPMENT_SERVER);

    expect(await development.rewrites?.()).toEqual([
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
    expect(development.skipTrailingSlashRedirect).toBe(true);
    for (const phase of [PHASE_PRODUCTION_BUILD, PHASE_PRODUCTION_SERVER]) {
        expect(await config(phase).rewrites?.()).toEqual([]);
    }
});
