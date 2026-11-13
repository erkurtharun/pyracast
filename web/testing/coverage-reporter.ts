import { readdir, readFile, writeFile } from "node:fs/promises";
import type { Reporter } from "@playwright/test/reporter";
import { CoverageReport } from "monocart-coverage-reports";
import {
    authoredSource,
    coverageOptions,
    sourcePath,
} from "./coverage-options";

export default class CoverageReporter implements Reporter {
    async onEnd() {
        await new CoverageReport(coverageOptions).generate();
        const lcov = (await readFile("coverage/lcov.info", "utf8")).replace(
            /^SF:(.+)$/gm,
            (_, filePath: string) => `SF:${sourcePath(filePath.trim())}`,
        );
        await writeFile("coverage/lcov.info", lcov);
        const measured = lcov
            .split("\n")
            .filter((line) => line.startsWith("SF:"))
            .map((line) => line.slice(3).trim());
        const sources = (await readdir("src", { recursive: true }))
            .map((file) => sourcePath(`src/${file}`))
            .filter(authoredSource);
        if (
            measured.length !== new Set(measured).size ||
            measured.length !== sources.length ||
            sources.some((file) => !measured.includes(file))
        ) {
            throw new Error(
                "LCOV must contain every authored TypeScript source exactly once",
            );
        }
        if (!/^DA:\d+,[1-9]\d*/m.test(lcov)) {
            throw new Error("LCOV contains no measured execution");
        }
    }
}
