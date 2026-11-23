import { readdirSync } from "node:fs";
import path from "node:path";
import { CDPClient, CoverageReport } from "monocart-coverage-reports";
import { serverInspector, serverSnapshots } from "../tests/support/server";
import { serverCoverage, serverEntry, serverOutput } from "./coverage-options";

function snapshots(directory: string): Set<string> {
    try {
        return new Set(
            readdirSync(directory).filter(
                (file) =>
                    file.startsWith("coverage-") && file.endsWith(".json"),
            ),
        );
    } catch {
        return new Set();
    }
}

function samePath(a: string, b: string): boolean {
    const [left, right] = [path.resolve(a), path.resolve(b)];
    return process.platform === "win32"
        ? left.toLowerCase() === right.toLowerCase()
        : left === right;
}

export async function ingestServerSnapshots(
    directory: string,
    before: ReadonlySet<string>,
): Promise<void> {
    if (![...snapshots(directory)].some((file) => !before.has(file)))
        throw new Error(
            `The Next server wrote no coverage snapshot to ${directory}`,
        );
    let accepted = 0;
    await new CoverageReport({
        ...serverCoverage(),
        entryFilter: (entry) => {
            const measured = serverEntry(entry.url);
            if (measured) accepted += 1;
            return measured;
        },
    }).addFromDir(directory);
    if (!accepted)
        throw new Error(
            `No entry of the Next server's coverage in ${directory} is a script of ${serverOutput()}`,
        );
}

// Playwright force-stops the server, so flush V8 coverage before shutdown.
export default async function collectServerCoverage(): Promise<void> {
    const { host, port } = serverInspector;
    const client = await CDPClient(serverInspector);
    if (!client) throw new Error(`No Next server inspector at ${host}:${port}`);
    try {
        const answer = (await client.safeSend("Runtime.evaluate", {
            expression: "process.env.NODE_V8_COVERAGE",
            returnByValue: true,
        })) as { result?: { value?: unknown } } | null;
        const recorded = answer?.result?.value;
        if (
            typeof recorded !== "string" ||
            !samePath(recorded, serverSnapshots)
        )
            throw new Error(
                `The inspector at ${host}:${port} is not this run's Next server: it records coverage in ${String(recorded)}, not ${serverSnapshots}`,
            );
        const before = snapshots(serverSnapshots);
        await client.writeCoverage();
        await ingestServerSnapshots(serverSnapshots, before);
    } finally {
        await client.close();
    }
}
