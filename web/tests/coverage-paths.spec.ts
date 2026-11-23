import { expect, test } from "@playwright/test";
import { spawnSync } from "node:child_process";
import {
    copyFileSync,
    existsSync,
    mkdirSync,
    mkdtempSync,
    readFileSync,
    rmSync,
    writeFileSync,
} from "node:fs";
import os from "node:os";
import path from "node:path";
import { pathToFileURL } from "node:url";
import { CoverageReport } from "monocart-coverage-reports";
import { config as proxyConfig } from "@/proxy";
import {
    combineCoverage,
    withoutInventedStatements,
    type CoverageMap,
    type FileCoverage,
} from "../testing/coverage-combine";
import {
    authoredSource,
    browserCoverage,
    browserEntry,
    serverEntry,
    serverOutput,
    sourcePath,
    withResolvedSources,
} from "../testing/coverage-options";
import CoverageReporter from "../testing/coverage-reporter";
import { ingestServerSnapshots } from "../testing/server-coverage";
import { coverageOrigin, coverageReadinessPath } from "./support/server";

const wholeScript = (source: string, count = 1) => [
    {
        functionName: "",
        isBlockCoverage: true,
        ranges: [{ startOffset: 0, endOffset: source.length, count }],
    },
];

function lcovRecords(lcov: string): Map<string, string> {
    const records = new Map<string, string>();
    for (const record of lcov.split("end_of_record")) {
        const file = /^SF:(.+)$/m.exec(record)?.[1];
        if (file) records.set(file.trim(), record);
    }
    return records;
}

async function inCheckout(
    files: Record<string, string>,
    run: (web: string) => Promise<void> | void,
): Promise<void> {
    const original = process.cwd();
    const distDir = process.env.NEXT_DIST_DIR;
    const temporary = mkdtempSync(path.join(os.tmpdir(), "pyracast-cov-"));
    try {
        const web = path.join(temporary, "web");
        mkdirSync(web);
        for (const [file, text] of Object.entries(files)) {
            mkdirSync(path.dirname(path.join(web, file)), { recursive: true });
            writeFileSync(path.join(web, file), text);
        }
        process.chdir(web);
        process.env.NEXT_DIST_DIR = ".next";
        await run(web);
    } finally {
        process.chdir(original);
        if (distDir === undefined) delete process.env.NEXT_DIST_DIR;
        else process.env.NEXT_DIST_DIR = distDir;
        rmSync(temporary, { recursive: true, force: true });
    }
}

const span = (line: number, column: number, endLine: number, end: number) => ({
    start: { line, column },
    end: { line: endLine, column: end },
});

function file(
    name: string,
    statements: [ReturnType<typeof span>, number][],
    functions: number[] = [],
    branches: number[][] = [],
): FileCoverage {
    return {
        path: name,
        statementMap: Object.fromEntries(statements.map(([s], i) => [i, s])),
        s: Object.fromEntries(statements.map(([, count], i) => [i, count])),
        fnMap: Object.fromEntries(
            functions.map((_, i) => [i, { name: `f${i}` }]),
        ),
        f: Object.fromEntries(functions.map((count, i) => [i, count])),
        branchMap: Object.fromEntries(
            branches.map((_, i) => [i, { type: "if" }]),
        ),
        b: Object.fromEntries(branches.map((arms, i) => [i, arms])),
    };
}

function lineHits(coverage: FileCoverage): Record<number, number> {
    const lines: Record<number, number> = {};
    for (const [key, location] of Object.entries(coverage.statementMap))
        lines[location.start.line] = Math.max(
            lines[location.start.line] ?? 0,
            coverage.s[key] ?? 0,
        );
    return lines;
}

