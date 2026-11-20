import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { expect, test } from "@playwright/test";
import { budgetLimit, routeBytes } from "../testing/bundle-budget";

function buildOutput(files: Record<string, string>) {
    const dist = mkdtempSync(path.join(tmpdir(), "pyracast-budget-"));
    for (const [name, content] of Object.entries(files)) {
        mkdirSync(path.dirname(path.join(dist, name)), { recursive: true });
        writeFileSync(path.join(dist, name), content);
    }
    return dist;
}

const manifest = (route: string, value: object) =>
    `globalThis.__RSC_MANIFEST=(globalThis.__RSC_MANIFEST||{});globalThis.__RSC_MANIFEST[${JSON.stringify(route)}]=${JSON.stringify(value)};`;

test("the route bytes count every script and stylesheet the browser loads once", () => {
    const dist = buildOutput({
        "server/app/app/page/build-manifest.json": JSON.stringify({
            polyfillFiles: ["static/chunks/polyfill.js"],
            rootMainFiles: ["static/chunks/main.js"],
        }),
        "server/app/app/page_client-reference-manifest.js": manifest(
            "/app/page",
            {
                entryJSFiles: {
                    "[project]/src/app/app/layout": ["static/chunks/shared.js"],
                    "[project]/src/app/app/page": [
                        "static/chunks/shared.js",
                        "static/chunks/page.js",
                    ],
                    "[project]/src/app/opengraph-image--metadata": [
                        "static/chunks/image.js",
                    ],
                },
                entryCSSFiles: {
                    "[project]/src/app/layout": [
                        { path: "static/chunks/site.css", inlined: false },
                    ],
                },
            },
        ),
        "static/chunks/polyfill.js": "p".repeat(1000),
        "static/chunks/main.js": "m".repeat(100),
        "static/chunks/shared.js": "s".repeat(20),
        "static/chunks/page.js": "a".repeat(3),
        "static/chunks/image.js": "i".repeat(1000),
        "static/chunks/site.css": "c".repeat(7),
    });
    try {
        expect(routeBytes(dist, "/app")).toBe(130);
    } finally {
        rmSync(dist, { recursive: true, force: true });
    }
});

test("a route missing from the build output fails the measurement", () => {
    const dist = buildOutput({});
    try {
        expect(() => routeBytes(dist, "/app")).toThrow();
    } finally {
        rmSync(dist, { recursive: true, force: true });
    }
});

test("the budget allows the baseline plus five percent", () => {
    expect(budgetLimit(200_000)).toBe(210_000);
    expect(budgetLimit(100_001)).toBe(105_001);
});
