import { readdirSync, readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import { CoverageReport } from "monocart-coverage-reports";
import ts from "typescript";
import { coverageOrigin } from "../tests/support/server";
import {
    authoredFile,
    authoredSource,
    authoredText,
    browserCoverage,
    browserEntry,
    distDir,
    reportKey,
} from "./coverage-options";

type Offset = { line: number; column: number };
type RawMap = {
    sources?: unknown;
    sourcesContent?: unknown;
    sourceRoot?: unknown;
    names?: unknown;
    mappings?: unknown;
    sections?: { offset: Offset; map: RawMap }[];
};
type Segment = number[];
export type PreparedMap = {
    version: 3;
    sources: string[];
    sourcesContent: (string | null)[];
    names: string[];
    mappings?: string;
    sections?: unknown;
    decodedMappings?: Segment[][];
};
type Script = { url: string; source?: string };
export type MapLoader = (mapUrl: string) => string;

// Ends every authored text handed to the converter, so its whole-text statement for an unmapped source ends past the authored lines.
export const authoredEnd = "// end of the authored text";

const directive = /^[ \t]*\/\/[#@][ \t]+sourceMappingURL=([^\s'"`]+)[ \t]*$/gm;
const vlq = new Map(
    [..."ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"].map(
        (digit, value) => [digit, value],
    ),
);

function decodeMappings(mappings: string): Segment[][] {
    const state = [0, 0, 0, 0, 0];
    return mappings.split(";").map((line) => {
        state[0] = 0;
        return line
            .split(",")
            .filter(Boolean)
            .map((segment) => {
                const deltas: number[] = [];
                let value = 0;
                let shift = 0;
                for (const digit of segment) {
                    const bits = vlq.get(digit);
                    if (bits === undefined)
                        throw new Error(
                            `Invalid source map segment ${segment}`,
                        );
                    value += (bits & 31) * 2 ** shift;
                    if (bits & 32) {
                        shift += 5;
                        continue;
                    }
                    deltas.push(value % 2 ? -(value - 1) / 2 : value / 2);
                    value = 0;
                    shift = 0;
                }
                return deltas.map((delta, index) => (state[index]! += delta));
            });
    });
}

function resolvedNames(map: RawMap, mapUrl: string): string[] {
    if (!Array.isArray(map.sources))
        throw new Error(`Map ${mapUrl} has no sources`);
    const root =
        typeof map.sourceRoot === "string" && map.sourceRoot
            ? new URL(map.sourceRoot.replace(/\/?$/, "/"), mapUrl).href
            : mapUrl;
    return map.sources.map((name: unknown, index) => {
        if (typeof name !== "string") return `unnamed:${index}`;
        return /^[a-z][a-z\d+.-]*:/i.test(name)
            ? name
            : new URL(name, root).href;
    });
}

type Flat = {
    sources: string[];
    sourcesContent: (string | null)[];
    names: string[];
    decodedMappings?: Segment[][];
};

// Mirrors monocart's flattenSourceMaps, which it applies only to maps it loads itself.
function flatten(
    map: RawMap,
    mapUrl: string,
    flat: Flat,
    start: Offset,
    stop: Offset,
): void {
    if (map.sections) {
        map.sections.forEach(({ offset, map: part }, index) => {
            const next = map.sections![index + 1]?.offset;
            const end = next
                ? {
                      line: Math.min(stop.line, start.line + next.line),
                      column: start.column + next.column,
                  }
                : stop;
            flatten(
                part,
                mapUrl,
                flat,
                {
                    line: start.line + offset.line,
                    column: start.column + offset.column,
                },
                end.line === stop.line
                    ? {
                          line: end.line,
                          column: Math.min(stop.column, end.column),
                      }
                    : end,
            );
        });
        return;
    }
    const sourcesOffset = flat.sources.length;
    const namesOffset = flat.names.length;
    const contents = Array.isArray(map.sourcesContent)
        ? map.sourcesContent
        : [];
    resolvedNames(map, mapUrl).forEach((name, index) => {
        flat.sources.push(name);
        flat.sourcesContent.push(
            typeof contents[index] === "string" ? contents[index] : null,
        );
    });
    if (Array.isArray(map.names))
        flat.names.push(...map.names.map((name: unknown) => String(name)));
    const lines = flat.decodedMappings;
    if (!lines || typeof map.mappings !== "string") return;
    decodeMappings(map.mappings).forEach((segments, index) => {
        const line = start.line + index;
        if (line > stop.line) return;
        for (let row = lines.length; row <= line; row += 1) lines.push([]);
        for (const [
            column,
            source,
            sourceLine,
            sourceColumn,
            name,
        ] of segments) {
            const shifted = column! + (index === 0 ? start.column : 0);
            if (line === stop.line && shifted >= stop.column) return;
            lines[line]!.push(
                source === undefined
                    ? [shifted]
                    : name === undefined
                      ? [
                            shifted,
                            sourcesOffset + source,
                            sourceLine!,
                            sourceColumn!,
                        ]
                      : [
                            shifted,
                            sourcesOffset + source,
                            sourceLine!,
                            sourceColumn!,
                            namesOffset + name,
                        ],
            );
        }
    });
}

function preparedMap(
    raw: string,
    mapUrl: string,
    scriptUrl: string,
): PreparedMap | undefined {
    const map = JSON.parse(raw) as RawMap;
    const flat: Flat = {
        sources: [],
        sourcesContent: [],
        names: [],
        decodedMappings: map.sections ? [] : undefined,
    };
    flatten(
        map,
        mapUrl,
        flat,
        { line: 0, column: 0 },
        {
            line: Infinity,
            column: Infinity,
        },
    );
    const mapped = flat.decodedMappings
        ? flat.decodedMappings.some((segments) => segments.length)
        : typeof map.mappings === "string" && /[^;,]/.test(map.mappings);
    if (!mapped) return undefined;
    const named = new Set<string>();
    flat.sources = flat.sources.map((name, index) => {
        const file = authoredFile(name);
        if (!file) return name;
        const key = reportKey(file);
        if (named.has(key))
            throw new Error(`Map ${mapUrl} of ${scriptUrl} names ${key} twice`);
        named.add(key);
        const text = flat.sourcesContent[index];
        if (typeof text !== "string")
            throw new Error(
                `Map ${mapUrl} of ${scriptUrl} has no text for ${key}`,
            );
        if (text.replace(/^\uFEFF/, "") !== authoredText(key))
            throw new Error(
                `Coverage text for ${key} in ${scriptUrl} is not the authored file`,
            );
        flat.sourcesContent[index] = `${text}\n${authoredEnd}`;
        return pathToFileURL(file).href;
    });
    if (!named.size) return undefined;
    return map.sections
        ? { version: 3, sections: map.sections, ...flat }
        : {
              version: 3,
              sources: flat.sources,
              sourcesContent: flat.sourcesContent,
              names: flat.names,
              mappings: map.mappings as string,
          };
}

function inlineMap(url: string): string {
    const comma = url.indexOf(",");
    const data = url.slice(comma + 1);
    return url.slice(0, comma).endsWith(";base64")
        ? Buffer.from(data, "base64").toString("utf8")
        : decodeURIComponent(data);
}

// The map the script names is attached here, so monocart's own map loading and its map cache are never used.
export function preparedEntry<T extends Script>(
    entry: T,
    load: MapLoader,
): (T & { sourceMap: PreparedMap }) | undefined {
    const reference = [...(entry.source ?? "").matchAll(directive)].at(-1)?.[1];
    if (!reference) return undefined;
    const inline = reference.startsWith("data:");
    const mapUrl = inline ? entry.url : new URL(reference, entry.url).href;
    const sourceMap = preparedMap(
        inline ? inlineMap(reference) : load(mapUrl),
        mapUrl,
        entry.url,
    );
    return sourceMap && { ...entry, sourceMap };
}

export function fileMap(mapUrl: string): string {
    if (!mapUrl.startsWith("file:"))
        throw new Error(`Map ${mapUrl} is not a file`);
    return readFileSync(fileURLToPath(mapUrl), "utf8");
}

function builtFile(url: string): string {
    const parsed = new URL(url);
    if (
        parsed.origin !== coverageOrigin ||
        !parsed.pathname.startsWith("/_next/static/")
    )
        throw new Error(`${url} is not served from this run's build`);
    return path.join(
        distDir(),
        "static",
        ...decodeURIComponent(
            parsed.pathname.slice("/_next/static/".length),
        ).split("/"),
    );
}

function browserMap(mapUrl: string): string {
    return readFileSync(builtFile(mapUrl), "utf8");
}

export async function addBrowserCoverage(entries: Script[]): Promise<void> {
    const prepared = entries
        .filter((entry) => browserEntry(entry.url))
        .flatMap((entry) => {
            if (entry.source !== readFileSync(builtFile(entry.url), "utf8"))
                throw new Error(
                    `Browser script ${entry.url} is not this run's build output`,
                );
            const script = preparedEntry(entry, browserMap);
            return script ? [script] : [];
        });
    if (prepared.length)
        await new CoverageReport(browserCoverage()).add(prepared);
}

export function authoredSources(): string[] {
    return readdirSync("src", { recursive: true })
        .map((file) => `web/src/${String(file).replaceAll("\\", "/")}`)
        .filter(authoredSource)
        .sort();
}

export function baselineEntries() {
    return authoredSources().flatMap((key) => {
        const file = path.resolve("src", key.slice(8));
        const url = pathToFileURL(file).href;
        const output = ts.transpileModule(readFileSync(file, "utf8"), {
            fileName: file,
            compilerOptions: {
                target: ts.ScriptTarget.ESNext,
                module: ts.ModuleKind.ESNext,
                jsx: ts.JsxEmit.ReactJSX,
                sourceMap: true,
                inlineSources: true,
            },
        });
        const script = preparedEntry(
            { url, source: output.outputText, empty: true },
            () => output.sourceMapText!,
        );
        return script ? [script] : [];
    });
}