test("coverage identifies only authored sources from known path forms", () => {
    const source = path.resolve("src", "lib", "api.ts");
    for (const candidate of [
        source,
        source.replaceAll("/", "\\"),
        pathToFileURL(source).href,
        "web/src/lib/api.ts",
        "turbopack:///[project]/src/lib/api.ts",
    ]) {
        expect(sourcePath(candidate), candidate).toBe("web/src/lib/api.ts");
        expect(authoredSource(candidate), candidate).toBe(true);
    }
    for (const candidate of [
        path.resolve("vendor/src/lib/api.ts"),
        "C:/src/pyracast/vendor/src/lib/api.ts",
        "turbopack:///[project]/node_modules/next/src/lib/api.ts",
        "webpack://other/src/lib/api.ts",
        "src/lib/api.ts",
        "../../../../src/lib/api.ts",
        "turbopack/[project]/src/lib/api.ts",
        "web/src/../vendor/api.ts",
        "src/lib/schema.d.ts",
    ]) {
        expect(authoredSource(candidate), candidate).toBe(false);
    }
    expect(
        sourcePath("web/src/lib/api.ts", { url: "../../src/lib/api.ts" }),
    ).toBe("../../src/lib/api.ts");
    expect(sourcePath("lib/api.ts", { url: pathToFileURL(source).href })).toBe(
        "web/src/lib/api.ts",
    );
});

test("absolute checkout roots containing src normalize from the actual root", () => {
    const original = process.cwd();
    const temporary = mkdtempSync(path.join(os.tmpdir(), "pyracast-paths-"));
    try {
        const checkout = path.join(temporary, "src", "pyracast", "web");
        mkdirSync(checkout, { recursive: true });
        process.chdir(checkout);
        const expected = path.resolve("src", "lib", "api.ts");
        expect(sourcePath(expected)).toBe("web/src/lib/api.ts");
        expect(authoredSource(path.resolve("vendor/src/lib/api.ts"))).toBe(
            false,
        );
    } finally {
        process.chdir(original);
        rmSync(temporary, { recursive: true, force: true });
    }
});

test("coverage measures this run's browser chunks and the Next server bundles only", () => {
    const server = serverOutput();
    const lookAlike = pathToFileURL(
        path.resolve("node_modules/demo/_next/static/fake.js"),
    ).href;
    expect(browserEntry(`${coverageOrigin}/_next/static/chunks/app.js`)).toBe(
        true,
    );
    for (const url of [
        "http://127.0.0.1:3100/_next/static/chunks/app.js",
        "https://example.com/_next/static/chunks/app.js",
        `${coverageOrigin}/app/_next/static/chunks/app.js`,
        lookAlike,
        "",
    ]) {
        expect(browserEntry(url), url).toBe(false);
    }
    for (const url of [
        pathToFileURL(path.join(server, "chunks", "ssr", "page.js")).href,
        pathToFileURL(path.join(server, "app", "layout.js")).href,
    ]) {
        expect(serverEntry(url), url).toBe(true);
    }
    for (const url of [
        lookAlike,
        `${coverageOrigin}/_next/static/chunks/app.js`,
        pathToFileURL(path.resolve("node_modules/next/dist/server/next.js"))
            .href,
        pathToFileURL(`${server}-other${path.sep}page.js`).href,
        "node:internal/main/run_main_module",
        "file://remote-host/page.js",
        "",
    ]) {
        expect(serverEntry(url), url).toBe(false);
    }
});

test("map sources resolve against the map's own location", () => {
    const mapUrl = "file:///checkout/web/.next/server/chunks/ssr/page.js.map";
    expect(
        withResolvedSources(
            {
                sources: [
                    "../../../../src/app/layout.tsx",
                    "../../../../../src/app/layout.tsx",
                    "turbopack:///[project]/src/lib/api.ts",
                ],
            },
            mapUrl,
        ).sources,
    ).toEqual([
        "file:///checkout/web/src/app/layout.tsx",
        "file:///checkout/src/app/layout.tsx",
        "turbopack:///[project]/src/lib/api.ts",
    ]);
    expect(
        withResolvedSources(
            { sourceRoot: "../../../..", sources: ["src/app/layout.tsx"] },
            mapUrl,
        ),
    ).toEqual({ sources: ["file:///checkout/web/src/app/layout.tsx"] });
});

