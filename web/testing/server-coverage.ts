import { readdirSync, readFileSync } from "node:fs";
import path from "node:path";
import { CDPClient, CoverageReport } from "monocart-coverage-reports";
import {
    coverageRun,
    serverInspector,
    serverSnapshots,
} from "../tests/support/server";
import { preparedEntry, serverMap } from "./coverage-maps";
import {
    builtFile,
    serverCoverage,
    serverEntry,
    serverOutput,
} from "./coverage-options";

type ServerProcess = { pid: number; threadId: number };
type ScriptCoverage = { url: string; functions: unknown[] };

function samePath(a: string, b: string): boolean {
    const [left, right] = [path.resolve(a), path.resolve(b)];
    return process.platform === "win32"
        ? left.toLowerCase() === right.toLowerCase()
        : left === right;
}

export function serverProcess(answer: unknown): ServerProcess {
    const { pid, threadId } = (answer ?? {}) as Record<string, unknown>;
    if (
        !Number.isInteger(pid) ||
        (pid as number) <= 0 ||
        !Number.isInteger(threadId) ||
        (threadId as number) < 0
    )
        throw new Error(
            `The Next server inspector answered no process identity: ${JSON.stringify(answer)}`,
        );
    return { pid: pid as number, threadId: threadId as number };
}

// Node names a snapshot coverage-<pid>-<milliseconds>-<thread id>.json (src/inspector_profiler.cc, GetFilename).
export function runSnapshot(directory: string, server: ServerProcess): string {
    let files: string[];
    try {
        files = readdirSync(directory);
    } catch (error) {
        if ((error as NodeJS.ErrnoException).code !== "ENOENT") throw error;
        files = [];
    }
    const name = new RegExp(
        `^coverage-${server.pid}-\\d+-${server.threadId}\\.json$`,
    );
    const written = files.filter((file) => name.test(file));
    if (written.length !== 1)
        throw new Error(
            `The Next server (process ${server.pid}) wrote ${written.length ? `${written.length} coverage snapshots` : "no coverage snapshot"} to ${directory}`,
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
                source: readFileSync(builtFile(entry.url, "server"), "utf8"),
            },
            serverMap,
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
    const directory = serverSnapshots(coverageRun()!);
    const { host, port } = serverInspector;
    const client = await CDPClient(serverInspector);
    if (!client) throw new Error(`No Next server inspector at ${host}:${port}`);
    try {
        const recorded = await evaluated(
            client,
            "process.env.NODE_V8_COVERAGE",
        );
        if (typeof recorded !== "string" || !samePath(recorded, directory))
            throw new Error(
                `The inspector at ${host}:${port} is not this run's Next server: it records coverage in ${String(recorded)}, not ${directory}`,
            );
        const server = serverProcess(
            await evaluated(
                client,
                '({ pid: process.pid, threadId: process.getBuiltinModule("node:worker_threads").threadId })',
            ),
        );
        await client.writeCoverage();
        await ingestServerSnapshot(runSnapshot(directory, server));
    } finally {
        await client.close();
    }
}
