import { expect, test } from "@playwright/test";
import { mkdirSync, mkdtempSync, rmSync } from "node:fs";
import os from "node:os";
import path from "node:path";
import { pathToFileURL } from "node:url";
import { authoredSource, sourcePath } from "../testing/coverage-options";

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
