import path from "node:path";
import { fileURLToPath } from "node:url";
import type {
    CoverageReportOptions,
    V8CoverageEntry,
} from "monocart-coverage-reports";
import ts from "typescript";

function startsWithDirectory(filePath: string, directory: string): boolean {
    const root = `${directory.replaceAll("\\", "/")}/`;
    return process.platform === "win32"
        ? filePath.toLowerCase().startsWith(root.toLowerCase())
        : filePath.startsWith(root);
}

function filePathOf(url: string): string | undefined {
    try {
        return fileURLToPath(url).replaceAll("\\", "/");
    } catch {
        return undefined;
    }
}

export function sourcePath(filePath: string): string {
    let normalized = filePath.replaceAll("\\", "/");
    if (normalized.startsWith("file:")) {
        const resolved = filePathOf(normalized);
        if (!resolved) return normalized;
        normalized = resolved;
    }
    const root = path.resolve("src");
    if (startsWithDirectory(normalized, root))
        return `web/src/${normalized.slice(root.length + 1)}`;
    const prefix = [
        "src/",
        "web/src/",
        "[project]/src/",
        "turbopack:///[project]/src/",
        "turbopack/[project]/src/",
    ].find((value) => normalized.startsWith(value));
    return prefix ? `web/src/${normalized.slice(prefix.length)}` : normalized;
}

export function serverOutput(): string {
    return path.resolve(process.env.NEXT_DIST_DIR ?? ".next", "server");
}

export function measuredEntry(url: string): boolean {
    if (url.includes("/_next/static/")) return true;
    const filePath = url.startsWith("file:") ? filePathOf(url) : undefined;
    return !!filePath && startsWithDirectory(filePath, serverOutput());
}

export function authoredSource(filePath: string): boolean {
    const normalized = sourcePath(filePath);
    return (
        normalized.startsWith("web/src/") &&
        normalized
            .slice("web/src/".length)
            .split("/")
            .every(
                (part) =>
                    part !== "" &&
                    part !== "." &&
                    part !== ".." &&
                    part !== "node_modules",
            ) &&
        /\.tsx?$/.test(normalized) &&
        !normalized.endsWith(".d.ts")
    );
}

export const coverageOptions: CoverageReportOptions = {
    name: "Pyracast frontend coverage",
    outputDir: "coverage",
    baseDir: path.resolve(".."),
    clean: false,
    reports: ["v8", "lcovonly", "console-details"],
    v8Ignore: false,
    entryFilter: (entry) => measuredEntry(entry.url),
    sourceFilter: authoredSource,
    outputFilter: (entry) => authoredSource(entry.sourcePath),
    sourcePath,
    all: {
        dir: "src",
        filter: authoredSource,
        transformer: (entry: V8CoverageEntry) => {
            if (!entry.source) throw new Error(`Missing source: ${entry.url}`);
            const output = ts.transpileModule(entry.source, {
                fileName: fileURLToPath(entry.url),
                compilerOptions: {
                    target: ts.ScriptTarget.ESNext,
                    module: ts.ModuleKind.ESNext,
                    jsx: ts.JsxEmit.ReactJSX,
                    sourceMap: true,
                    inlineSources: true,
                },
            });
            if (!output.sourceMapText)
                throw new Error(`Missing map: ${entry.url}`);
            entry.source = output.outputText;
            entry.sourceMap = JSON.parse(output.sourceMapText);
            entry.sourceMap.sources = [entry.url];
            return Promise.resolve();
        },
    },
};
