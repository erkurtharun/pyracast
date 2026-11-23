import { expect, test } from "@playwright/test";
import { spawnSync } from "node:child_process";
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
import os from "node:os";
import path from "node:path";
import { pathToFileURL } from "node:url";
import { CoverageReport } from "monocart-coverage-reports";
import { config as proxyConfig } from "@/proxy";
import {
    combineCoverage,
    type FileCoverage,
    type ScriptRecords,
} from "../testing/coverage-combine";
import {
    addBrowserCoverage,
    authoredEnd,
    fileMap,
    preparedEntry,
} from "../testing/coverage-maps";
import {
    authoredFile,
    browserCoverage,
    browserEntry,
    distDir,
    reportKey,
    serverCoverage,
    serverEntry,
    serverOutput,
} from "../testing/coverage-options";
import CoverageReporter, { scriptRecords } from "../testing/coverage-reporter";
import { ingestServerSnapshot, newSnapshot } from "../testing/server-coverage";
import {
    coverageOrigin,
    coverageReadinessPath,
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

const inlineReference = (map: object) =>
    `//# sourceMappingURL=data:application/json;base64,${Buffer.from(JSON.stringify(map)).toString("base64")}`;

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

test("the coverage server is configured to be ready without running authored server code", () => {
    expect(webServer(true).url).toBe(
        `${coverageOrigin}${coverageReadinessPath}`,
    );
    expect(existsSync(path.join("public", coverageReadinessPath))).toBe(true);
    const [matcher] = proxyConfig.matcher;
    expect(new RegExp(`^${matcher}$`).test(coverageReadinessPath)).toBe(false);
    expect(new RegExp(`^${matcher}$`).test("/app/")).toBe(true);
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
            baseline: {},
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
        baseline: {},
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
        baseline,
    })[name]!;
    expect(lineHits(combined)).toEqual({ 1: 3, 2: 1, 3: 0 });
    const functionsOnly = combineCoverage({
        sources: [name],
        browser: { [name]: { a: file(name, [], [1]) } },
        server: {},
        baseline,
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
        baseline,
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
            baseline: {},
        })["web/src/d.ts"],
    ).toEqual(file("web/src/d.ts", []));
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

