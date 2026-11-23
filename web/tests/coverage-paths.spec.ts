import { expect, test } from "@playwright/test";
import { spawn, spawnSync } from "node:child_process";
import {
    copyFileSync,
    existsSync,
    mkdirSync,
    mkdtempSync,
    readFileSync,
    rmSync,
    symlinkSync,
    writeFileSync,
} from "node:fs";
import { createServer, type AddressInfo, type Server } from "node:net";
import os from "node:os";
import path from "node:path";
import { pathToFileURL } from "node:url";
import { CoverageReport } from "monocart-coverage-reports";
import { config as proxyConfig } from "@/proxy";
import {
    branchEvidence,
    byCodeUnit,
    combineCoverage,
    executed,
    type FileCoverage,
    type ScriptRecords,
} from "../testing/coverage-combine";
import {
    addBrowserCoverage,
    authoredEnd,
    preparedEntry,
    serverMap,
} from "../testing/coverage-maps";
import {
    authoredFile,
    browserCoverage,
    browserEntry,
    distDir,
    identityUrl,
    reportKey,
    serverCoverage,
    serverEntry,
    serverOutput,
} from "../testing/coverage-options";
import CoverageReporter, { scriptRecords } from "../testing/coverage-reporter";
import {
    ingestServerSnapshot,
    runSnapshot,
    serverProcess,
} from "../testing/server-coverage";
import {
    coverageOrigin,
    coverageReadinessPath,
    coverageRun,
    serverSnapshots,
    webServer,
} from "./support/server";

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

const lineRecords = (record: string | undefined) =>
    (record ?? "").split("\n").filter((line) => line.startsWith("DA:"));

async function inCheckout(
    files: Record<string, string>,
    run: (web: string) => Promise<void> | void,
): Promise<void> {
    const original = process.cwd();
    const distDirectory = process.env.NEXT_DIST_DIR;
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
        if (distDirectory === undefined) delete process.env.NEXT_DIST_DIR;
        else process.env.NEXT_DIST_DIR = distDirectory;
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
    branches: [number, number][][] = [],
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
            branches.map((arms, i) => [
                i,
                {
                    type: "switch",
                    locations: arms.map(([line]) => span(line, 4, line, 11)),
                },
            ]),
        ),
        b: Object.fromEntries(
            branches.map((arms, i) => [i, arms.map(([, count]) => count)]),
        ),
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

function armHits(coverage: FileCoverage): Record<number, number> {
    const arms: Record<number, number> = {};
    for (const [key, branch] of Object.entries(coverage.branchMap)) {
        const { locations } = branch as {
            locations: ReturnType<typeof span>[];
        };
        locations.forEach((location, arm) => {
            arms[location.start.line] =
                (arms[location.start.line] ?? 0) + coverage.b[key]![arm]!;
        });
    }
    return arms;
}

function only(records: Record<string, FileCoverage> | undefined) {
    const all = Object.values(records ?? {});
    expect(all).toHaveLength(1);
    return all[0]!;
}

const inlineReference = (map: object) =>
    `//# sourceMappingURL=data:application/json;base64,${Buffer.from(JSON.stringify(map)).toString("base64")}`;

const flatMap = (source: string, content: string, mappings = "AAAA") => ({
    version: 3,
    sources: [source],
    sourcesContent: [content],
    names: [],
    mappings,
});

test("map names are authored only as files under the physical src root or Turbopack project sources", () => {
    const source = path.resolve("src", "lib", "api.ts");
    const physical = authoredFile(pathToFileURL(source).href);
    expect(physical).toBeDefined();
    for (const name of [
        pathToFileURL(source).href,
        "turbopack:///[project]/src/lib/api.ts",
    ]) {
        expect(authoredFile(name), name).toBe(physical);
        expect(reportKey(authoredFile(name)!), name).toBe("web/src/lib/api.ts");
    }
    for (const name of [
        "web/src/lib/api.ts",
        "src/lib/api.ts",
        "../../../../src/lib/api.ts",
        source,
        "turbopack/[project]/src/lib/api.ts",
        "turbopack:///[project]/node_modules/next/src/lib/api.ts",
        "turbopack:///[project]/src/../src/lib/missing.ts",
        "webpack://other/src/lib/api.ts",
        pathToFileURL(path.resolve("src", "lib", "schema.d.ts")).href,
        pathToFileURL(path.resolve("src", "lib", "missing.ts")).href,
        pathToFileURL(path.resolve("vendor", "src", "lib", "api.ts")).href,
    ]) {
        expect(authoredFile(name), name).toBeUndefined();
    }
});

test("a checkout reached through a link gives the keys of its physical path", () => {
    const original = process.cwd();
    const temporary = mkdtempSync(path.join(os.tmpdir(), "pyracast-link-"));
    const distDirectory = process.env.NEXT_DIST_DIR;
    try {
        const physical = path.join(temporary, "src", "pyracast", "web");
        const chunk = path.join(physical, ".next", "server", "chunks", "a.js");
        mkdirSync(path.join(physical, "src", "lib"), { recursive: true });
        mkdirSync(path.dirname(chunk), { recursive: true });
        writeFileSync(path.join(physical, "src", "lib", "a.ts"), "a;\n");
        writeFileSync(chunk, "a;\n");
        const link = path.join(temporary, "link");
        symlinkSync(path.dirname(physical), link, "junction");
        process.chdir(path.join(link, "web"));
        process.env.NEXT_DIST_DIR = ".next";
        for (const root of [physical, path.join(link, "web")]) {
            const name = pathToFileURL(
                path.join(root, "src", "lib", "a.ts"),
            ).href;
            expect(reportKey(authoredFile(name)!), name).toBe(
                "web/src/lib/a.ts",
            );
            const script = pathToFileURL(
                path.join(root, ".next", "server", "chunks", "a.js"),
            ).href;
            expect(serverEntry(script), script).toBe(true);
        }
        expect(
            reportKey(authoredFile("turbopack:///[project]/src/lib/a.ts")!),
        ).toBe("web/src/lib/a.ts");
        expect(
            authoredFile(
                pathToFileURL(path.join(temporary, "src", "lib", "a.ts")).href,
            ),
        ).toBeUndefined();
    } finally {
        process.chdir(original);
        if (distDirectory === undefined) delete process.env.NEXT_DIST_DIR;
        else process.env.NEXT_DIST_DIR = distDirectory;
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
        pathToFileURL(path.join(server, "..", "page.js")).href,
        "node:internal/main/run_main_module",
        "file://remote-host/page.js",
        "",
    ]) {
        expect(serverEntry(url), url).toBe(false);
    }
});

