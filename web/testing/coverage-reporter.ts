import { existsSync } from "node:fs";
import { readdir, readFile, writeFile } from "node:fs/promises";
import path from "node:path";
import type { Reporter } from "@playwright/test/reporter";
import {
    CoverageReport,
    type CoverageReportOptions,
} from "monocart-coverage-reports";
import {
    authoredSource,
    authoredText,
    baselineCoverage,
    browserCoverage,
    combinedCoverage,
    serverCoverage,
    sourcePath,
} from "./coverage-options";
import {
    combineCoverage,
    executed,
    withoutInventedStatements,
    type CoverageMap,
} from "./coverage-combine";
import {
    coverageFigures,
    coverageShortfalls,
    type Floor,
} from "./coverage-floor";

async function convert(options: CoverageReportOptions): Promise<CoverageMap> {
    await new CoverageReport(options).generate();
    const file = path.join(options.outputDir!, "coverage-final.json");
    return existsSync(file)
        ? (JSON.parse(await readFile(file, "utf8")) as CoverageMap)
        : {};
}

async function measuredSide(
    name: string,
    options: CoverageReportOptions,
): Promise<CoverageMap> {
    const side = Object.fromEntries(
        Object.entries(await convert(options)).map(([source, file]) => [
            source,
            withoutInventedStatements(file, authoredText(source)!),
        ]),
    );
    if (!Object.values(side).some(executed))
        throw new Error(
            `The ${name} coverage maps no execution to an authored source`,
        );
    return side;
}

export default class CoverageReporter implements Reporter {
    async onEnd() {
        const browser = await measuredSide("browser", browserCoverage());
        const server = await measuredSide("Next server", serverCoverage());
        const baseline = await convert(baselineCoverage());
        const report = new CoverageReport(combinedCoverage());
        await report.add(combineCoverage({ browser, server, baseline }));
        await report.generate();
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
            .map((file) => `web/src/${file.replaceAll("\\", "/")}`)
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
        console.log(
            `Measured frontend coverage: ${JSON.stringify(coverageFigures(lcov))}`,
        );
        const floor = JSON.parse(
            await readFile("coverage-floor.json", "utf8"),
        ) as Floor;
        const shortfalls = coverageShortfalls(lcov, floor);
        if (shortfalls.length)
            throw new Error(`Frontend coverage fell: ${shortfalls.join("; ")}`);
    }
}