function browserScript(
    script: string,
    text: string,
    map: object,
    count = 1,
): { url: string; source: string; functions: unknown[] } {
    const directory = path.join(distDir(), "static", "chunks");
    mkdirSync(directory, { recursive: true });
    const source = `${text}//# sourceMappingURL=${script}.js.map\n`;
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

const sharedScript = (text = authored["src/lib/shared.ts"]) =>
    browserScript("shared", "globalThis.shared = 1;\n", {
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
        expect([...records.keys()].sort()).toEqual([
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
    for (const text of ["go();\n", "go();"]) {
        await inCheckout(
            { "src/lib/go.ts": text, "src/lib/other.ts": "other();\n" },
            async () => {
                const script = browserScript(
                    "go",
                    "go();\n",
                    {
                        sources: [
                            "turbopack:///[project]/src/lib/go.ts",
                            "turbopack:///[project]/src/lib/other.ts",
                        ],
                        sourcesContent: [text, "other();\n"],
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
                expect(records["web/src/lib/other.ts"]![script.url]!.s).toEqual(
                    {},
                );
                expect(
                    lineHits(records["web/src/lib/go.ts"]![script.url]!),
                ).toEqual({ 1: 4 });
            },
        );
    }
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
            expect(Object.keys(records["web/src/lib/shared.ts"]!)).toEqual([
                `${coverageOrigin}/_next/static/chunks/a.js`,
                `${coverageOrigin}/_next/static/chunks/b.js`,
            ]);
            const combined = combineCoverage({
                sources: ["web/src/lib/shared.ts"],
                browser: records,
                server: {},
                baseline: {},
            })["web/src/lib/shared.ts"]!;
            expect(lineHits(combined)).toEqual({ 1: 3 });
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
                for (const text of texts)
                    await addBrowserCoverage([sharedScript(text)]);
            };
            await expect(run()).rejects.toThrow(
                "Coverage text for web/src/lib/shared.ts",
            );
        });
    }
});

test("map texts are compared with the authored file without a leading byte order mark", async () => {
    for (const fileBom of ["", "\uFEFF"])
        for (const mapBom of ["", "\uFEFF"])
            await inCheckout(
                { "src/lib/bom.ts": `${fileBom}export const bom = 1;\n` },
                () => {
                    const entry = (text: string) => ({
                        url: pathToFileURL(
                            path.join(serverOutput(), "chunks", "bom.js"),
                        ).href,
                        source: `bom;\n${inlineReference({
                            version: 3,
                            sources: ["../../../src/lib/bom.ts"],
                            sourcesContent: [text],
                            names: [],
                            mappings: "AAAA",
                        })}\n`,
                    });
                    const same = `${mapBom}export const bom = 1;\n`;
                    expect(
                        preparedEntry(entry(same), fileMap)?.sourceMap
                            .sourcesContent,
                    ).toEqual([`${same}\n${authoredEnd}`]);
                    expect(() =>
                        preparedEntry(
                            entry(`${mapBom}export const bom = 2;\n`),
                            fileMap,
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
                source: `api;\n${inlineReference({
                    version: 3,
                    sources: [source],
                    sourcesContent: [text],
                    names: [],
                    mappings: "AAAA",
                })}\n`,
            });
            expect(preparedEntry(entry("web/src/lib/api.ts"), fileMap)).toBe(
                undefined,
            );
            const physical = authoredFile(
                pathToFileURL(path.resolve("src/lib/api.ts")).href,
            )!;
            expect(
                preparedEntry(entry("../../../src/lib/api.ts"), fileMap)
                    ?.sourceMap.sources,
            ).toEqual([pathToFileURL(physical).href]);
        },
    );
});

test("an index map is flattened with its section offsets", async () => {
    const one = "export const one = 1;\n";
    const two = "export const two = 2;\n";
    await inCheckout(
        { "src/lib/one.ts": one, "src/lib/two.ts": two },
        async () => {
            const generated = "globalThis.one = 1;\nglobalThis.two = 2;\n";
            const directory = path.join(serverOutput(), "chunks");
            mkdirSync(directory, { recursive: true });
            const source = `${generated}//# sourceMappingURL=index.js.map\n`;
            writeFileSync(path.join(directory, "index.js"), source);
            const section = (
                line: number,
                sourceName: string,
                content: string,
            ) => ({
                offset: { line, column: 0 },
                map: {
                    version: 3,
                    sources: [sourceName],
                    sourcesContent: [content],
                    names: [],
                    mappings: "AAAA",
                },
            });
            writeFileSync(
                path.join(directory, "index.js.map"),
                JSON.stringify({
                    version: 3,
                    sections: [
                        section(0, "../../../src/lib/one.ts", one),
                        section(
                            1,
                            "turbopack:///[project]/src/lib/two.ts",
                            two,
                        ),
                    ],
                }),
            );
            const url = pathToFileURL(path.join(directory, "index.js")).href;
            const prepared = preparedEntry({ url, source }, fileMap)!;
            expect(prepared.sourceMap.decodedMappings).toEqual([
                [[0, 0, 0, 0]],
                [[0, 1, 0, 0]],
            ]);
            await new CoverageReport(serverCoverage()).add([
                { ...prepared, functions: wholeScript(source) },
            ]);
            const records = await scriptRecords(serverCoverage());
            expect(lineHits(records["web/src/lib/one.ts"]![url]!)).toEqual({
                1: 1,
            });
            expect(lineHits(records["web/src/lib/two.ts"]![url]!)).toEqual({
                1: 1,
            });
        },
    );
});

test("server coverage is read only from the new snapshot of this run's server process", async () => {
    await inCheckout(authored, async (web) => {
        const valid = serverBuild(web);
        const directory = path.dirname(valid);
        const server = { pid: 7, threadId: 0 };
        writeFileSync(
            path.join(directory, "coverage-8-3-0.json"),
            readFileSync(valid),
        );
        expect(() =>
            newSnapshot(directory, new Set(["coverage-7-1-0.json"]), server),
        ).toThrow("wrote no new coverage snapshot");
        const empty = path.join(directory, "coverage-7-2-0.json");
        writeFileSync(empty, JSON.stringify({ result: [] }));
        const before = new Set(["coverage-7-1-0.json"]);
        expect(newSnapshot(directory, before, server)).toBe(empty);
        await expect(ingestServerSnapshot(empty)).rejects.toThrow(
            "No entry of the Next server's coverage snapshot",
        );
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
    const kept = ["coverage", ".next-coverage"].every((directory) =>
        existsSync(path.join(web, directory, "kept")),
    );
    return { ...result, kept };
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
            expect(result.kept).toBe(true);
        }
    });
});

test("the coverage wrapper refuses an existing lock whatever process it names", async () => {
    const ended = spawnSync(process.execPath, ["-e", ""]).pid;
    for (const holder of [ended, process.pid]) {
        await inCheckout({ "coverage.lock": String(holder) }, (web) => {
            const result = wrapperRun(web, []);
            expect(result.status).toBe(2);
            expect(result.stderr).toContain("Delete it by hand only after");
            expect(result.kept).toBe(true);
            expect(readFileSync(path.join(web, "coverage.lock"), "utf8")).toBe(
                String(holder),
            );
        });
    }
});

test("the coverage wrapper releases its lock when the build fails", async () => {
    await inCheckout({}, (web) => {
        const result = wrapperRun(web, []);
        expect(result.status).toBe(1);
        expect(existsSync(path.join(web, "coverage.lock"))).toBe(false);
    });
});