test("the coverage server records into this run's directory and is ready without running authored server code", () => {
    const server = webServer("run-1");
    expect(server.url).toBe(`${coverageOrigin}${coverageReadinessPath}`);
    expect(server.env.NODE_V8_COVERAGE).toBe(
        path.resolve("coverage", "v8", "run-1"),
    );
    expect(serverSnapshots("run-1")).toBe(server.env.NODE_V8_COVERAGE);
    expect(existsSync(path.join("public", coverageReadinessPath))).toBe(true);
    const [matcher] = proxyConfig.matcher;
    expect(new RegExp(`^${matcher}$`).test(coverageReadinessPath)).toBe(false);
    expect(new RegExp(`^${matcher}$`).test("/app/")).toBe(true);
});

test("coverage mode refuses to start without the wrapper's run identifier", () => {
    expect(() => coverageRun({ PLAYWRIGHT_COVERAGE: "1" })).toThrow(
        "testing/run-coverage.mjs",
    );
    expect(
        coverageRun({
            PLAYWRIGHT_COVERAGE: "1",
            PLAYWRIGHT_COVERAGE_RUN: "run-1",
        }),
    ).toBe("run-1");
    expect(coverageRun({ PLAYWRIGHT_COVERAGE_RUN: "run-1" })).toBeUndefined();
});

const name = "web/src/lib/pick.ts";

test("compilations whose tables differ are never combined and the first executed one by script is kept", () => {
    const caseOne = (count: number) => [2, count] as [number, number];
    const caseTwo = (count: number) => [4, count] as [number, number];
    const forward = file(
        name,
        [[span(2, 4, 2, 11), 1]],
        [1],
        [[caseOne(1), caseTwo(0)]],
    );
    const reversed = file(
        name,
        [[span(2, 4, 2, 11), 1]],
        [1],
        [[caseTwo(0), caseOne(1)]],
    );
    for (const server of [
        { "file:///a.js": forward, "file:///b.js": reversed },
        { "file:///a.js": reversed, "file:///b.js": forward },
    ]) {
        const combined = combineCoverage({
            sources: [name],
            browser: {},
            server: { [name]: server },
            baseline: [],
        })[name]!;
        expect(armHits(combined)).toEqual({ 2: 1, 4: 0 });
        expect(Object.keys(combined.b)).toHaveLength(1);
    }
    const combined = combineCoverage({
        sources: [name],
        browser: {},
        server: {
            [name]: { "file:///a.js": forward, "file:///b.js": forward },
        },
        baseline: [],
    })[name]!;
    expect(armHits(combined)).toEqual({ 2: 2, 4: 0 });
    expect(combined.f).toEqual({ 0: 2 });
    expect(lineHits(combined)).toEqual({ 2: 2 });
});

test("lines are the union of the baseline and every measured record", () => {
    const baseline: ScriptRecords = {
        [name]: {
            [name]: file(name, [
                [span(1, 0, 1, 9), 0],
                [span(2, 0, 2, 9), 0],
                [span(3, 0, 3, 9), 0],
            ]),
        },
    };
    const combined = combineCoverage({
        sources: [name],
        browser: { [name]: { a: file(name, [[span(1, 0, 1, 9), 3]], [3]) } },
        server: { [name]: { b: file(name, [[span(2, 0, 2, 9), 1]], [1]) } },
        baseline: [baseline],
    })[name]!;
    expect(lineHits(combined)).toEqual({ 1: 3, 2: 1, 3: 0 });
    const functionsOnly = combineCoverage({
        sources: [name],
        browser: { [name]: { a: file(name, [], [1]) } },
        server: {},
        baseline: [baseline],
    })[name]!;
    expect(lineHits(functionsOnly)).toEqual({ 1: 0, 2: 0, 3: 0 });
    expect(functionsOnly.f).toEqual({ 0: 1 });
});