test("the coverage server is ready without running authored server code", () => {
    expect(existsSync(path.join("public", coverageReadinessPath))).toBe(true);
    const [matcher] = proxyConfig.matcher;
    expect(new RegExp(`^${matcher}$`).test(coverageReadinessPath)).toBe(false);
    expect(new RegExp(`^${matcher}$`).test("/app/")).toBe(true);
});

test("a source both sides ran keeps the browser's functions and branches and gains the server's lines", () => {
    const name = "web/src/lib/shared.ts";
    const browser = file(name, [[span(1, 0, 1, 9), 3]], [3], [[3, 0]]);
    const server = file(
        name,
        [
            [span(1, 0, 1, 9), 2],
            [span(2, 0, 2, 9), 1],
        ],
        [2, 1],
        [
            [0, 2],
            [1, 0],
        ],
    );
    const baseline = file(name, [[span(1, 0, 1, 9), 0]], [0], [[0, 0]]);
    const combined = combineCoverage({
        browser: { [name]: browser },
        server: { [name]: server },
        baseline: { [name]: baseline },
    })[name]!;
    expect(combined.f).toEqual(browser.f);
    expect(combined.fnMap).toEqual(browser.fnMap);
    expect(combined.b).toEqual(browser.b);
    expect(combined.branchMap).toEqual(browser.branchMap);
    expect(lineHits(combined)).toEqual({ 1: 5, 2: 1 });
    const swapped = combineCoverage({
        browser: { [name]: server },
        server: { [name]: browser },
        baseline: { [name]: baseline },
    })[name]!;
    expect(lineHits(swapped)).toEqual(lineHits(combined));
    expect(swapped.b).toEqual(server.b);
});

test("a source only one side ran takes that side's record and a source nobody ran keeps the zero baseline", () => {
    const names = [
        "web/src/a.ts",
        "web/src/b.ts",
        "web/src/c.ts",
        "web/src/d.ts",
    ];
    const ran = (name: string) =>
        file(name, [[span(1, 0, 1, 5), 1]], [1], [[1, 0]]);
    const idle = (name: string) =>
        file(name, [[span(1, 0, 1, 7), 0]], [0], [[0, 0, 0]]);
    const baseline: CoverageMap = Object.fromEntries(
        names.map((name) => [
            name,
            file(name, [[span(1, 0, 1, 6), 1]], [1], [[1, 0]]),
        ]),
    );
    const browser: CoverageMap = {
        "web/src/a.ts": ran("web/src/a.ts"),
        "web/src/b.ts": idle("web/src/b.ts"),
        "web/src/d.ts": idle("web/src/d.ts"),
    };
    const server: CoverageMap = {
        "web/src/b.ts": ran("web/src/b.ts"),
        "web/src/c.ts": ran("web/src/c.ts"),
        "web/src/d.ts": idle("web/src/d.ts"),
    };
    const combined = combineCoverage({ browser, server, baseline });
    expect(Object.keys(combined)).toEqual(names);
    expect(combined["web/src/a.ts"]!.b).toEqual(browser["web/src/a.ts"]!.b);
    expect(combined["web/src/b.ts"]!.b).toEqual(server["web/src/b.ts"]!.b);
    expect(lineHits(combined["web/src/b.ts"]!)).toEqual({ 1: 1 });
    expect(combined["web/src/c.ts"]).toEqual(server["web/src/c.ts"]);
    expect(combined["web/src/d.ts"]).toEqual(
        file("web/src/d.ts", [[span(1, 0, 1, 6), 0]], [0], [[0, 0]]),
    );
});

