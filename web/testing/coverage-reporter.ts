import { existsSync } from "node:fs";
import { readFile, writeFile } from "node:fs/promises";
import path from "node:path";
import type { Reporter } from "@playwright/test/reporter";
import {
    CoverageReport,
    type CoverageReportOptions,
} from "monocart-coverage-reports";
import {
    combineCoverage,
    executed,
    withoutFallback,
    type CoverageMap,
    type ScriptRecords,
} from "./coverage-combine";
import {
    coverageFigures,
    coverageShortfalls,
    type Floor,
} from "./coverage-floor";
import { authoredSources, baselineEntries } from "./coverage-maps";
import {
    authoredText,
    baselineCoverage,
    browserCoverage,
    combinedCoverage,
    scriptRecordKey,
    serverCoverage,
} from "./coverage-options";

export async function scriptRecords(
    options: CoverageReportOptions,
): Promise<ScriptRecords> {
    await new CoverageReport(options).generate();
    const file = path.join(options.outputDir!, "coverage-final.json");
    const converted = existsSync(file)
        ? (JSON.parse(await readFile(file, "utf8")) as CoverageMap)
        : {};
    const records: ScriptRecords = {};
    for (const [key, coverage] of Object.entries(converted)) {
        const [source, script] = scriptRecordKey(key)!;
        (records[source] ??= {})[script] = withoutFallback(
            coverage,
            authoredText(source)!,
        );
    }
    return records;
}

function ran(records: ScriptRecords): boolean {
    return Object.values(records).some((scripts) =>
        Object.values(scripts).some(executed),
    );
}

async function baselineRecords(): Promise<ScriptRecords> {
    const options = baselineCoverage();
    const entries = baselineEntries();
    if (entries.length) await new CoverageReport(options).add(entries);
    return scriptRecords(options);
}

export default class CoverageReporter implements Reporter {
    async onEnd() {
        const browser = await scriptRecords(browserCoverage());
        const server = await scriptRecords(serverCoverage());
        const idle = Object.entries({ browser, "Next server": server })
            .filter(([, records]) => !ran(records))
            .map(([side]) => side);
        if (idle.length)
            throw new Error(
                `No mapped execution of an authored source in the coverage of: ${idle.join(", ")}`,
            );
        const sources = authoredSources();
        const baseline = await baselineRecords();
        const report = new CoverageReport(combinedCoverage());
        await report.add(
            combineCoverage({ sources, browser, server, baseline }),
        );
        await report.generate();
        const lcov = (await readFile("coverage/lcov.info", "utf8")).replace(
            /^SF:(.+)$/gm,
            (_, file: string) => `SF:${file.trim().replaceAll("\\", "/")}`,
        );
        await writeFile("coverage/lcov.info", lcov);
        const measured = lcov
            .split("\n")
            .filter((line) => line.startsWith("SF:"))
            .map((line) => line.slice(3).trim());
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