test("a source takes the browser's tables when it ran, else the server's, else the zero baseline", () => {
    const sources = ["web/src/a.ts", "web/src/b.ts", "web/src/c.ts"];
    const ran = (source: string) =>
        file(
            source,
            [[span(1, 0, 1, 5), 1]],
            [1],
            [
                [
                    [1, 1],
                    [2, 0],
                ],
            ],
        );
    const idle = (source: string) =>
        file(source, [[span(1, 0, 1, 7), 0]], [0, 0], [[[1, 0]]]);
    const baseline: ScriptRecords = Object.fromEntries(
        sources.map((source) => [
            source,
            {
                [source]: file(
                    source,
                    [[span(1, 0, 1, 6), 1]],
                    [1],
                    [[[3, 1]]],
                ),
            },
        ]),
    );
    const combined = combineCoverage({
        sources,
        browser: {
            "web/src/a.ts": { x: ran("web/src/a.ts") },
            "web/src/b.ts": { x: idle("web/src/b.ts") },
            "web/src/c.ts": { x: idle("web/src/c.ts") },
        },
        server: {
            "web/src/a.ts": { y: idle("web/src/a.ts") },
            "web/src/b.ts": { y: ran("web/src/b.ts") },
        },
        baseline: [baseline],
    });
    expect(Object.keys(combined)).toEqual(sources);
    expect(combined["web/src/a.ts"]!.b).toEqual({ 0: [1, 0] });
    expect(combined["web/src/b.ts"]!.b).toEqual({ 0: [1, 0] });
    expect(combined["web/src/c.ts"]!.b).toEqual({ 0: [0] });
    expect(combined["web/src/c.ts"]!.f).toEqual({ 0: 0 });
    expect(lineHits(combined["web/src/c.ts"]!)).toEqual({ 1: 0 });
    expect(
        combineCoverage({
            sources: ["web/src/d.ts"],
            browser: {},
            server: {},
            baseline: [],
        })["web/src/d.ts"],
    ).toEqual(file("web/src/d.ts", []));
});

test("when both sides ran a source its functions and branches are the browser's in full, whichever records each side holds", () => {
    const one = file(
        name,
        [[span(1, 0, 1, 9), 2]],
        [2, 0],
        [
            [
                [2, 1],
                [4, 0],
            ],
        ],
    );
    const other = file(
        name,
        [[span(1, 0, 1, 9), 1]],
        [0, 1, 1],
        [
            [
                [2, 0],
                [3, 1],
                [4, 0],
            ],
        ],
    );
    for (const [browser, server] of [
        [one, other],
        [other, one],
    ]) {
        const combined = combineCoverage({
            sources: [name],
            browser: { [name]: { a: browser! } },
            server: { [name]: { b: server! } },
            baseline: [],
        })[name]!;
        const { fnMap, f, branchMap, b } = browser!;
        expect({
            fnMap: combined.fnMap,
            f: combined.f,
            branchMap: combined.branchMap,
            b: combined.b,
        }).toEqual({ fnMap, f, branchMap, b });
    }
});

test("a source no side ran takes the zero tables of the browser build, else the server build, else the TypeScript transpile", () => {
    const [browserBuild, serverBuild, transpile] = [1, 2, 3].map(
        (functions): ScriptRecords => ({
            [name]: {
                [`s${functions}`]: file(
                    name,
                    [[span(1, 0, 1, 5), 0]],
                    Array<number>(functions).fill(0),
                ),
            },
        }),
    );
    const tables = (baseline: ScriptRecords[]) =>
        Object.keys(
            combineCoverage({
                sources: [name],
                browser: {},
                server: {},
                baseline,
            })[name]!.fnMap,
        ).length;
    expect(tables([browserBuild!, serverBuild!, transpile!])).toBe(1);
    expect(tables([{}, serverBuild!, transpile!])).toBe(2);
    expect(tables([{}, {}, transpile!])).toBe(3);
});

test("branch evidence counts the measured groups outside the selected tables of executed sources", () => {
    const selected = file(
        name,
        [[span(1, 0, 1, 5), 1]],
        [1],
        [
            [
                [2, 1],
                [4, 0],
            ],
        ],
    );
    const unselected = file(
        name,
        [[span(1, 0, 1, 5), 1]],
        [1],
        [
            [
                [2, 1],
                [4, 0],
            ],
            [
                [6, 0],
                [7, 1],
            ],
        ],
    );
    const serverOnly = file(
        name,
        [[span(1, 0, 1, 5), 0]],
        [0],
        [
            [
                [2, 0],
                [4, 0],
            ],
            [
                [9, 0],
                [10, 0],
                [11, 0],
            ],
        ],
    );
    const idle = "web/src/lib/idle.ts";
    const sides = {
        browser: { [name]: { a: selected, b: unselected } },
        server: {
            [name]: { c: serverOnly },
            [idle]: {
                c: file(
                    idle,
                    [],
                    [0],
                    [
                        [
                            [1, 0],
                            [2, 0],
                        ],
                    ],
                ),
            },
        },
    };
    const combined = combineCoverage({
        sources: [name, idle],
        ...sides,
        baseline: [],
    });
    expect(combined[name]!.b).toEqual(selected.b);
    expect(branchEvidence(sides, combined)).toEqual({
        groups: 2,
        arms: 5,
        taken: 1,
        sources: 1,
    });
});

const chunk =
    "globalThis.layout = 1;\nglobalThis.shared = 1;\nglobalThis.outside = 1;\n//# sourceMappingURL=page.js.map\n";
const authored = {
    "src/app/layout.tsx": "\u{FEFF}export const layout = 1;\n",
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
    writeFileSync(path.join(chunkDir, "plain.js"), "globalThis.plain = 1;\n");
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
    const snapshot = path.join(web, "coverage", "v8", "coverage-7-1-0.json");
    mkdirSync(path.dirname(snapshot), { recursive: true });
    writeFileSync(
        snapshot,
        JSON.stringify({
            result: [
                ["page.js", chunk],
                ["plain.js", "globalThis.plain = 1;\n"],
            ]
                .map(([script, text], index) => ({
                    scriptId: String(index + 1),
                    url: pathToFileURL(path.join(chunkDir, script!)).href,
                    functions: wholeScript(text!),
                }))
                .concat({
                    scriptId: "9",
                    url: pathToFileURL(
                        path.resolve("node_modules/next/dist/server/next.js"),
                    ).href,
                    functions: wholeScript("x"),
                }),
        }),
    );
    return snapshot;
}