test("the converter's invented statement over a whole source is not an execution", () => {
    const text = "export const a = 1;\nexport const b = 2;\n";
    const invented = file("web/src/a.ts", [[span(1, 0, 3, 0), 1]]);
    expect(withoutInventedStatements(invented, text).s).toEqual({});
    const real = file("web/src/a.ts", [
        [span(1, 0, 3, 0), 1],
        [span(2, 0, 2, 19), 4],
    ]);
    expect(lineHits(withoutInventedStatements(real, text))).toEqual({ 2: 4 });
});

const chunk =
    "globalThis.layout = 1;\nglobalThis.shared = 1;\nglobalThis.outside = 1;\n//# sourceMappingURL=page.js.map\n";
const authored = {
    "src/app/layout.tsx": "\uFEFFexport const layout = 1;\n",
    "src/app/page.tsx": "export const page = 1;\n",
    "src/lib/shared.ts": "export const shared = 1;\n",
    "src/lib/unmapped.ts": "export const unmapped = 1;\n",
    "src/lib/types.ts": "export type Shape = { id: number };\n",
    "coverage-floor.json": '{ "lines": 0, "branches": 0, "functions": 0 }',
};

function serverBuild(web: string): string {
    const chunkDir = path.join(serverOutput(), "chunks", "ssr");
    mkdirSync(chunkDir, { recursive: true });
    writeFileSync(path.join(chunkDir, "page.js"), chunk);
    writeFileSync(
        path.join(chunkDir, "page.js.map"),
        JSON.stringify({
            version: 3,
            sources: [
                "../../../../src/app/layout.tsx",
                "../../../../src/lib/shared.ts",
                "../../../../src/lib/unmapped.ts",
                "../../../../../src/app/page.tsx",
            ],
            sourcesContent: [
                authored["src/app/layout.tsx"].slice(1),
                authored["src/lib/shared.ts"],
                authored["src/lib/unmapped.ts"],
                authored["src/app/page.tsx"],
            ],
            names: [],
            mappings: "AAAA;ACAA;AEAA",
        }),
    );
    const snapshots = path.join(web, "coverage", "v8");
    mkdirSync(snapshots, { recursive: true });
    writeFileSync(
        path.join(snapshots, "coverage-1-1-0.json"),
        JSON.stringify({
            result: [
                {
                    scriptId: "1",
                    url: pathToFileURL(path.join(chunkDir, "page.js")).href,
                    functions: wholeScript(chunk),
                },
                {
                    scriptId: "2",
                    url: pathToFileURL(
                        path.resolve("node_modules/next/dist/server/next.js"),
                    ).href,
                    functions: wholeScript("x"),
                },
            ],
        }),
    );
    return snapshots;
}

async function quietly(run: () => Promise<void>): Promise<void> {
    const log = console.log;
    console.log = () => undefined;
    try {
        await run();
    } finally {
        console.log = log;
    }
}

async function browserRun(text = authored["src/lib/shared.ts"]) {
    await new CoverageReport(browserCoverage()).add([
        {
            url: `${coverageOrigin}/_next/static/chunks/shared.js`,
            source: text,
            sourceMap: {
                version: 3,
                sources: ["turbopack:///[project]/src/lib/shared.ts"],
                sourcesContent: [text],
                names: [],
                mappings: "AAAA",
            },
            functions: wholeScript(text),
        },
    ]);
}

test("the report keeps each side's mapped execution and the baseline for what no mapping ran", async () => {
    await inCheckout(authored, async (web) => {
        await ingestServerSnapshots(serverBuild(web), new Set());
        await browserRun();
        await quietly(() => new CoverageReporter().onEnd());
        const records = lcovRecords(readFileSync("coverage/lcov.info", "utf8"));
        expect([...records.keys()].sort()).toEqual([
            "web/src/app/layout.tsx",
            "web/src/app/page.tsx",
            "web/src/lib/shared.ts",
            "web/src/lib/types.ts",
            "web/src/lib/unmapped.ts",
        ]);
        expect(records.get("web/src/app/layout.tsx")).toMatch(/^DA:1,1$/m);
        expect(records.get("web/src/lib/shared.ts")).toMatch(/^DA:1,[12]$/m);
        for (const idle of [
            "web/src/app/page.tsx",
            "web/src/lib/types.ts",
            "web/src/lib/unmapped.ts",
        ]) {
            expect(records.get(idle)).toMatch(/^DA:1,0$/m);
            expect(records.get(idle)).not.toMatch(/^DA:\d+,[1-9]/m);
        }
    });
});

