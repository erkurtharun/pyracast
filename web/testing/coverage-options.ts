import { readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import type {
    CoverageReportOptions,
    CoverageResults,
    V8CoverageEntry,
} from "monocart-coverage-reports";
import ts from "typescript";
import { coverageOrigin } from "../tests/support/server";

type SourceMap = { sources?: unknown; sourceRoot?: unknown };

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

function absolutePath(value: string): string | undefined {
    if (value.startsWith("file:")) return filePathOf(value);
    return path.isAbsolute(value)
        ? path.resolve(value).replaceAll("\\", "/")
        : undefined;
}

// Monocart passes its own normalised path first; the URL in `info` is what the input named.
export function sourcePath(
    filePath: string,
    info?: Record<string, unknown>,
): string {
    const named = (
        typeof info?.url === "string" && info.url ? info.url : filePath
    ).replaceAll("\\", "/");
    const absolute = absolutePath(named);
    if (absolute !== undefined) {
        const root = path.resolve("src");
        return startsWithDirectory(absolute, root)
            ? `web/src/${absolute.slice(root.length + 1)}`
            : absolute;
    }
    const prefix = ["web/src/", "turbopack:///[project]/src/"].find((value) =>
        named.startsWith(value),
    );
    return prefix ? `web/src/${named.slice(prefix.length)}` : named;
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

export function authoredText(name: string): string | undefined {
    try {
        return readFileSync(
            path.resolve("src", name.slice("web/src/".length)),
            "utf8",
        ).replace(/^\uFEFF/, "");
    } catch {
        return undefined;
    }
}

export function serverOutput(): string {
    return path.resolve(process.env.NEXT_DIST_DIR ?? ".next", "server");
}

export function browserEntry(url: string): boolean {
    if (!URL.canParse(url)) return false;
    const parsed = new URL(url);
    return (
        parsed.origin === coverageOrigin &&
        parsed.pathname.startsWith("/_next/static/")
    );
}

export function serverEntry(url: string): boolean {
    const filePath = url.startsWith("file:") ? filePathOf(url) : undefined;
    return !!filePath && startsWithDirectory(filePath, serverOutput());
}

export function withResolvedSources<T>(map: T, mapUrl: string): T {
    const parts = (map as { sections?: { map?: SourceMap }[] }).sections?.map(
        (section) => section.map,
    ) ?? [map as SourceMap];
    for (const part of parts) {
        if (!part || !Array.isArray(part.sources)) continue;
        const root =
            typeof part.sourceRoot === "string" && part.sourceRoot
                ? new URL(part.sourceRoot.replace(/\/?$/, "/"), mapUrl)
                : mapUrl;
        part.sources = part.sources.map((source: unknown) =>
            typeof source === "string" && !/^[a-z][a-z\d+.-]*:/i.test(source)
                ? new URL(source, root).href
                : source,
        );
        delete part.sourceRoot;
    }
    return map;
}

function requireAuthoredTexts(results?: CoverageResults): void {
    const named = new Set<string>();
    for (const file of results?.files ?? []) {
        if (named.has(file.sourcePath))
            throw new Error(
                `Coverage inputs name ${file.sourcePath} with different texts`,
            );
        named.add(file.sourcePath);
        if (file.source !== authoredText(file.sourcePath))
            throw new Error(
                `Coverage text for ${file.sourcePath} is not the authored file`,
            );
    }
}

function sharedOptions(outputDir: string): CoverageReportOptions {
    return {
        name: "Pyracast frontend coverage",
        outputDir,
        baseDir: path.resolve(".."),
        clean: false,
        v8Ignore: false,
        sourceFilter: authoredSource,
        outputFilter: (entry) => authoredSource(entry.sourcePath),
        sourcePath,
    };
}

function measuredSide(outputDir: string): CoverageReportOptions {
    return {
        ...sharedOptions(outputDir),
        reports: ["json"],
        sourceMapResolver: async (url, load) =>
            withResolvedSources(
                await (load as (url: string) => Promise<unknown>)(url),
                url,
            ),
        onEnd: requireAuthoredTexts,
    };
}

export function browserCoverage(): CoverageReportOptions {
    return {
        ...measuredSide("coverage/browser"),
        entryFilter: (entry) => browserEntry(entry.url),
    };
}

export function serverCoverage(): CoverageReportOptions {
    return {
        ...measuredSide("coverage/server"),
        entryFilter: (entry) => serverEntry(entry.url),
        // Node's cached map would bypass sourceMapResolver; the library reloads it from the chunk's sourceMappingURL.
        onEntry: (entry) => {
            delete entry.sourceMap;
        },
    };
}

export function baselineCoverage(): CoverageReportOptions {
    return {
        ...sharedOptions("coverage/baseline"),
        reports: ["json"],
        all: {
            dir: "src",
            filter: authoredSource,
            transformer: (entry: V8CoverageEntry) => {
                if (!entry.source)
                    throw new Error(`Missing source: ${entry.url}`);
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
}

export function combinedCoverage(): CoverageReportOptions {
    return {
        ...sharedOptions("coverage"),
        reports: ["lcovonly", "console-details"],
    };
}
