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

function page(route: string, files: string[]) {
    const folder = `server/app${route}/page`;
    return {
        [`${folder}/build-manifest.json`]: JSON.stringify({
            polyfillFiles: ["static/chunks/polyfill.js"],
            rootMainFiles: ["static/chunks/main.js"],
        }),
        [`${folder}_client-reference-manifest.js`]: manifest(`${route}/page`, {
            entryJSFiles: { [`[project]/src/app${route}/page`]: files },
        }),
    };
}

const sized = (content: string, size: number) => content.padEnd(size, " ");

test("each chunk counts once: in a first load, in the entry of the export it registers or in the rest", () => {
    const dist = buildOutput({
        ...page("", ["static/chunks/home.js"]),
        ...page("/app", ["static/chunks/app.js", "static/chunks/shared.js"]),
        "static/chunks/polyfill.js": "p".repeat(1000),
        "static/chunks/main.js": "m".repeat(100),
        "static/chunks/home.js": "h".repeat(5),
        "static/chunks/app.js": sized('t.s(["Settings",0]);', 20),
        "static/chunks/shared.js": "s".repeat(3),
        "static/chunks/calibration.js": sized('e.s(["Calibrations",0,c]);', 40),
        "static/chunks/outages.js": "o".repeat(7),
        "static/chunks/catalogue.js": sized(
            `JSON.parse('{"Calibrations":{}}')`,
            60,
        ),
        "static/chunks/nested/style.css": "n".repeat(2),
        "static/chunks/notes.txt": "x".repeat(500),
    });
    try {
        expect(
            measure(dist, {
                tolerancePercent: 10,
                firstLoad: { "/app": 120 },
                onDemand: { Calibrations: 40 },
                otherOnDemand: 60,
            }),
        ).toEqual([
            { name: "/app", bytes: 123, limit: 132 },
            { name: "Calibrations", bytes: 40, limit: 44 },
            { name: "other on-demand chunks", bytes: 69, limit: 66 },
        ]);
        expect(() =>
            measure(dist, {
                tolerancePercent: 1,
                firstLoad: {},
                onDemand: { Settings: 1 },
                otherOnDemand: 0,
            }),
        ).toThrow("Expected one on-demand chunk exporting Settings, found 0.");
    } finally {
        rmSync(dist, { recursive: true, force: true });
    }
});

const onDemandOnly = (chunks: Record<string, string>) =>
    buildOutput({
        ...page("/app", ["static/chunks/app.js"]),
        "static/chunks/polyfill.js": "p",
        "static/chunks/main.js": "m",
        "static/chunks/app.js": "a",
        ...Object.fromEntries(
            Object.entries(chunks).map(([name, content]) => [
                `static/chunks/${name}`,
                content,
            ]),
        ),
    });

const entries = (onDemand: Record<string, number>) => ({
    tolerancePercent: 1,
    firstLoad: {},
    onDemand,
    otherOnDemand: 0,
});

test("an on-demand entry is found by its exact name at any position of its chunk's registration", () => {
    const dist = onDemandOnly({
        "section.js": sized('e.s(["helper",0,h,"Calibrations",0,c]);', 50),
        "panel.js": sized('e.s(["$Panel",0,p]);', 30),
        "near.js": sized('e.s(["xPanel",0,q,"CalibrationsView",0,v]);', 60),
    });
    try {
        expect(
            measure(dist, entries({ Calibrations: 50, $Panel: 30 })),
        ).toEqual([
            { name: "Calibrations", bytes: 50, limit: 50 },
            { name: "$Panel", bytes: 30, limit: 30 },
            { name: "other on-demand chunks", bytes: 60, limit: 0 },
        ]);
    } finally {
        rmSync(dist, { recursive: true, force: true });
    }
});

test("two chunks registering one export name fail the measurement", () => {
    const dist = onDemandOnly({
        "first.js": 'e.s(["Outages",0,o]);',
        "second.js": 'e.s(["other",0,x,"Outages",0,o]);',
    });
    try {
        expect(() => measure(dist, entries({ Outages: 1 }))).toThrow(
            "Expected one on-demand chunk exporting Outages, found 2.",
        );
    } finally {
        rmSync(dist, { recursive: true, force: true });
    }
});

test("two on-demand entries that resolve to one chunk fail the measurement", () => {
    const dist = onDemandOnly({
        "merged.js": 'e.s(["Calibrations",0,c,"Outages",0,o]);',
    });
    try {
        expect(() =>
            measure(dist, entries({ Calibrations: 1, Outages: 1 })),
        ).toThrow(
            "Outages resolves to static/chunks/merged.js, which Calibrations already counts.",
        );
    } finally {
        rmSync(dist, { recursive: true, force: true });
    }
});
