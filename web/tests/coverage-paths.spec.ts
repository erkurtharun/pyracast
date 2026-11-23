import { expect, test } from "@playwright/test";
import {
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
import {
    authoredSource,
    coverageOptions,
    measuredEntry,
    serverOutput,
    sourcePath,
} from "../testing/coverage-options";

const wholeScript = (source: string, count: number) => [
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
        if (file) records.set(sourcePath(file.trim()), record);
    }
    return records;
}

test("coverage identifies only authored sources from known path forms", () => {
    const source = path.resolve("src", "lib", "api.ts");
    for (const candidate of [
        source,
        source.replaceAll("/", "\\"),
        pathToFileURL(source).href,
        "src/lib/api.ts",
        "web/src/lib/api.ts",
        "[project]/src/lib/api.ts",
        "turbopack:///[project]/src/lib/api.ts",
        "turbopack/[project]/src/lib/api.ts",
    ]) {
        expect(sourcePath(candidate), candidate).toBe("web/src/lib/api.ts");
        expect(authoredSource(candidate), candidate).toBe(true);
    }
    for (const candidate of [
        path.resolve("vendor/src/lib/api.ts"),
        "C:/src/pyracast/vendor/src/lib/api.ts",
        "turbopack:///[project]/node_modules/next/src/lib/api.ts",
        "[project]/node_modules/next/src/lib/api.ts",
        "webpack://other/src/lib/api.ts",
        "web/src/../vendor/api.ts",
        "src/lib/schema.d.ts",
    ]) {
        expect(authoredSource(candidate), candidate).toBe(false);
    }
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

test("LCOV records authored execution once when a generated chunk has no source map", async () => {
    const temporary = mkdtempSync(path.join(os.tmpdir(), "pyracast-lcov-"));
    const source = "const observed = 1;\n";
    const generated = "globalThis.chunkLoaded = true;\n";
    try {
        const report = new CoverageReport({
            ...coverageOptions,
            outputDir: temporary,
            reports: ["lcovonly"],
            all: undefined,
        });
        await report.add([
            {
                url: "http://127.0.0.1/_next/static/authored.js",
                source,
                sourceMap: {
                    version: 3,
                    sources: ["src/lib/coverage-fixture.ts"],
                    sourcesContent: [source],
                    names: [],
                    mappings: "AAAA",
                },
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
                        ],
                    },
                ],
            },
            {
                url: "http://127.0.0.1/_next/static/generated.js",
                source: generated,
                functions: [
                    {
                        functionName: "",
                        isBlockCoverage: true,
                        ranges: [
                            {
                                startOffset: 0,
                                endOffset: generated.length,
                                count: 1,
                            },
                        ],
                    },
                ],
            },
        ]);
        await report.generate();
        const lcov = readFileSync(path.join(temporary, "lcov.info"), "utf8");
        const files = (lcov.match(/^SF:.+$/gm) ?? []).map((entry) =>
            sourcePath(entry.slice(3)),
        );
        expect(files).toEqual(["web/src/lib/coverage-fixture.ts"]);
        expect(lcov).toContain("DA:1,1");
    } finally {
        rmSync(temporary, { recursive: true, force: true });
    }
});

test("coverage measures browser chunks and the Next server bundles only", () => {
    const server = serverOutput();
    for (const url of [
        "http://127.0.0.1:3102/_next/static/chunks/app.js",
        pathToFileURL(path.join(server, "chunks", "ssr", "page.js")).href,
        pathToFileURL(path.join(server, "app", "layout.js")).href,
    ]) {
        expect(measuredEntry(url), url).toBe(true);
    }
    for (const url of [
        pathToFileURL(path.resolve("node_modules/next/dist/server/next.js"))
            .href,
        pathToFileURL(`${server}-other${path.sep}page.js`).href,
        pathToFileURL(path.resolve("testing/coverage-options.ts")).href,
        "node:internal/main/run_main_module",
        "file://remote-host/page.js",
        "",
    ]) {
        expect(measuredEntry(url), url).toBe(false);
    }
});

test("server coverage from the V8 directory merges with browser coverage into one record per source", async () => {
    const original = process.cwd();
    const temporary = mkdtempSync(path.join(os.tmpdir(), "pyracast-server-"));
    const layout = "export const layout = 1;\n";
    const shared = "export const shared = 1;\n";
    try {
        const web = path.join(temporary, "web");
        mkdirSync(web);
        process.chdir(web);
        const chunkDir = path.join(serverOutput(), "chunks", "ssr");
        mkdirSync(chunkDir, { recursive: true });
        const chunk = `globalThis.layout = 1;\nglobalThis.shared = 1;\n//# sourceMappingURL=page.js.map\n`;
        writeFileSync(path.join(chunkDir, "page.js"), chunk);
        writeFileSync(
            path.join(chunkDir, "page.js.map"),
            JSON.stringify({
                version: 3,
                sources: [
                    "../../../../src/app/layout.tsx",
                    "../../../../src/lib/shared.ts",
                ],
                sourcesContent: [layout, shared],
                names: [],
                mappings: "AAAA;ACAA",
            }),
        );
        const v8Dir = path.join(temporary, "v8");
        mkdirSync(v8Dir);
        writeFileSync(
            path.join(v8Dir, "coverage-1-1-0.json"),
            JSON.stringify({
                result: [
                    {
                        scriptId: "1",
                        url: pathToFileURL(path.join(chunkDir, "page.js")).href,
                        functions: wholeScript(chunk, 1),
                    },
                    {
                        scriptId: "2",
                        url: pathToFileURL(
                            path.resolve(
                                "node_modules/next/dist/server/next.js",
                            ),
                        ).href,
                        functions: wholeScript("x", 1),
                    },
                ],
            }),
        );
        const report = new CoverageReport({
            ...coverageOptions,
            outputDir: path.join(temporary, "report"),
            reports: ["lcovonly"],
            all: undefined,
        });
        await report.addFromDir(v8Dir);
        await report.add([
            {
                url: "http://127.0.0.1/_next/static/chunks/shared.js",
                source: shared,
                sourceMap: {
                    version: 3,
                    sources: ["turbopack:///[project]/src/lib/shared.ts"],
                    sourcesContent: [shared],
                    names: [],
                    mappings: "AAAA",
                },
                functions: wholeScript(shared, 1),
            },
        ]);
        await report.generate();
        const lcov = readFileSync(
            path.join(temporary, "report", "lcov.info"),
            "utf8",
        );
        const files = (lcov.match(/^SF:.+$/gm) ?? []).map((entry) =>
            sourcePath(entry.slice(3)),
        );
        expect(files.sort()).toEqual([
            "web/src/app/layout.tsx",
            "web/src/lib/shared.ts",
        ]);
        const records = lcovRecords(lcov);
        expect(records.get("web/src/app/layout.tsx")).toMatch(/^DA:1,1$/m);
        expect(records.get("web/src/lib/shared.ts")).toMatch(/^DA:1,2$/m);
    } finally {
        process.chdir(original);
        rmSync(temporary, { recursive: true, force: true });
    }
});