const lineDirective = (map: string) => `//# sourceMappingURL=${map}\n`;

function browserScript(
    script: string,
    text: string,
    map: object,
    count = 1,
    directive = lineDirective,
): { url: string; source: string; functions: unknown[] } {
    const directory = path.join(distDir(), "static", "chunks");
    mkdirSync(directory, { recursive: true });
    const source = `${text}${directive(`${script}.js.map`)}`;
    writeFileSync(path.join(directory, `${script}.js`), source);
    writeFileSync(
        path.join(directory, `${script}.js.map`),
        JSON.stringify({ version: 3, names: [], ...map }),
    );
    return {
        url: `${coverageOrigin}/_next/static/chunks/${script}.js`,
        source,
        functions: wholeScript(source, count),
    };
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

const sharedScript = (
    text = authored["src/lib/shared.ts"],
    generated = "globalThis.shared = 1;\n",
) =>
    browserScript("shared", generated, {
        sources: ["turbopack:///[project]/src/lib/shared.ts"],
        sourcesContent: [text],
        mappings: "AAAA",
    });

test("the report adds each side's mapped execution and keeps the baseline for what no mapping ran", async () => {
    await inCheckout(authored, async (web) => {
        await ingestServerSnapshot(serverBuild(web));
        await addBrowserCoverage([sharedScript()]);
        await quietly(() => new CoverageReporter().onEnd());
        const records = lcovRecords(readFileSync("coverage/lcov.info", "utf8"));
        expect([...records.keys()].sort(byCodeUnit)).toEqual([
            "web/src/app/layout.tsx",
            "web/src/app/page.tsx",
            "web/src/lib/shared.ts",
            "web/src/lib/types.ts",
            "web/src/lib/unmapped.ts",
        ]);
        expect(records.get("web/src/app/layout.tsx")).toMatch(/^DA:1,1$/m);
        expect(records.get("web/src/lib/shared.ts")).toMatch(/^DA:1,2$/m);
        for (const idle of ["web/src/app/page.tsx", "web/src/lib/unmapped.ts"])
            expect(records.get(idle)).toMatch(/^DA:1,0\nLF:1\nLH:0$/m);
        expect(records.get("web/src/lib/types.ts")).not.toMatch(/^DA:/m);
        expect(records.get("web/src/lib/types.ts")).toMatch(/^LF:0$/m);
    });
});

test("the zero baseline comes from the build's own scripts, and the TypeScript transpile only from sources no build script maps", async () => {
    const layout = [
        ...Array.from({ length: 20 }, (_, line) => `// ${line + 1}`),
        "go();",
        "// 22",
        "// 23",
        "wait();",
        "stop();",
        "",
    ].join("\n");
    await inCheckout(
        {
            ...authored,
            "src/app/layout.tsx": layout,
            "src/lib/idle.ts": "idle();\n",
        },
        async (web) => {
            const chunks = path.join(serverOutput(), "chunks", "ssr");
            mkdirSync(chunks, { recursive: true });
            const page = "go();stop();\n//# sourceMappingURL=layout.js.map\n";
            writeFileSync(path.join(chunks, "layout.js"), page);
            writeFileSync(
                path.join(chunks, "layout.js.map"),
                JSON.stringify(
                    flatMap(
                        "../../../../src/app/layout.tsx",
                        layout,
                        "AAoBA,KAIA",
                    ),
                ),
            );
            const snapshot = path.join(
                web,
                "coverage",
                "v8",
                "coverage-7-1-0.json",
            );
            mkdirSync(path.dirname(snapshot), { recursive: true });
            writeFileSync(
                snapshot,
                JSON.stringify({
                    result: [
                        {
                            scriptId: "1",
                            url: pathToFileURL(path.join(chunks, "layout.js"))
                                .href,
                            functions: wholeScript(page),
                        },
                    ],
                }),
            );
            await ingestServerSnapshot(snapshot);
            await addBrowserCoverage([sharedScript()]);
            browserScript("idle", "idle();\n", {
                sources: ["turbopack:///[project]/src/lib/idle.ts"],
                sourcesContent: ["idle();\n"],
                mappings: "AAAA",
            });
            await quietly(() => new CoverageReporter().onEnd());
            const records = lcovRecords(
                readFileSync("coverage/lcov.info", "utf8"),
            );
            expect(lineRecords(records.get("web/src/app/layout.tsx"))).toEqual([
                "DA:21,1",
                "DA:25,1",
            ]);
            expect(lineRecords(records.get("web/src/lib/idle.ts"))).toEqual([
                "DA:1,0",
            ]);
            expect(lineRecords(records.get("web/src/app/page.tsx"))).toEqual([
                "DA:1,0",
            ]);
            expect(records.get("web/src/lib/types.ts")).toMatch(/^LF:0$/m);
        },
    );
});

test("the reporter names every side that maps no execution", async () => {
    await inCheckout(authored, async () => {
        await expect(
            quietly(() => new CoverageReporter().onEnd()),
        ).rejects.toThrow(
            "No mapped execution of an authored source in the coverage of: browser, Next server",
        );
    });
    await inCheckout(authored, async () => {
        await addBrowserCoverage([sharedScript()]);
        await expect(
            quietly(() => new CoverageReporter().onEnd()),
        ).rejects.toThrow(
            "No mapped execution of an authored source in the coverage of: Next server",
        );
    });
});

test("the converter's statement for a source no mapping reaches is removed and a real statement at the line end is kept", async () => {
    for (const text of ["go();\n", "go();"])
        for (const other of ["other();\n", "other();\r"])
            await inCheckout(
                { "src/lib/go.ts": text, "src/lib/other.ts": other },
                async () => {
                    const script = browserScript(
                        "go",
                        "go();\n",
                        {
                            sources: [
                                "turbopack:///[project]/src/lib/go.ts",
                                "turbopack:///[project]/src/lib/other.ts",
                            ],
                            sourcesContent: [text, other],
                            mappings: "AAAA,IAAK",
                        },
                        4,
                    );
                    await addBrowserCoverage([script]);
                    const records = await scriptRecords(browserCoverage());
                    const converted = JSON.parse(
                        readFileSync(
                            "coverage/browser/coverage-final.json",
                            "utf8",
                        ),
                    ) as Record<string, FileCoverage>;
                    const [real, fallback] = ["go", "other"].map(
                        (source) =>
                            Object.entries(converted).find(([key]) =>
                                key.startsWith(`web/src/lib/${source}.ts#`),
                            )![1],
                    );
                    expect(Object.values(real!.statementMap)).toEqual([
                        span(1, 0, 2, 0),
                    ]);
                    expect(Object.values(fallback!.s)).toEqual([1]);
                    const remaining = only(records["web/src/lib/other.ts"]);
                    expect(remaining.s).toEqual({});
                    expect(executed(remaining)).toBe(false);
                    expect(
                        lineHits(only(records["web/src/lib/go.ts"])),
                    ).toEqual({
                        1: 4,
                    });
                },
            );
});

test("a map that places an authored source on a line its text does not have fails preparation", async () => {
    await inCheckout({ "src/lib/go.ts": "go();" }, () => {
        const url = pathToFileURL(
            path.join(serverOutput(), "chunks", "go.js"),
        ).href;
        const source = `go();\n${inlineReference(flatMap("../../../src/lib/go.ts", "go();", "AACA"))}\n`;
        expect(() => preparedEntry({ url, source }, serverMap)).toThrow(
            `Coverage map ${url} of ${url}: it maps web/src/lib/go.ts to line 2`,
        );
    });
});

test("two scripts of one authored source stay two records and add their counts", async () => {
    await inCheckout(
        { "src/lib/shared.ts": authored["src/lib/shared.ts"] },
        async () => {
            const map = {
                sources: ["turbopack:///[project]/src/lib/shared.ts"],
                sourcesContent: [authored["src/lib/shared.ts"]],
                mappings: "AAAA",
            };
            await addBrowserCoverage([
                browserScript("a", "globalThis.shared = 1;\n", map),
                browserScript("b", "globalThis.shared = 1;\n", map, 2),
            ]);
            const records = await scriptRecords(browserCoverage());
            expect(
                Object.keys(records["web/src/lib/shared.ts"]!)
                    .map(identityUrl)
                    .sort(byCodeUnit),
            ).toEqual([
                `${coverageOrigin}/_next/static/chunks/a.js`,
                `${coverageOrigin}/_next/static/chunks/b.js`,
            ]);
            const combined = combineCoverage({
                sources: ["web/src/lib/shared.ts"],
                browser: records,
                server: {},
                baseline: [],
            })["web/src/lib/shared.ts"]!;
            expect(lineHits(combined)).toEqual({ 1: 3 });
        },
    );
});

test("two compilations of a switch in opposite arm order count only the arm each ran, at one script address or two", async () => {
    const lines = [
        "switch (globalThis.x) {",
        "    case 1: globalThis.one = 1; break;",
        "    case 2: globalThis.two = 2; break;",
        "}",
    ];
    const text = `${lines.join("\n")}\n`;
    await inCheckout({ "src/lib/pick.ts": text }, async () => {
        const compilation = (
            order: number[],
            mappings: string,
            url: string,
        ) => {
            const generated = order.map((line) => lines[line]!).join("\n");
            const source = `${generated}\n${inlineReference(flatMap("turbopack:///[project]/src/lib/pick.ts", text, mappings))}\n`;
            const idle = generated.indexOf("case 2");
            return preparedEntry(
                {
                    url,
                    source,
                    functions: [
                        {
                            functionName: "",
                            isBlockCoverage: true,
                            ranges: [
                                {
                                    startOffset: 0,
                                    endOffset: source.length,
                                    count: 1,
                                },
                                {
                                    startOffset: idle,
                                    endOffset: generated.indexOf("\n", idle),
                                    count: 0,
                                },
                            ],
                        },
                    ],
                },
                serverMap,
            )!;
        };
        const url = (script: string) =>
            `${coverageOrigin}/_next/static/chunks/${script}.js`;
        for (const [first, second] of [
            [url("a"), url("a")],
            [url("a"), url("b")],
        ])
            for (const reversedFirst of [false, true]) {
                rmSync("coverage", { recursive: true, force: true });
                const forward = compilation(
                    [0, 1, 2, 3],
                    "AAAA;AACA;AACA;AACA",
                    first!,
                );
                const reversed = compilation(
                    [0, 2, 1, 3],
                    "AAAA;AAEA;AADA;AAEA",
                    second!,
                );
                await new CoverageReport(browserCoverage()).add(
                    reversedFirst ? [reversed, forward] : [forward, reversed],
                );
                const records = await scriptRecords(browserCoverage());
                expect(Object.keys(records[name]!)).toHaveLength(2);
                const combined = combineCoverage({
                    sources: [name],
                    browser: records,
                    server: {},
                    baseline: [],
                })[name]!;
                expect(armHits(combined), `${second} ${reversedFirst}`).toEqual(
                    {
                        2: 1,
                        3: 0,
                    },
                );
            }
    });
});

test("byte-identical scripts at one address with maps of different sources keep each source's own record", async () => {
    const text = "globalThis.a = 1;\n";
    await inCheckout(
        { "src/lib/one.ts": text, "src/lib/two.ts": text },
        async () => {
            const url = `${coverageOrigin}/_next/static/chunks/a.js`;
            const source = `${text}//# sourceMappingURL=a.js.map\n`;
            const entry = (source_: string, count: number) =>
                preparedEntry(
                    { url, source, functions: wholeScript(source, count) },
                    () =>
                        JSON.stringify(
                            flatMap(
                                `turbopack:///[project]/src/lib/${source_}.ts`,
                                text,
                            ),
                        ),
                )!;
            await new CoverageReport(browserCoverage()).add([
                entry("one", 1),
                entry("two", 3),
            ]);
            const records = await scriptRecords(browserCoverage());
            expect(lineHits(only(records["web/src/lib/one.ts"]))).toEqual({
                1: 1,
            });
            expect(lineHits(only(records["web/src/lib/two.ts"]))).toEqual({
                1: 3,
            });
        },
    );
});

test("a browser map whose text is not the authored file fails the run in either order", async () => {
    for (const texts of [
        [authored["src/lib/shared.ts"], "export const shared = 2;\n"],
        ["export const shared = 2;\n", authored["src/lib/shared.ts"]],
    ]) {
        await inCheckout(authored, async () => {
            const run = async () => {
                for (const [index, text] of texts.entries())
                    await addBrowserCoverage([
                        sharedScript(text, `globalThis.shared = ${index};\n`),
                    ]);
            };
            await expect(run()).rejects.toThrow(
                "Coverage text for web/src/lib/shared.ts",
            );
        });
    }
});

test("map texts are compared with the authored file without a leading byte order mark", async () => {
    for (const fileBom of ["", "\u{FEFF}"])
        for (const mapBom of ["", "\u{FEFF}"])
            await inCheckout(
                { "src/lib/bom.ts": `${fileBom}export const bom = 1;\n` },
                () => {
                    const entry = (text: string) => ({
                        url: pathToFileURL(
                            path.join(serverOutput(), "chunks", "bom.js"),
                        ).href,
                        source: `bom;\n${inlineReference(flatMap("../../../src/lib/bom.ts", text))}\n`,
                    });
                    const same = `${mapBom}export const bom = 1;\n`;
                    expect(
                        preparedEntry(entry(same), serverMap)?.sourceMap
                            .sourcesContent,
                    ).toEqual([`${same}\n${authoredEnd}`]);
                    expect(() =>
                        preparedEntry(
                            entry(`${mapBom}export const bom = 2;\n`),
                            serverMap,
                        ),
                    ).toThrow("Coverage text for web/src/lib/bom.ts");
                },
            );
});

test("an inline map's relative names resolve against the script, not the report's key form", async () => {
    const text = "export const api = 1;\n";
    await inCheckout(
        {
            "src/lib/api.ts": text,
            ".next/server/chunks/web/src/lib/api.ts": text,
        },
        () => {
            const entry = (source: string) => ({
                url: pathToFileURL(
                    path.join(serverOutput(), "chunks", "page.js"),
                ).href,
                source: `api;\n${inlineReference(flatMap(source, text))}\n`,
            });
            expect(preparedEntry(entry("web/src/lib/api.ts"), serverMap)).toBe(
                undefined,
            );
            const physical = authoredFile(
                pathToFileURL(path.resolve("src/lib/api.ts")).href,
            )!;
            expect(
                preparedEntry(entry("../../../src/lib/api.ts"), serverMap)
                    ?.sourceMap.sources,
            ).toEqual([pathToFileURL(physical).href]);
        },
    );
});

test("an external map's names resolve against the map's own address joined with its relative source root", async () => {
    const text = "export const api = 1;\n";
    await inCheckout({ "src/lib/api.ts": text }, () => {
        const chunks = path.join(serverOutput(), "chunks");
        mkdirSync(path.join(chunks, "maps"), { recursive: true });
        writeFileSync(
            path.join(chunks, "maps", "page.js.map"),
            JSON.stringify({
                ...flatMap("lib/api.ts", text),
                sourceRoot: "../../../../src",
            }),
        );
        const physical = authoredFile(
            pathToFileURL(path.resolve("src/lib/api.ts")).href,
        )!;
        expect(
            preparedEntry(
                {
                    url: pathToFileURL(path.join(chunks, "page.js")).href,
                    source: "api;\n//# sourceMappingURL=maps/page.js.map\n",
                },
                serverMap,
            )?.sourceMap.sources,
        ).toEqual([pathToFileURL(physical).href]);
    });
});

test("only flat maps and line-offset indexes of flat maps are accepted, and the converter never loads another map", async () => {
    const one = "export const one = 1;\n";
    const two = "export const two = 2;\n";
    await inCheckout(
        { "src/lib/one.ts": one, "src/lib/two.ts": two },
        async () => {
            const directory = path.join(serverOutput(), "chunks");
            mkdirSync(directory, { recursive: true });
            const generated = "globalThis.one = 1;\nglobalThis.two = 2;\n";
            const script = (scriptName: string, map: object) => {
                const source = `${generated}//# sourceMappingURL=${scriptName}.js.map\n`;
                writeFileSync(path.join(directory, `${scriptName}.js`), source);
                writeFileSync(
                    path.join(directory, `${scriptName}.js.map`),
                    JSON.stringify(map),
                );
                const url = pathToFileURL(
                    path.join(directory, `${scriptName}.js`),
                ).href;
                return { url, source, mapUrl: `${url}.map` };
            };
            const index = (sections: unknown[]) => ({ version: 3, sections });
            const first = {
                offset: { line: 0, column: 0 },
                map: flatMap("../../../src/lib/one.ts", one),
            };
            const second = {
                offset: { line: 1, column: 0 },
                map: flatMap("turbopack:///[project]/src/lib/two.ts", two),
            };
            for (const [scriptName, map, reason] of [
                [
                    "unmapped",
                    index([
                        first,
                        {
                            ...second,
                            map: { version: 3, sources: [], names: [] },
                        },
                    ]),
                    "a map has no mappings",
                ],
                [
                    "text-column",
                    index([
                        first,
                        { ...second, offset: { line: 1, column: "0" } },
                    ]),
                    "not at the start of a line",
                ],
                [
                    "column",
                    index([
                        first,
                        { ...second, offset: { line: 1, column: 5 } },
                    ]),
                    "not at the start of a line",
                ],
                [
                    "nested",
                    index([
                        first,
                        {
                            offset: { line: 1, column: 0 },
                            map: index([second]),
                        },
                    ]),
                    "nested",
                ],
                ["descending", index([second, first]), "out of order"],
            ] as const) {
                const { url, source, mapUrl } = script(scriptName, map);
                expect(
                    () => preparedEntry({ url, source }, serverMap),
                    scriptName,
                ).toThrow(`Coverage map ${mapUrl} of ${url}: `);
                expect(
                    () => preparedEntry({ url, source }, serverMap),
                    scriptName,
                ).toThrow(reason);
            }
            const unordered = script(
                "unordered",
                flatMap("../../../src/lib/one.ts", one, "IAAA,HAAA"),
            );
            expect(
                preparedEntry(unordered, serverMap)?.sourceMap.mappings,
            ).toBe("CAAA,GAAA");
            const flat = {
                version: 3,
                sources: ["../../../src/lib/one.ts", "../../../src/lib/two.ts"],
                sourcesContent: [one, two],
                names: [],
                mappings: "AAAA;ACAA",
            };
            const prepared = [
                script("index", index([first, second])),
                script("flat", flat),
                {
                    url: pathToFileURL(path.join(directory, "inline.js")).href,
                    source: `${generated}${inlineReference(flat)}\n`,
                },
            ].map(({ url, source }) =>
                preparedEntry({ url, source }, serverMap)!,
            );
            for (const entry of prepared)
                expect(entry.sourceMap.mappings).toBe("AAAA;ACAA");
            const resolved: string[] = [];
            const options = {
                ...serverCoverage(),
                sourceMapResolver: (url: string) => {
                    resolved.push(url);
                    return Promise.resolve(undefined);
                },
            };
            await new CoverageReport(options).add(
                prepared.map((entry) => ({
                    ...entry,
                    functions: wholeScript(entry.source),
                })),
            );
            expect(resolved).toEqual([]);
            const records = await scriptRecords(serverCoverage());
            for (const source of ["one", "two"])
                expect(
                    Object.values(records[`web/src/lib/${source}.ts`]!).map(
                        lineHits,
                    ),
                ).toEqual([{ 1: 1 }, { 1: 1 }, { 1: 1 }]);
        },
    );
});

test("a block comment directive keeps its script's execution and a script without a directive adds nothing", async () => {
    const text = "go();\n";
    await inCheckout({ "src/lib/go.ts": text }, async () => {
        const map = {
            sources: ["turbopack:///[project]/src/lib/go.ts"],
            sourcesContent: [text],
            mappings: "AAAA",
        };
        const commented = browserScript(
            "go",
            text,
            map,
            2,
            (file) => `/*# sourceMappingURL=${file} */\n`,
        );
        const plain = browserScript("plain", text, map, 1, () => "");
        await addBrowserCoverage([commented, plain]);
        const records = await scriptRecords(browserCoverage());
        expect(Object.keys(records)).toEqual(["web/src/lib/go.ts"]);
        expect(
            Object.keys(records["web/src/lib/go.ts"]!).map(identityUrl),
        ).toEqual([commented.url]);
        expect(lineHits(only(records["web/src/lib/go.ts"]))).toEqual({ 1: 2 });
    });
});

test("build files are read only inside the build's static and server directories", async () => {
    await inCheckout(authored, async (web) => {
        const source = "globalThis.shared = 1;\n";
        writeFileSync(path.join(web, "outside.js"), source);
        mkdirSync(path.join(web, "elsewhere"));
        writeFileSync(path.join(web, "elsewhere", "x.js"), source);
        const staticDirectory = path.join(distDir(), "static");
        mkdirSync(staticDirectory, { recursive: true });
        symlinkSync(
            path.join(web, "elsewhere"),
            path.join(staticDirectory, "linked"),
            "junction",
        );
        for (const script of [
            "..%2F..%2Foutside.js",
            "%2E%2E%5C%2E%2E%5Coutside.js",
            "linked/x.js",
        ]) {
            const url = `${coverageOrigin}/_next/static/${script}`;
            await expect(
                addBrowserCoverage([
                    { url, source, functions: wholeScript(source) },
                ]),
                script,
            ).rejects.toThrow(url);
        }
        const chunks = path.join(serverOutput(), "chunks");
        mkdirSync(chunks, { recursive: true });
        writeFileSync(
            path.join(web, "outside.js.map"),
            JSON.stringify(
                flatMap("src/lib/shared.ts", authored["src/lib/shared.ts"]),
            ),
        );
        const url = pathToFileURL(path.join(chunks, "page.js")).href;
        const page = `${source}//# sourceMappingURL=../../../outside.js.map\n`;
        writeFileSync(path.join(chunks, "page.js"), page);
        expect(() => preparedEntry({ url, source: page }, serverMap)).toThrow(
            `Coverage map ${pathToFileURL(path.join(web, "outside.js.map")).href} of ${url}: `,
        );
    });
});

test("server coverage is read from the one snapshot of the inspected process in this run's directory", async () => {
    await inCheckout(authored, async (web) => {
        const valid = serverBuild(web);
        const directory = path.dirname(valid);
        const server = { pid: 7, threadId: 0 };
        expect(runSnapshot(directory, server)).toBe(valid);
        writeFileSync(path.join(directory, "coverage-8-3-0.json"), "{}");
        writeFileSync(path.join(directory, "coverage-7-3-1.json"), "{}");
        expect(runSnapshot(directory, server)).toBe(valid);
        expect(() =>
            runSnapshot(path.join(directory, "missing"), server),
        ).toThrow("wrote no coverage snapshot");
        expect(() => runSnapshot(valid, server)).toThrow("ENOTDIR");
        const empty = path.join(directory, "coverage-7-2-0.json");
        writeFileSync(empty, JSON.stringify({ result: [] }));
        expect(() => runSnapshot(directory, server)).toThrow(
            "wrote 2 coverage snapshots",
        );
        await expect(ingestServerSnapshot(empty)).rejects.toThrow(
            "No entry of the Next server's coverage snapshot",
        );
        expect(serverProcess({ pid: 7, threadId: 0 })).toEqual(server);
        for (const answer of [
            undefined,
            null,
            { pid: 0, threadId: 0 },
            { pid: 7 },
            { pid: "7", threadId: 0 },
            { pid: 7, threadId: -1 },
            { pid: 7.5, threadId: 0 },
        ])
            expect(() => serverProcess(answer), JSON.stringify(answer)).toThrow(
                "answered no process identity",
            );
    });
});

function listening(port = 0): Promise<Server> {
    const listener = createServer();
    return new Promise((resolve, reject) => {
        listener.once("error", reject);
        listener.listen({ host: "127.0.0.1", port }, () => resolve(listener));
    });
}

const portOf = (listener: Server) => (listener.address() as AddressInfo).port;
const closed = (listener: Server) =>
    new Promise<void>((resolve) => listener.close(() => resolve()));

async function freePort(): Promise<number> {
    const listener = await listening();
    const port = portOf(listener);
    await closed(listener);
    return port;
}

const testing = path.resolve("testing");
const lockModule = path.join(testing, "coverage-lock.mjs");
const serverSettings = pathToFileURL(path.resolve("tests/support/server.ts"));

function wrapperRun(web: string, args: string[], lockPort: number) {
    mkdirSync(path.join(web, "testing"), { recursive: true });
    for (const script of ["run-coverage.mjs", "coverage-lock.mjs"])
        copyFileSync(
            path.join(testing, script),
            path.join(web, "testing", script),
        );
    mkdirSync(path.join(web, "tests", "support"), { recursive: true });
    writeFileSync(
        path.join(web, "tests", "support", "server.ts"),
        `export * from ${JSON.stringify(serverSettings.href)};\nexport const coverageLockPort = ${lockPort};\n`,
    );
    for (const directory of ["coverage", ".next-coverage"]) {
        mkdirSync(path.join(web, directory), { recursive: true });
        writeFileSync(path.join(web, directory, "kept"), "");
    }
    const result = spawnSync(
        process.execPath,
        [path.join(web, "testing", "run-coverage.mjs"), ...args],
        { encoding: "utf8" },
    );
    const kept = ["coverage", ".next-coverage"].every((directory) =>
        existsSync(path.join(web, directory, "kept")),
    );
    return { ...result, kept };
}

test("the coverage wrapper refuses UI mode before it deletes or builds anything", async () => {
    const port = await freePort();
    await inCheckout({}, (web) => {
        for (const args of [
            ["--ui"],
            ["--ui-port=9000"],
            ["tests/a.spec.ts", "--ui"],
        ]) {
            const result = wrapperRun(web, args, port);
            expect(result.status).toBe(2);
            expect(result.stderr).toContain("UI mode");
            expect(result.kept).toBe(true);
        }
    });
});

test("the coverage wrapper refuses to run while its port is held and leaves no lock behind", async () => {
    await inCheckout({}, async (web) => {
        const holder = await listening();
        const port = portOf(holder);
        try {
            const refused = wrapperRun(web, [], port);
            expect(refused.status).toBe(2);
            expect(refused.stderr).toContain(
                `Port ${port} on 127.0.0.1 is taken: another coverage run on this machine holds it`,
            );
            expect(refused.kept).toBe(true);
        } finally {
            await closed(holder);
        }
        const failed = wrapperRun(web, [], port);
        expect(failed.status).toBe(1);
        expect(failed.kept).toBe(false);
        expect(existsSync(path.join(web, "coverage.lock"))).toBe(false);
        await closed(await listening(port));
    });
});

test("the system frees the coverage lock of a process that was killed", async () => {
    const child = spawn(
        process.execPath,
        [
            "--input-type=module",
            "-e",
            `import { holdCoverageLock } from ${JSON.stringify(pathToFileURL(lockModule).href)};\nconsole.log("held " + (await holdCoverageLock(0)));\nsetInterval(() => undefined, 1000);`,
        ],
        { stdio: ["ignore", "pipe", "inherit"] },
    );
    const exited = new Promise((resolve) => child.once("exit", resolve));
    const port = await new Promise<number>((resolve, reject) => {
        let output = "";
        child.stdout.on("data", (data: Buffer) => {
            output += data.toString();
            const held = /held (\d+)/.exec(output);
            if (held) resolve(Number(held[1]));
        });
        child.once("exit", () => reject(new Error("The lock holder ended")));
    });
    await expect(listening(port)).rejects.toThrow("EADDRINUSE");
    child.kill();
    await exited;
    await closed(await listening(port));
});
