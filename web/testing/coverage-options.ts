import { readFileSync, realpathSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import type { CoverageReportOptions } from "monocart-coverage-reports";
import { coverageOrigin } from "../tests/support/server";

const scriptSeparator = "#";
const turbopackProject = "turbopack:///[project]/";

function physical(filePath: string): string | undefined {
    try {
        return realpathSync.native(filePath);
    } catch {
        return undefined;
    }
}

function within(filePath: string, directory: string): string | undefined {
    const relative = path.relative(directory, filePath);
    return relative &&
        relative !== ".." &&
        !relative.startsWith(`..${path.sep}`) &&
        !path.isAbsolute(relative)
        ? relative.replaceAll("\\", "/")
        : undefined;
}

function sourceRoot(): string {
    const root = path.resolve("src");
    return physical(root) ?? root;
}

function authoredName(relative: string): boolean {
    return (
        /\.tsx?$/.test(relative) &&
        !relative.endsWith(".d.ts") &&
        !relative.split("/").includes("node_modules")
    );
}

export function authoredSource(key: string): boolean {
    return key.startsWith("web/src/") && authoredName(key.slice(8));
}

export function authoredText(key: string): string | undefined {
    try {
        return readFileSync(path.resolve("src", key.slice(8)), "utf8").replace(
            /^\uFEFF/,
            "",
        );
    } catch {
        return undefined;
    }
}

// Turbopack's [project] is the web directory: `turbopack.root` in the build's required-server-files.json.
export function authoredFile(url: string): string | undefined {
    let candidate: string;
    if (url.startsWith(turbopackProject)) {
        candidate = path.join(
            path.dirname(sourceRoot()),
            ...decodeURIComponent(url.slice(turbopackProject.length)).split(
                "/",
            ),
        );
    } else if (url.startsWith("file:")) {
        try {
            candidate = fileURLToPath(url);
        } catch {
            return undefined;
        }
    } else {
        return undefined;
    }
    const file = physical(candidate);
    const relative = file && within(file, sourceRoot());
    return relative && authoredName(relative) ? file : undefined;
}

export function reportKey(file: string): string {
    const relative = within(file, sourceRoot());
    if (!relative) throw new Error(`${file} is not an authored source`);
    return `web/src/${relative}`;
}

export function scriptRecordKey(key: string): [string, string] | undefined {
    const at = key.lastIndexOf(scriptSeparator);
    if (at < 0 || !authoredSource(key.slice(0, at))) return undefined;
    return [key.slice(0, at), decodeURIComponent(key.slice(at + 1))];
}

// Called for each generated script, each source of its map (`distFile` set) and each converted Istanbul record.
function sourcePath(filePath: string, info?: Record<string, unknown>): string {
    if (info && "statementMap" in info && typeof info.path === "string")
        return info.path;
    const url = typeof info?.url === "string" ? info.url : filePath;
    if (typeof info?.distFile !== "string") return url;
    const file = authoredFile(url);
    return file
        ? `${reportKey(file)}${scriptSeparator}${encodeURIComponent(info.distFile)}`
        : url;
}

export function distDir(): string {
    return path.resolve(process.env.NEXT_DIST_DIR ?? ".next");
}

export function serverOutput(): string {
    const output = path.join(distDir(), "server");
    return physical(output) ?? output;
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
    if (!url.startsWith("file:")) return false;
    let filePath: string;
    try {
        filePath = fileURLToPath(url);
    } catch {
        return false;
    }
    return !!within(physical(filePath) ?? filePath, serverOutput());
}

function sideOptions(outputDir: string): CoverageReportOptions {
    return {
        name: "Pyracast frontend coverage",
        outputDir,
        baseDir: path.resolve(".."),
        clean: false,
        v8Ignore: false,
        reports: ["json"],
        sourceFilter: (key) => !!scriptRecordKey(key),
        outputFilter: (entry) => !!scriptRecordKey(entry.sourcePath),
        sourcePath,
    };
}

export function browserCoverage(): CoverageReportOptions {
    return {
        ...sideOptions("coverage/browser"),
        entryFilter: (entry) => browserEntry(entry.url),
    };
}

export function serverCoverage(): CoverageReportOptions {
    return {
        ...sideOptions("coverage/server"),
        entryFilter: (entry) => serverEntry(entry.url),
    };
}

export function baselineCoverage(): CoverageReportOptions {
    return sideOptions("coverage/baseline");
}

export function combinedCoverage(): CoverageReportOptions {
    return {
        name: "Pyracast frontend coverage",
        outputDir: "coverage",
        baseDir: path.resolve(".."),
        clean: false,
        reports: ["lcovonly", "console-details"],
        sourcePath,
    };
}
