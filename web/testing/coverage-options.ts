import path from "node:path";
import { fileURLToPath } from "node:url";
import type {
    CoverageReportOptions,
    V8CoverageEntry,
} from "monocart-coverage-reports";
import ts from "typescript";

export function sourcePath(filePath: string): string {
    let normalized = filePath.replaceAll("\\", "/");
    if (normalized.startsWith("file:")) {
        try {
            normalized = fileURLToPath(normalized).replaceAll("\\", "/");
        } catch {
            return normalized;
        }
    }
    const root = `${path.resolve("src").replaceAll("\\", "/")}/`;
    const prefixes = [
        root,
        "src/",
        "web/src/",
        "[project]/src/",
        "turbopack:///[project]/src/",
        "turbopack/[project]/src/",
    ];
    const prefix = prefixes.find((value) =>
        process.platform === "win32" && value === root
            ? normalized.toLowerCase().startsWith(value.toLowerCase())
            : normalized.startsWith(value),
    );
    return prefix ? `web/src/${normalized.slice(prefix.length)}` : normalized;
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
    entryFilter: (entry) => entry.url.includes("/_next/static/"),
    sourceFilter: authoredSource,
    sourcePath,
    all: {
        dir: "src",
        filter: authoredSource,
        transformer: async (entry: V8CoverageEntry) => {
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
        },
    },
};
