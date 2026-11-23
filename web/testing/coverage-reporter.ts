import { existsSync } from "node:fs";
import { readFile, writeFile } from "node:fs/promises";
import path from "node:path";
import type { Reporter } from "@playwright/test/reporter";
import {
    CoverageReport,
    type CoverageReportOptions,
} from "monocart-coverage-reports";
import {
    branchEvidence,
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
import {
    authoredSources,
    baselineEntries,
    convertedText,
} from "./coverage-maps";
import {
    authoredText,
    baselineCoverage,
    browserCoverage,
    browserEntry,
    combinedCoverage,
    identityUrl,
    scriptRecordKey,
    serverCoverage,
    serverEntry,
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
            convertedText(authoredText(source)!),
        );
    }
    return records;
}

function ran(records: ScriptRecords): boolean {
    return Object.values(records).some((scripts) =>
        Object.values(scripts).some(executed),
    );
}

async function baselineRecords(sources: string[]): Promise<ScriptRecords[]> {
    const options = baselineCoverage();
    const entries = baselineEntries(sources);
    if (entries.length) await new CoverageReport(options).add(entries);
    const kinds: ScriptRecords[] = [{}, {}, {}];
    for (const [source, scripts] of Object.entries(
        await scriptRecords(options),
    ))
        for (const [script, record] of Object.entries(scripts)) {
            const url = identityUrl(script);
            const kind = browserEntry(url) ? 0 : serverEntry(url) ? 1 : 2;
            (kinds[kind]![source] ??= {})[script] = record;
        }
    return kinds;
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
        const baseline = await baselineRecords(sources);
        const combined = combineCoverage({
            sources,
            browser,
            server,
            baseline,
        });
        const report = new CoverageReport(combinedCoverage());
        await report.add(combined);
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
        const unique = new Set(measured);
        if (
            measured.length !== unique.size ||
            measured.length !== sources.length ||
            sources.some((file) => !unique.has(file))
        ) {
            throw new Error(
                "LCOV must contain every authored TypeScript source exactly once",
            );
        }
        console.log(
            `Measured frontend coverage: ${JSON.stringify(coverageFigures(lcov))}`,
        );
        console.log(
            `Branch evidence outside the selected tables: ${JSON.stringify(branchEvidence({ browser, server }, combined))}`,
        );
        const floor = JSON.parse(
            await readFile("coverage-floor.json", "utf8"),
        ) as Floor;
        const shortfalls = coverageShortfalls(lcov, floor);
        if (shortfalls.length)
            throw new Error(`Frontend coverage fell: ${shortfalls.join("; ")}`);
    }
}
