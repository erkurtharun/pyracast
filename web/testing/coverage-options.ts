import { createHash } from "node:crypto";
import { readFileSync, realpathSync } from "node:fs";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import type { CoverageReportOptions } from "monocart-coverage-reports";
import { coverageOrigin } from "../tests/support/server";

const scriptSeparator = "#";
const identitySeparator = " ";
const turbopackProject = "turbopack:///[project]/";
const staticPrefix = "/_next/static/";

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
            /^\u{FEFF}/u,
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

const mapDigests = new WeakMap<object, string>();

function digest(text: string): string {
    return createHash("sha256").update(text).digest("hex");
}

function scriptIdentity(
    url: string,
    source: string,
    sourceMap: object,
): string {
    let map = mapDigests.get(sourceMap);
    if (!map) {
        map = digest(JSON.stringify(sourceMap));
        mapDigests.set(sourceMap, map);
    }
    return `${url}${identitySeparator}${digest(`${digest(source)}${map}`)}`;
}

export function identityUrl(identity: string): string {
    return identity.slice(0, identity.lastIndexOf(identitySeparator));
}

// Called for each generated script, each source of its map (`distFile` set) and each converted Istanbul record.
function sourcePath(filePath: string, info?: Record<string, unknown>): string {
    if (info && "statementMap" in info && typeof info.path === "string")
        return info.path;
    const url = typeof info?.url === "string" ? info.url : filePath;
    if (typeof info?.distFile === "string") {
        const file = authoredFile(url);
        return file
            ? `${reportKey(file)}${scriptSeparator}${encodeURIComponent(info.distFile)}`
            : url;
    }
    if (
        typeof info?.source === "string" &&
        typeof info.sourceMap === "object" &&
        info.sourceMap
    )
        return scriptIdentity(url, info.source, info.sourceMap);
    return url;
}

export function distDir(): string {
    return path.resolve(process.env.NEXT_DIST_DIR ?? ".next");
}

export type BuildPart = "static" | "server";

export function buildRoot(part: BuildPart): string {
    const root = path.join(distDir(), part);
    return physical(root) ?? root;
}

export function serverOutput(): string {
    return buildRoot("server");
}

export function browserEntry(url: string): boolean {
    if (!URL.canParse(url)) return false;
    const parsed = new URL(url);
    return (
        parsed.origin === coverageOrigin &&
        parsed.pathname.startsWith(staticPrefix)
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

export function builtUrl(part: BuildPart, relative: string): string {
    return part === "static"
        ? `${coverageOrigin}${staticPrefix}${relative.split(/[\\/]/).map(encodeURIComponent).join("/")}`
        : pathToFileURL(path.join(buildRoot(part), relative)).href;
}

function candidatePath(url: string, part: BuildPart): string {
    if (part === "server") return fileURLToPath(url);
    if (!browserEntry(url))
        throw new Error(`${url} is not served from this run's build`);
    const segments = new URL(url).pathname
        .slice(staticPrefix.length)
        .split("/")
        .map(decodeURIComponent);
    if (segments.some((segment) => /^\.{0,2}$|[\\/]/.test(segment)))
        throw new Error(`${url} does not name a file of this run's build`);
    return path.join(buildRoot(part), ...segments);
}

export function builtFile(url: string, part: BuildPart): string {
    const file = physical(candidatePath(url, part));
    if (!file || !within(file, buildRoot(part)))
        throw new Error(`${url} is not a file of ${buildRoot(part)}`);
    return file;
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