test("a run whose server coverage maps no execution fails although the browser's is healthy", async () => {
    await inCheckout(authored, async () => {
        await browserRun();
        await expect(
            quietly(() => new CoverageReporter().onEnd()),
        ).rejects.toThrow(
            "The Next server coverage maps no execution to an authored source",
        );
    });
});

test("inputs that name an authored source with another text fail the run in either order", async () => {
    for (const order of [
        ["export const shared = 1;\n", "export const shared = 1;\r\n"],
        ["export const shared = 1;\r\n", "export const shared = 1;\n"],
    ]) {
        await inCheckout(authored, async () => {
            for (const text of order) await browserRun(text);
            await expect(
                new CoverageReport(browserCoverage()).generate(),
            ).rejects.toThrow("web/src/lib/shared.ts");
        });
    }
});

test("server coverage needs a new snapshot with an entry of the server's build output", async () => {
    await inCheckout(authored, async (web) => {
        const snapshots = serverBuild(web);
        await expect(
            ingestServerSnapshots(snapshots, new Set(["coverage-1-1-0.json"])),
        ).rejects.toThrow("wrote no coverage snapshot");
        writeFileSync(
            path.join(snapshots, "coverage-1-1-0.json"),
            JSON.stringify({
                result: [
                    {
                        scriptId: "2",
                        url: pathToFileURL(
                            path.resolve(
                                "node_modules/next/dist/server/next.js",
                            ),
                        ).href,
                        functions: wholeScript("x"),
                    },
                ],
            }),
        );
        await expect(
            ingestServerSnapshots(snapshots, new Set()),
        ).rejects.toThrow("No entry of the Next server's coverage");
    });
});

const wrapper = path.resolve("testing/run-coverage.mjs");

function wrapperRun(web: string, args: string[]) {
    mkdirSync(path.join(web, "testing"), { recursive: true });
    copyFileSync(wrapper, path.join(web, "testing/run-coverage.mjs"));
    for (const directory of ["coverage", ".next-coverage"]) {
        mkdirSync(path.join(web, directory), { recursive: true });
        writeFileSync(path.join(web, directory, "kept"), "");
    }
    const result = spawnSync(
        process.execPath,
        [path.join(web, "testing/run-coverage.mjs"), ...args],
        { encoding: "utf8" },
    );
    for (const directory of ["coverage", ".next-coverage"])
        expect(existsSync(path.join(web, directory, "kept"))).toBe(true);
    return result;
}

test("the coverage wrapper refuses UI mode before it deletes or builds anything", async () => {
    await inCheckout({}, (web) => {
        for (const args of [
            ["--ui"],
            ["--ui-port=9000"],
            ["tests/a.spec.ts", "--ui"],
        ]) {
            const result = wrapperRun(web, args);
            expect(result.status).toBe(2);
            expect(result.stderr).toContain("UI mode");
        }
    });
});

test("the coverage wrapper refuses while another coverage run holds the checkout", async () => {
    await inCheckout({ "coverage.lock": String(process.pid) }, (web) => {
        const result = wrapperRun(web, []);
        expect(result.status).toBe(2);
        expect(result.stderr).toContain("holds this checkout");
        expect(readFileSync(path.join(web, "coverage.lock"), "utf8")).toBe(
            String(process.pid),
        );
    });
});
