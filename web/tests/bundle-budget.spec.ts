import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { expect, test } from "@playwright/test";
import { budgetLimit, measure, routeBytes } from "../testing/bundle-budget";

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

test("the limit is the recorded size plus the stated tolerance", () => {
    expect(budgetLimit(200_000, 1)).toBe(202_000);
    expect(budgetLimit(100_001, 1)).toBe(101_001);
    expect(budgetLimit(100_001, 0)).toBe(100_001);
});

function page(route: string, files: string[], loadable: object = {}) {
    const folder = `server/app${route}/page`;
    return {
        [`${folder}/build-manifest.json`]: JSON.stringify({
            polyfillFiles: ["static/chunks/polyfill.js"],
            rootMainFiles: ["static/chunks/main.js"],
        }),
        [`${folder}_client-reference-manifest.js`]: manifest(`${route}/page`, {
            entryJSFiles: { [`[project]/src/app${route}/page`]: files },
        }),
        [`${folder}/react-loadable-manifest.json`]: JSON.stringify(loadable),
    };
}

test("each chunk counts once: in a first load, in its named on-demand entry or in the rest", () => {
    const dist = buildOutput({
        ...page("", ["static/chunks/home.js"]),
        ...page("/app", ["static/chunks/app.js", "static/chunks/shared.js"], {
            "plant.tsx -> ./calibrations": {
                id: 1,
                files: [
                    "static/chunks/shared.js",
                    "static/chunks/calibration.js",
                ],
            },
            "plant.tsx -> ./outages": {
                id: 2,
                files: ["static/chunks/outages.js"],
            },
        }),
        "static/chunks/polyfill.js": "p".repeat(1000),
        "static/chunks/main.js": "m".repeat(100),
        "static/chunks/home.js": "h".repeat(5),
        "static/chunks/app.js": "a".repeat(20),
        "static/chunks/shared.js": "s".repeat(3),
        "static/chunks/calibration.js": "c".repeat(40),
        "static/chunks/outages.js": "o".repeat(7),
        "static/chunks/catalogue.js": "t".repeat(60),
        "static/chunks/nested/style.css": "n".repeat(2),
        "static/chunks/notes.txt": "x".repeat(500),
    });
    try {
        expect(
            measure(dist, {
                tolerancePercent: 10,
                firstLoad: { "/app": 120 },
                onDemand: { "plant.tsx -> ./calibrations": 40 },
                otherOnDemand: 60,
            }),
        ).toEqual([
            { name: "/app", bytes: 123, limit: 132 },
            { name: "plant.tsx -> ./calibrations", bytes: 40, limit: 44 },
            { name: "other on-demand chunks", bytes: 69, limit: 66 },
        ]);
        expect(() =>
            measure(dist, {
                tolerancePercent: 1,
                firstLoad: {},
                onDemand: { "plant.tsx -> ./settings": 1 },
                otherOnDemand: 0,
            }),
        ).toThrow("No on-demand chunk for plant.tsx -> ./settings.");
    } finally {
        rmSync(dist, { recursive: true, force: true });
    }
});
