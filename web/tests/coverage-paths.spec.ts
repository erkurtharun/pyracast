import { expect, test, type CDPSession } from "@playwright/test";
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
import { connect, createServer, type AddressInfo, type Server } from "node:net";
import os from "node:os";
import path from "node:path";
import { pathToFileURL } from "node:url";
import { CoverageReport } from "monocart-coverage-reports";
import { config as proxyConfig } from "@/proxy";
import {
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
    startBrowserCoverage,
} from "../testing/coverage-maps";
import {
    authoredFile,
    browserCoverage,
    browserEntry,
    combinedCoverage,
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
            functions.map((_, i) => [
                i,
                {
                    name: `f${i}`,
                    decl: span(20 + i, 0, 20 + i, 9),
                    loc: span(20 + i, 9, 20 + i, 30),
                },
            ]),
        ),
        f: Object.fromEntries(functions.map((count, i) => [i, count])),
        branchMap: Object.fromEntries(
            branches.map((arms, i) => [
                i,
                {
                    type: "SwitchStatement",
                    loc: span(40 + i, 0, 40 + i, 50),
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
const first = (count: number): [number, number] => [2, count];
const second = (count: number): [number, number] => [4, count];
const third = (count: number): [number, number] => [6, count];
const ranSwitch = (...arms: [number, number][]) =>
    file(name, [[span(1, 0, 5, 1), 1]], [1], [arms]);
const union = (
    sides: Partial<Omit<Parameters<typeof combineCoverage>[0], "sources">>,
) => {
    const { coverage, statistics } = combineCoverage({
        sources: [name],
        browser: {},
        server: {},
        baseline: {},
        ...sides,
    });
    return { coverage: coverage[name]!, statistics };
};
const united = (sides: Parameters<typeof union>[0]) => union(sides).coverage;
const statistics = ({ functions = 0, groups = 0, lackedArms = 0 }) => ({
    unitedOnEqualRanges: { functions, groups },
    armsSomeRecordLacks: lackedArms,
});
const bothOrders = (one: FileCoverage, other: FileCoverage) => [
    { "a.js": one, "b.js": other },
    { "a.js": other, "b.js": one },
];

test("a switch compiled in opposite arm order keeps each authored arm's count in either script order", () => {
    for (const [forward, reversed] of [
        [ranSwitch(first(0), second(0)), ranSwitch(second(0), first(1))],
        [ranSwitch(first(1), second(0)), ranSwitch(second(0), first(0))],
    ])
        for (const scripts of bothOrders(forward!, reversed!)) {
            const coverage = united({ server: { [name]: scripts } });
            expect(armHits(coverage)).toEqual({ 2: 1, 4: 0 });
            expect(coverage.b).toEqual({ 0: [1, 0] });
        }
});

test("an arm taken only in a script after the first executed one is credited", () => {
    const untaken = file(
        name,
        [[span(1, 0, 5, 1), 1]],
        [1],
        [
            [first(0), second(0)],
            [
                [8, 0],
                [9, 0],
            ],
        ],
    );
    const taken = ranSwitch(first(0), second(1));
    expect(
        armHits(united({ browser: { [name]: { a: untaken, b: taken } } })),
    ).toEqual({ 2: 0, 4: 1, 8: 0, 9: 0 });
    expect(
        armHits(
            united({
                browser: { [name]: { a: untaken } },
                server: { [name]: { b: taken } },
            }),
        ),
    ).toEqual({ 2: 0, 4: 1, 8: 0, 9: 0 });
});

test("an arm one compilation lacks is counted once and taken when the compilation that has it took it", () => {
    for (const scripts of bothOrders(
        ranSwitch(first(1), second(0)),
        ranSwitch(first(0), second(0), third(1)),
    )) {
        const coverage = united({ browser: { [name]: scripts } });
        expect(coverage.b).toEqual({ 0: [1, 0, 1] });
        expect(armHits(coverage)).toEqual({ 2: 1, 4: 0, 6: 1 });
    }
});

test("a function only the server executed is executed although the browser's record has it unexecuted", () => {
    const coverage = united({
        browser: { [name]: { a: file(name, [[span(1, 0, 1, 9), 1]], [0, 1]) } },
        server: { [name]: { b: file(name, [[span(1, 0, 1, 9), 1]], [1, 0]) } },
    });
    expect(coverage.f).toEqual({ 0: 1, 1: 1 });
    expect(Object.keys(coverage.fnMap)).toHaveLength(2);
});

test("records with equal tables add their counts and a zero baseline record adds only what is counted", () => {
    const measured = file(
        name,
        [[span(1, 0, 1, 9), 1]],
        [1],
        [[first(1), second(0)]],
    );
    const coverage = united({
        browser: { [name]: { a: measured, b: measured } },
        baseline: {
            [name]: {
                c: file(
                    name,
                    [
                        [span(1, 0, 1, 9), 5],
                        [span(3, 0, 3, 9), 5],
                    ],
                    [5, 5],
                    [
                        [first(5), second(5)],
                        [
                            [8, 5],
                            [9, 5],
                        ],
                    ],
                ),
            },
        },
    });
    expect(coverage.f).toEqual({ 0: 2, 1: 0 });
    expect(coverage.b).toEqual({ 0: [2, 0], 1: [0, 0] });
    expect(lineHits(coverage)).toEqual({ 1: 2, 3: 0 });
    expect(united({})).toEqual(file(name, []));
});

test("arms that some records of a group lack are counted as partial arms", () => {
    const one = file(
        name,
        [],
        [],
        [
            [first(1), second(0)],
            [
                [8, 0],
                [9, 1],
            ],
        ],
    );
    const other = file(
        name,
        [],
        [],
        [
            [first(0), [5, 0]],
            [
                [8, 1],
                [9, 0],
            ],
        ],
    );
    for (const scripts of bothOrders(one, other)) {
        const measured = union({ browser: { [name]: scripts } });
        expect(armHits(measured.coverage)).toEqual({
            2: 1,
            4: 0,
            5: 0,
            8: 1,
            9: 1,
        });
        expect(measured.statistics).toEqual(statistics({ lackedArms: 2 }));
    }
});

type Range = ReturnType<typeof span>;
const compiled = (
    functions: [string, Range, Range, number][],
    branches: [string, Range, [Range, number][]][] = [],
): FileCoverage => ({
    path: name,
    statementMap: {},
    s: {},
    fnMap: Object.fromEntries(
        functions.map(([fn, decl, loc], i) => [i, { name: fn, decl, loc }]),
    ),
    f: Object.fromEntries(functions.map(([, , , count], i) => [i, count])),
    branchMap: Object.fromEntries(
        branches.map(([type, loc, arms], i) => [
            i,
            { type, loc, locations: arms.map(([location]) => location) },
        ]),
    ),
    b: Object.fromEntries(
        branches.map(([, , arms], i) => [i, arms.map(([, count]) => count)]),
    ),
});
const counted = (coverage: FileCoverage) =>
    Object.entries(coverage.fnMap).map(([key, fn]) => [
        fn.name,
        coverage.f[key],
    ]);

test("a function whose start differs between compilations while its body's end agrees is one function", () => {
    for (const scripts of bothOrders(
        compiled([["isLanguage", span(4, 7, 4, 7), span(4, 7, 6, 1), 1]]),
        compiled([["isLanguage", span(4, 7, 4, 62), span(4, 62, 6, 1), 2]]),
    ))
        expect(counted(united({ browser: { [name]: scripts } }))).toEqual([
            ["isLanguage", 3],
        ]);
});

test("functions sharing an end in one record unite only on equal ranges, so no count moves between an outer and an inner function", () => {
    const inner: [string, Range, Range, number] = [
        "inner",
        span(3, 23, 3, 23),
        span(3, 23, 3, 40),
        2,
    ];
    const listedOuterFirst = compiled([
        ["outer", span(3, 17, 3, 17), span(3, 17, 3, 40), 1],
        ["inner", span(3, 23, 3, 23), span(3, 23, 3, 40), 0],
    ]);
    const placed = (coverage: FileCoverage) =>
        Object.entries(coverage.fnMap).map(([key, fn]) => [
            fn.name,
            fn.decl!.start.column,
            coverage.f[key],
        ]);
    for (const [other, expected] of [
        [
            compiled([
                inner,
                ["outer", span(3, 16, 3, 16), span(3, 16, 3, 40), 3],
            ]),
            [
                ["outer", 16, 3],
                ["outer", 17, 1],
                ["inner", 23, 2],
            ],
        ],
        [
            compiled([
                inner,
                ["outer", span(3, 17, 3, 17), span(3, 17, 3, 40), 3],
            ]),
            [
                ["outer", 17, 4],
                ["inner", 23, 2],
            ],
        ],
    ] as const)
        for (const scripts of bothOrders(listedOuterFirst, other)) {
            const measured = union({ browser: { [name]: scripts } });
            expect(placed(measured.coverage)).toEqual(expected);
            expect(measured.statistics).toEqual(statistics({ functions: 1 }));
        }
});

const described = (coverage: FileCoverage) =>
    Object.entries(coverage.fnMap).map(([key, fn]) => ({
        ...fn,
        count: coverage.f[key],
    }));

test("a module function some compilations carry keeps its own name, place and count beside the function sharing its end", () => {
    const callback = (count: number) =>
        ["noop", span(1, 19, 1, 19), span(1, 19, 1, 21), count] as [
            string,
            Range,
            Range,
            number,
        ];
    const wrapped = compiled([
        ["module", span(1, 13, 1, 13), span(1, 13, 1, 21), 5],
        callback(0),
    ]);
    for (const count of [0, 3])
        for (const scripts of bothOrders(
            compiled([callback(count)]),
            wrapped,
        )) {
            const measured = union({ browser: { [name]: scripts } });
            expect(described(measured.coverage)).toEqual([
                {
                    name: "module",
                    decl: span(1, 13, 1, 13),
                    loc: span(1, 13, 1, 21),
                    count: 5,
                },
                {
                    name: "noop",
                    decl: span(1, 19, 1, 19),
                    loc: span(1, 19, 1, 21),
                    count,
                },
            ]);
            expect(measured.statistics).toEqual(statistics({ functions: 1 }));
        }
});

const armsByEnd = (coverage: FileCoverage) =>
    Object.entries(coverage.branchMap).map(([key, branch]) =>
        Object.fromEntries(
            branch.locations!.map((location, arm) => [
                `${location.end!.line}:${location.end!.column}`,
                coverage.b[key]![arm],
            ]),
        ),
    );

test("a branch group whose start column differs between compilations while type, start line and end agree is one group with its arms united by end", () => {
    const one = compiled(
        [],
        [
            [
                "SwitchStatement",
                span(7, 4, 10, 1),
                [
                    [span(8, 4, 8, 20), 1],
                    [span(9, 4, 9, 20), 0],
                ],
            ],
        ],
    );
    const other = compiled(
        [],
        [
            [
                "SwitchStatement",
                span(7, 0, 10, 1),
                [
                    [span(9, 9, 9, 20), 0],
                    [span(8, 8, 8, 20), 2],
                    [span(9, 22, 9, 30), 1],
                ],
            ],
        ],
    );
    for (const scripts of bothOrders(one, other)) {
        const measured = union({ browser: { [name]: scripts } });
        expect(armsByEnd(measured.coverage)).toEqual([
            { "8:20": 3, "9:20": 0, "9:30": 1 },
        ]);
        expect(measured.statistics).toEqual(statistics({ lackedArms: 1 }));
    }
});

test("the else arm the converter writes without a location is one arm of its group across records", () => {
    const withoutElse = (counts: number[]): FileCoverage => ({
        ...compiled([]),
        branchMap: {
            0: {
                type: "IfStatement",
                loc: span(46, 8, 56, 9),
                locations: [span(46, 35, 56, 9), { start: {}, end: {} }],
            },
        },
        b: { 0: counts },
    });
    for (const scripts of bothOrders(
        withoutElse([1, 0]),
        withoutElse([0, 2]),
    )) {
        const measured = union({
            browser: { [name]: scripts },
            baseline: { [name]: { c: withoutElse([5, 5]) } },
        });
        expect(measured.coverage.b).toEqual({ 0: [1, 2] });
        expect(measured.statistics).toEqual(statistics({}));
    }
});

test("branch groups of one type and end that start on different lines in two records stay two groups", () => {
    const group = (line: number, counts: [number, number]) =>
        compiled(
            [],
            [
                [
                    "IfStatement",
                    span(line, 4, 9, 1),
                    [
                        [span(line, 20, line, 30), counts[0]],
                        [span(line + 1, 4, 9, 1), counts[1]],
                    ],
                ],
            ],
        );
    for (const scripts of bothOrders(group(3, [1, 0]), group(5, [0, 1]))) {
        const measured = union({ browser: { [name]: scripts } });
        expect(
            Object.values(measured.coverage.branchMap).map(
                ({ loc }) => loc!.start.line,
            ),
        ).toEqual([3, 5]);
        expect(measured.coverage.b).toEqual({ 0: [1, 0], 1: [0, 1] });
        expect(measured.statistics).toEqual(statistics({}));
    }
});

async function branchLines(coverage: FileCoverage): Promise<string[]> {
    let lines: string[] = [];
    await inCheckout({}, async () => {
        const report = new CoverageReport({
            ...combinedCoverage(),
            reports: ["lcovonly"],
        });
        await report.add({ [coverage.path]: coverage });
        await report.generate();
        lines = readFileSync("coverage/lcov.info", "utf8")
            .split("\n")
            .filter((line) => /^BR[DFH]/.test(line));
    });
    return lines;
}

test("sibling conditions of one end that one compilation lacks stay one group per start line counted by the records that have them", async () => {
    const conditional = (line: number, counts: [number, number]) =>
        [
            "ConditionalExpression",
            span(line, 11, 719, 25),
            [
                [span(line, 20, line, 30), counts[0]],
                [span(line + 1, 8, 719, 25), counts[1]],
            ],
        ] as [string, Range, [Range, number][]];
    const four = compiled(
        [],
        [
            conditional(712, [0, 1]),
            conditional(714, [0, 1]),
            conditional(715, [1, 0]),
            conditional(717, [0, 0]),
        ],
    );
    const three = compiled(
        [],
        [
            conditional(712, [0, 1]),
            conditional(715, [1, 0]),
            conditional(717, [0, 0]),
        ],
    );
    for (const scripts of bothOrders(four, three)) {
        const measured = union({ browser: { [name]: scripts } });
        expect(await branchLines(measured.coverage)).toEqual([
            "BRDA:712,0,0,0",
            "BRDA:712,0,1,2",
            "BRDA:714,1,0,0",
            "BRDA:714,1,1,1",
            "BRDA:715,2,0,2",
            "BRDA:715,2,1,0",
            "BRDA:717,3,0,0",
            "BRDA:717,3,1,0",
            "BRF:8",
            "BRH:3",
        ]);
        expect(measured.statistics).toEqual(statistics({}));
    }
});

test("a taken arm of sibling conditions sharing an end is reported only on the line of the group that took it", async () => {
    const conditional = (line: number, counts: [number, number]) =>
        [
            "ConditionalExpression",
            span(line, 11, 6, 0),
            [
                [span(line, 40, line, 63), counts[0]],
                [span(line + 1, 0, 6, 0), counts[1]],
            ],
        ] as [string, Range, [Range, number][]];
    const client = compiled(
        [],
        [conditional(2, [1, 0]), conditional(3, [0, 0])],
    );
    const node = compiled([], [conditional(2, [0, 1]), conditional(4, [1, 0])]);
    const edge = compiled([], [conditional(2, [0, 0])]);
    for (const scripts of bothOrders(client, node)) {
        const measured = union({
            browser: { [name]: scripts },
            baseline: { [name]: { edge } },
        });
        expect(await branchLines(measured.coverage)).toEqual([
            "BRDA:2,0,0,1",
            "BRDA:2,0,1,1",
            "BRDA:3,1,0,0",
            "BRDA:3,1,1,0",
            "BRDA:4,2,0,1",
            "BRDA:4,2,1,0",
            "BRF:6",
            "BRH:3",
        ]);
        expect(measured.statistics).toEqual(statistics({}));
    }
});

test("arms of one group sharing an end unite only on equal locations", () => {
    const logical = (arms: [number, number][]) =>
        compiled(
            [],
            [
                [
                    "LogicalExpression",
                    span(5, 5, 5, 20),
                    arms.map(([start, count]) => [
                        span(5, start, 5, 20),
                        count,
                    ]),
                ],
            ],
        );
    for (const scripts of bothOrders(
        logical([
            [5, 1],
            [10, 0],
            [15, 0],
        ]),
        logical([
            [5, 1],
            [15, 4],
        ]),
    )) {
        const measured = union({ browser: { [name]: scripts } });
        expect(measured.coverage.b).toEqual({ 0: [2, 0, 4] });
        expect(
            measured.coverage.branchMap[0]!.locations!.map(
                (location) => location.start!.column,
            ),
        ).toEqual([5, 10, 15]);
        expect(measured.statistics).toEqual(statistics({ lackedArms: 1 }));
    }
});

test("groups of another type with one start line and end in another record stay two groups", () => {
    const group = (type: string) =>
        compiled(
            [],
            [
                [
                    type,
                    span(3, 0, 6, 1),
                    [
                        [span(4, 4, 4, 20), 1],
                        [span(5, 4, 6, 1), 0],
                    ],
                ],
            ],
        );
    for (const scripts of bothOrders(
        group("IfStatement"),
        group("ConditionalExpression"),
    )) {
        const measured = union({ browser: { [name]: scripts } });
        expect(measured.coverage.b).toEqual({ 0: [1, 0], 1: [1, 0] });
        expect(measured.statistics).toEqual(statistics({}));
    }
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
    const combined = united({
        browser: { [name]: { a: file(name, [[span(1, 0, 1, 9), 3]], [3]) } },
        server: { [name]: { b: file(name, [[span(2, 0, 2, 9), 1]], [1]) } },
        baseline,
    });
    expect(lineHits(combined)).toEqual({ 1: 3, 2: 1, 3: 0 });
    const functionsOnly = united({
        browser: { [name]: { a: file(name, [], [1]) } },
        baseline,
    });
    expect(lineHits(functionsOnly)).toEqual({ 1: 0, 2: 0, 3: 0 });
    expect(functionsOnly.f).toEqual({ 0: 1 });
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
): { url: string; source: string; functions: ReturnType<typeof wholeScript> } {
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

async function logged(run: () => Promise<void>): Promise<string[]> {
    const lines: string[] = [];
    const log = console.log;
    console.log = (...parts: unknown[]) => void lines.push(parts.join(" "));
    try {
        await run();
    } finally {
        console.log = log;
    }
    return lines;
}

async function quietly(run: () => Promise<void>): Promise<void> {
    await logged(run);
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

test("a browser script that is not the build's output is listed and never counted, and fails the report only when it ran", async () => {
    await inCheckout(authored, async (web) => {
        await ingestServerSnapshot(serverBuild(web));
        const ordinary = sharedScript();
        const cut = ordinary.source.slice(0, 10);
        await addBrowserCoverage([
            ordinary,
            { url: ordinary.url, source: cut, functions: wholeScript(cut, 0) },
        ]);
        const printed = await logged(() => new CoverageReporter().onEnd());
        expect(
            lcovRecords(readFileSync("coverage/lcov.info", "utf8")).get(
                "web/src/lib/shared.ts",
            ),
        ).toMatch(/^DA:1,2$/m);
        expect(
            printed.filter((line) => line.startsWith("Browser script")),
        ).toEqual([
            `Browser script that is not this build's output, not counted: ${JSON.stringify(
                {
                    url: ordinary.url,
                    observed: 10,
                    built: ordinary.source.length,
                    prefix: true,
                    executed: false,
                },
            )}`,
        ]);
    });
    await inCheckout(authored, async (web) => {
        await ingestServerSnapshot(serverBuild(web));
        const ordinary = sharedScript();
        const other = "globalThis.other = 1;\n";
        await addBrowserCoverage([ordinary]);
        await addBrowserCoverage([
            { url: ordinary.url, source: other, functions: wholeScript(other) },
        ]);
        await expect(
            quietly(() => new CoverageReporter().onEnd()),
        ).rejects.toThrow(
            `Code that is not this build's output ran in the browser: ${ordinary.url}`,
        );
    });
});

test("a browser script whose text the browser did not give is listed, never counted and never fails the report", async () => {
    await inCheckout(authored, async (web) => {
        await ingestServerSnapshot(serverBuild(web));
        const ordinary = sharedScript();
        await addBrowserCoverage([
            ordinary,
            { url: ordinary.url, functions: wholeScript(ordinary.source) },
        ]);
        const printed = await logged(() => new CoverageReporter().onEnd());
        expect(
            lcovRecords(readFileSync("coverage/lcov.info", "utf8")).get(
                "web/src/lib/shared.ts",
            ),
        ).toMatch(/^DA:1,2$/m);
        expect(
            printed.filter((line) => line.startsWith("Browser script")),
        ).toEqual([
            `Browser script whose text the browser did not give, not counted: ${JSON.stringify(
                {
                    url: ordinary.url,
                    observed: null,
                    built: ordinary.source.length,
                    prefix: false,
                    executed: true,
                },
            )}`,
        ]);
    });
});

test("a report whose only browser execution came from a script that is not the build's output names that script", async () => {
    await inCheckout(authored, async (web) => {
        await ingestServerSnapshot(serverBuild(web));
        const { url } = sharedScript();
        const other = "globalThis.other = 1;\n";
        await addBrowserCoverage([
            { url, source: other, functions: wholeScript(other) },
        ]);
        await expect(
            quietly(() => new CoverageReporter().onEnd()),
        ).rejects.toThrow(
            `Code that is not this build's output ran in the browser: ${url}`,
        );
    });
});

test("the browser capture returns each script's text the browser answers after the coverage was taken", async () => {
    const built = `${coverageOrigin}/_next/static/chunks/late.js`;
    const refused = `${coverageOrigin}/_next/static/chunks/refused.js`;
    const functions = wholeScript("late();\n");
    const parsed: ((event: { scriptId: string; url: string }) => void)[] = [];
    const sent: string[] = [];
    const profile = Promise.withResolvers<{ result: object[] }>();
    const source = Promise.withResolvers<void>();
    const session = {
        on: (_: string, listener: (typeof parsed)[number]) =>
            void parsed.push(listener),
        detach: () => Promise.resolve(void sent.push("detach")),
        send: (method: string, params?: { scriptId: string }) => {
            sent.push(`${method} ${params?.scriptId ?? ""}`.trim());
            if (method === "Profiler.takePreciseCoverage")
                return profile.promise;
            if (method !== "Debugger.getScriptSource")
                return Promise.resolve({});
            return source.promise.then(() => {
                if (params!.scriptId === "2") throw new Error("No script");
                return { scriptSource: "late();\n" };
            });
        },
    };
    const stop = await startBrowserCoverage(session as unknown as CDPSession);
    for (const [scriptId, url] of [
        ["1", built],
        ["2", refused],
        ["3", "https://example.com/a.js"],
    ])
        for (const listener of parsed)
            listener({ scriptId: scriptId!, url: url! });
    const stopped = stop();
    profile.resolve({
        result: ["1", "2", "3", "4"].map((scriptId) => ({
            scriptId,
            url: {
                1: built,
                2: refused,
                3: "https://example.com/a.js",
            }[scriptId],
            functions,
        })),
    });
    await profile.promise;
    expect(sent).not.toContain("Profiler.stopPreciseCoverage");
    source.resolve();
    expect(await stopped).toEqual([
        { url: built, source: "late();\n", functions },
        { url: refused, source: undefined, functions },
    ]);
    expect(sent).toEqual([
        "Profiler.enable",
        "Profiler.startPreciseCoverage",
        "Debugger.enable",
        "Debugger.setSkipAllPauses",
        "Debugger.getScriptSource 1",
        "Debugger.getScriptSource 2",
        "Profiler.takePreciseCoverage",
        "Profiler.stopPreciseCoverage",
        "detach",
    ]);
});

test("only a script's last line is its map directive, so a directive-shaped line in a later template literal credits nothing", async () => {
    const text = "go();\n";
    await inCheckout(
        { "src/lib/go.ts": text, "src/lib/idle.ts": text },
        async () => {
            const map = (source: string) => ({
                sources: [`turbopack:///[project]/src/lib/${source}.ts`],
                sourcesContent: [text],
                mappings: "AAAA",
            });
            browserScript("idle", text, map("idle"));
            const shadowed = browserScript(
                "go",
                text,
                map("go"),
                4,
                (file) =>
                    `${lineDirective(file)}const later = \`\n${lineDirective("idle.js.map")}\`;\n`,
            );
            const last = browserScript(
                "last",
                text,
                map("go"),
                2,
                (file) => `${lineDirective(file)}\n\n`,
            );
            await addBrowserCoverage([shadowed, last]);
            const records = await scriptRecords(browserCoverage());
            expect(Object.keys(records)).toEqual(["web/src/lib/go.ts"]);
            expect(
                Object.keys(records["web/src/lib/go.ts"]!).map(identityUrl),
            ).toEqual([last.url]);
            expect(lineHits(only(records["web/src/lib/go.ts"]))).toEqual({
                1: 2,
            });
        },
    );
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
    const idle = "const ready = 1;\nidle();\n";
    await inCheckout(
        {
            ...authored,
            "src/app/layout.tsx": layout,
            "src/lib/idle.ts": idle,
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
                sourcesContent: [idle],
                mappings: "AACA",
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
                "DA:2,0",
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
                baseline: {},
            }).coverage["web/src/lib/shared.ts"]!;
            expect(lineHits(combined)).toEqual({ 1: 3 });
        },
    );
});

test("two compilations of a switch in opposite arm order count each authored arm by what ran, at one script address or two", async () => {
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
            idle: string[],
        ) => {
            const generated = order.map((line) => lines[line]!).join("\n");
            const source = `${generated}\n${inlineReference(flatMap("turbopack:///[project]/src/lib/pick.ts", text, mappings))}\n`;
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
                                ...idle.map((arm) => ({
                                    startOffset: generated.indexOf(arm),
                                    endOffset: generated.indexOf(
                                        "\n",
                                        generated.indexOf(arm),
                                    ),
                                    count: 0,
                                })),
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
            [url("b"), url("a")],
        ])
            for (const reversedFirst of [false, true]) {
                rmSync("coverage", { recursive: true, force: true });
                const forward = compilation(
                    [0, 1, 2, 3],
                    "AAAA;AACA;AACA;AACA",
                    first!,
                    ["case 2"],
                );
                const reversed = compilation(
                    [0, 2, 1, 3],
                    "AAAA;AAEA;AADA;AAEA",
                    second!,
                    ["case 1", "case 2"],
                );
                await new CoverageReport(browserCoverage()).add(
                    reversedFirst ? [reversed, forward] : [forward, reversed],
                );
                const records = await scriptRecords(browserCoverage());
                expect(Object.keys(records[name]!)).toHaveLength(2);
                const combined = united({ browser: records });
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

function lockHolder() {
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
    const port = new Promise<number>((resolve, reject) => {
        let output = "";
        child.stdout.on("data", (data: Buffer) => {
            output += data.toString();
            const held = /^held (\d+)\r?\n/.exec(output);
            if (held) resolve(Number(held[1]));
        });
        child.once("exit", () => reject(new Error("The lock holder ended")));
    });
    return { child, exited, port };
}

test("the system frees the coverage lock of a process that was killed", async () => {
    const holder = lockHolder();
    try {
        const port = await holder.port;
        await expect(listening(port)).rejects.toThrow("EADDRINUSE");
        holder.child.kill();
        await holder.exited;
        await closed(await listening(port));
    } finally {
        holder.child.kill();
    }
});

test("the coverage lock closes every connection it accepts", async () => {
    const holder = lockHolder();
    try {
        const client = connect({ host: "127.0.0.1", port: await holder.port });
        const hadError = await new Promise<boolean>((resolve, reject) => {
            client.once("close", resolve);
            client.once("error", reject);
        });
        expect(hadError).toBe(false);
    } finally {
        holder.child.kill();
    }
});
