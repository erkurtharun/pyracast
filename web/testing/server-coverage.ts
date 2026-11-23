import { readdirSync, readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { CDPClient, CoverageReport } from "monocart-coverage-reports";
import { serverInspector, serverSnapshots } from "../tests/support/server";
import { fileMap, preparedEntry } from "./coverage-maps";
import { serverCoverage, serverEntry, serverOutput } from "./coverage-options";

type ServerProcess = { pid: number; threadId: number };
type ScriptCoverage = { url: string; functions: unknown[] };

function snapshots(directory: string): Set<string> {
    try {
        return new Set(readdirSync(directory));
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

// Node names a snapshot coverage-<pid>-<milliseconds>-<thread id>.json (src/inspector_profiler.cc, GetFilename).
export function newSnapshot(
    directory: string,
    before: ReadonlySet<string>,
    server: ServerProcess,
): string {
    const name = new RegExp(
        `^coverage-${server.pid}-\\d+-${server.threadId}\\.json$`,
    );
    const written = [...snapshots(directory)].filter(
        (file) => !before.has(file) && name.test(file),
    );
    if (written.length !== 1)
        throw new Error(
            `The Next server (process ${server.pid}) wrote ${written.length ? `${written.length} new coverage snapshots` : "no new coverage snapshot"} to ${directory}`,
        );
    return path.join(directory, written[0]!);
}

export async function ingestServerSnapshot(file: string): Promise<void> {
    const snapshot = JSON.parse(readFileSync(file, "utf8")) as {
        result?: ScriptCoverage[];
    };
    const accepted = (snapshot.result ?? []).filter((entry) =>
        serverEntry(entry.url),
    );
    if (!accepted.length)
        throw new Error(
            `No entry of the Next server's coverage snapshot ${file} is a script of ${serverOutput()}`,
        );
    const entries = accepted.flatMap((entry) => {
        const script = preparedEntry(
            {
                ...entry,
                source: readFileSync(fileURLToPath(entry.url), "utf8"),
            },
            fileMap,
        );
        return script ? [script] : [];
    });
    if (entries.length) await new CoverageReport(serverCoverage()).add(entries);
}

async function evaluated(
    client: NonNullable<Awaited<ReturnType<typeof CDPClient>>>,
    expression: string,
): Promise<unknown> {
    const answer = (await client.safeSend("Runtime.evaluate", {
        expression,
        returnByValue: true,
    })) as { result?: { value?: unknown } } | null;
    return answer?.result?.value;
}

// Playwright force-stops the server, so flush V8 coverage before shutdown.
export default async function collectServerCoverage(): Promise<void> {
    const { host, port } = serverInspector;
    const client = await CDPClient(serverInspector);
    if (!client) throw new Error(`No Next server inspector at ${host}:${port}`);
    try {
        const recorded = await evaluated(
            client,
            "process.env.NODE_V8_COVERAGE",
        );
        if (
            typeof recorded !== "string" ||
            !samePath(recorded, serverSnapshots)
        )
            throw new Error(
                `The inspector at ${host}:${port} is not this run's Next server: it records coverage in ${String(recorded)}, not ${serverSnapshots}`,
            );
        const server = (await evaluated(
            client,
            '({ pid: process.pid, threadId: process.getBuiltinModule("node:worker_threads").threadId })',
        )) as ServerProcess;
        const before = snapshots(serverSnapshots);
        await client.writeCoverage();
        await ingestServerSnapshot(
            newSnapshot(serverSnapshots, before, server),
        );
    } finally {
        await client.close();
    }
}
