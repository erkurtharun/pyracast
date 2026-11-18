import { expect, test } from "@playwright/test";
import { mkdirSync, mkdtempSync, readFileSync, rmSync } from "node:fs";
import os from "node:os";
import path from "node:path";
import { pathToFileURL } from "node:url";
import { CoverageReport } from "monocart-coverage-reports";
import {
    authoredSource,
    coverageOptions,
    sourcePath,
} from "../testing/coverage-options";

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
