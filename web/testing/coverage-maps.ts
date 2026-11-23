import { readdirSync, readFileSync } from "node:fs";
import path from "node:path";
import { pathToFileURL } from "node:url";
import { CoverageReport } from "monocart-coverage-reports";
import ts from "typescript";
import { byCodeUnit, lineCount } from "./coverage-combine";
import {
    authoredFile,
    authoredSource,
    authoredText,
    browserCoverage,
    browserEntry,
    buildRoot,
    builtFile,
    builtUrl,
    distDir,
    reportKey,
    type BuildPart,
} from "./coverage-options";

type Segment = number[];
type FlatMap = {
    sources: string[];
    sourcesContent: (string | null)[];
    names: string[];
    sourceRoot?: string;
    lines: Segment[][];
};
type Flattened = Omit<FlatMap, "sourceRoot">;
type PreparedMap = {
    version: 3;
    sources: string[];
    sourcesContent: (string | null)[];
    names: string[];
    mappings: string;
};
type Script = { url: string; source?: string };
type MapLoader = (mapUrl: string) => string;
type Preparation<T> = {
    entry: T & { sourceMap: PreparedMap };
    authored: string[];
};

export const authoredEnd = "// end of the authored text";

export function convertedText(text: string): string {
    return `${text}\n${authoredEnd}`;
}

// Both comment forms of the source map specification; `@` is their deprecated variant.
const directive =
    /\/(?:\/[#@][ \t]+sourceMappingURL=([^\s'"`]+)|\*[#@][ \t]+sourceMappingURL=([^\s'"`*]+)[ \t]*\*\/)[ \t]*$/gm;
const digits =
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
const values = new Map([...digits].map((digit, value) => [digit, value]));

function decodeLine(line: string, state: number[]): Segment[] {
    state[0] = 0;
    return line
        .split(",")
        .filter(Boolean)
        .map((segment) => {
            const deltas: number[] = [];
            let value = 0;
            let shift = 0;
            for (const digit of segment) {
                const bits = values.get(digit);
                if (bits === undefined)
                    throw new Error(`invalid mapping segment ${segment}`);
                value += (bits & 31) * 2 ** shift;
                if (bits & 32) {
                    shift += 5;
                    continue;
                }
                deltas.push(value % 2 ? -(value - 1) / 2 : value / 2);
                value = 0;
                shift = 0;
            }
            if (shift || ![1, 4, 5].includes(deltas.length))
                throw new Error(`invalid mapping segment ${segment}`);
            return deltas.map((delta, index) => (state[index]! += delta));
        })
        .sort((a, b) => a[0]! - b[0]!);
}

function encodeValue(value: number): string {
    let rest = value < 0 ? -value * 2 + 1 : value * 2;
    let encoded = "";
    do {
        const digit = rest % 32;
        rest = Math.floor(rest / 32);
        encoded += digits[rest ? digit + 32 : digit];
    } while (rest);
    return encoded;
}

function encodeMappings(lines: Segment[][]): string {
    const state = [0, 0, 0, 0, 0];
    return lines
        .map((segments) => {
            state[0] = 0;
            return segments
                .map((segment) =>
                    segment
                        .map((value, index) => {
                            const delta = value - state[index]!;
                            state[index] = value;
                            return encodeValue(delta);
                        })
                        .join(""),
                )
                .join(",");
        })
        .join(";");
}

const isObject = (value: unknown): value is Record<string, unknown> =>
    typeof value === "object" && value !== null && !Array.isArray(value);
const strings = (value: unknown): value is string[] =>
    Array.isArray(value) && value.every((item) => typeof item === "string");

function validFlatMap(value: unknown): FlatMap {
    if (!isObject(value)) throw new Error("a map is not an object");
    if ("sections" in value) throw new Error("an index map is nested");
    const { version, sources, sourcesContent, names, mappings, sourceRoot } =
        value;
    if (version !== 3) throw new Error("a map is not of version 3");
    if (!strings(sources)) throw new Error("map sources are not strings");
    if (typeof mappings !== "string") throw new Error("a map has no mappings");
    if (
        sourcesContent !== undefined &&
        !(
            Array.isArray(sourcesContent) &&
            sourcesContent.every(
                (text) => text === null || typeof text === "string",
            )
        )
    )
        throw new Error("map sourcesContent is not a list of texts");
    if (names !== undefined && !strings(names))
        throw new Error("map names are not strings");
    if (sourceRoot !== undefined && typeof sourceRoot !== "string")
        throw new Error("map sourceRoot is not a string");
    const state = [0, 0, 0, 0, 0];
    const lines = mappings.split(";").map((line) => decodeLine(line, state));
    for (const segment of lines.flat()) {
        const [column, source, line, sourceColumn, name] = segment;
        if (
            column! < 0 ||
            (segment.length > 1 &&
                (source! < 0 ||
                    source! >= sources.length ||
                    line! < 0 ||
                    sourceColumn! < 0)) ||
            (segment.length === 5 &&
                (name! < 0 || name! >= (names ?? []).length))
        )
            throw new Error(
                `a mapping segment is out of range: ${segment.join(",")}`,
            );
    }
    return {
        sources,
        sourcesContent: sources.map(
            (_, index) =>
                (sourcesContent as (string | null)[] | undefined)?.[index] ??
                null,
        ),
        names: names ?? [],
        sourceRoot,
        lines,
    };
}

function sectionsOf(value: unknown): { line: number; map: FlatMap }[] {
    if (!isObject(value) || !("sections" in value))
        return [{ line: 0, map: validFlatMap(value) }];
    if (value.version !== 3)
        throw new Error("an index map is not of version 3");
    if (!Array.isArray(value.sections))
        throw new Error("index map sections are not a list");
    return value.sections.map((section: unknown, index, all) => {
        if (!isObject(section) || !isObject(section.offset))
            throw new Error(`index map section ${index} has no offset`);
        const { line, column } = section.offset;
        if (!Number.isInteger(line) || (line as number) < 0 || column !== 0)
            throw new Error(
                `index map section ${index} is not at the start of a line`,
            );
        const previous = index
            ? (all[index - 1] as { offset: { line: number } }).offset.line
            : -1;
        if ((line as number) <= previous)
            throw new Error(`index map section ${index} is out of order`);
        return { line: line as number, map: validFlatMap(section.map) };
    });
}

function resolvedNames(map: FlatMap, mapUrl: string): string[] {
    const root = map.sourceRoot
        ? new URL(map.sourceRoot.replace(/\/?$/, "/"), mapUrl).href
        : mapUrl;
    return map.sources.map((name) =>
        /^[a-z][a-z\d+.-]*:/i.test(name) ? name : new URL(name, root).href,
    );
}

function flattened(value: unknown, mapUrl: string): Flattened {
    const flat: Flattened = {
        sources: [],
        sourcesContent: [],
        names: [],
        lines: [],
    };
    const sections = sectionsOf(value);
    sections.forEach(({ line, map }, index) => {
        const stop = sections[index + 1]?.line ?? Infinity;
        const sourcesOffset = flat.sources.length;
        const namesOffset = flat.names.length;
        flat.sources.push(...resolvedNames(map, mapUrl));
        flat.sourcesContent.push(...map.sourcesContent);
        flat.names.push(...map.names);
        map.lines.forEach((segments, row) => {
            const target = line + row;
            if (target >= stop) return;
            while (flat.lines.length <= target) flat.lines.push([]);
            flat.lines[target] = segments.map((segment) =>
                segment.map((value, field) =>
                    field === 1
                        ? value + sourcesOffset
                        : field === 4
                          ? value + namesOffset
                          : value,
                ),
            );
        });
    });
    return flat;
}

function preparedMap(
    raw: string,
    mapUrl: string,
): { sourceMap: PreparedMap; authored: string[] } | undefined {
    const flat = flattened(JSON.parse(raw) as unknown, mapUrl);
    const keys: (string | undefined)[] = [];
    const lineLimits: number[] = [];
    flat.sources = flat.sources.map((name, index) => {
        const file = authoredFile(name);
        if (!file) return name;
        const key = reportKey(file);
        if (keys.includes(key)) throw new Error(`it names ${key} twice`);
        const text = flat.sourcesContent[index];
        if (typeof text !== "string")
            throw new Error(`it has no text for ${key}`);
        if (text.replace(/^\u{FEFF}/u, "") !== authoredText(key))
            throw new Error(
                `Coverage text for ${key} is not the authored file`,
            );
        const converted = convertedText(text);
        flat.sourcesContent[index] = converted;
        keys[index] = key;
        lineLimits[index] = lineCount(converted) - 1;
        return pathToFileURL(file).href;
    });
    const mapped = new Set<string>();
    for (const [, source, line] of flat.lines.flat()) {
        const key = source === undefined ? undefined : keys[source];
        if (!key) continue;
        if (line! >= lineLimits[source!]!)
            throw new Error(
                `it maps ${key} to line ${line! + 1}, which its text does not have`,
            );
        mapped.add(key);
    }
    if (!mapped.size) return undefined;
    return {
        sourceMap: {
            version: 3,
            sources: flat.sources,
            sourcesContent: flat.sourcesContent,
            names: flat.names,
            mappings: encodeMappings(flat.lines),
        },
        authored: [...mapped],
    };
}

function inlineMap(url: string): string {
    const comma = url.indexOf(",");
    const data = url.slice(comma + 1);
    return url.slice(0, comma).endsWith(";base64")
        ? Buffer.from(data, "base64").toString("utf8")
        : decodeURIComponent(data);
}

function preparation<T extends Script>(
    entry: T,
    load: MapLoader,
): Preparation<T> | undefined {
    const reference = [...(entry.source ?? "").matchAll(directive)].at(-1);
    const target = reference?.[1] ?? reference?.[2];
    if (!target) return undefined;
    const inline = target.startsWith("data:");
    let mapUrl = entry.url;
    try {
        if (!inline) mapUrl = new URL(target, entry.url).href;
        const prepared = preparedMap(
            inline ? inlineMap(target) : load(mapUrl),
            mapUrl,
        );
        return (
            prepared && {
                entry: { ...entry, sourceMap: prepared.sourceMap },
                authored: prepared.authored,
            }
        );
    } catch (error) {
        throw new Error(
            `Coverage map ${mapUrl} of ${entry.url}: ${(error as Error).message}`,
            { cause: error },
        );
    }
}

export function preparedEntry<T extends Script>(
    entry: T,
    load: MapLoader,
): (T & { sourceMap: PreparedMap }) | undefined {
    return preparation(entry, load)?.entry;
}

const browserMap: MapLoader = (mapUrl) =>
    readFileSync(builtFile(mapUrl, "static"), "utf8");

export const serverMap: MapLoader = (mapUrl) =>
    readFileSync(builtFile(mapUrl, "server"), "utf8");

const observed = new Map<
    string,
    { source: string; sourceMap: PreparedMap | undefined }
>();

export async function addBrowserCoverage<T extends Script>(
    entries: T[],
): Promise<void> {
    const prepared = entries
        .filter((entry) => browserEntry(entry.url))
        .flatMap((entry) => {
            const key = `${distDir()}\n${entry.url}`;
            let known = observed.get(key);
            if (!known || known.source !== entry.source) {
                const built = readFileSync(
                    builtFile(entry.url, "static"),
                    "utf8",
                );
                if (entry.source !== built)
                    throw new Error(
                        `Browser script ${entry.url} is not this run's build output`,
                    );
                known = {
                    source: built,
                    sourceMap: preparedEntry(entry, browserMap)?.sourceMap,
                };
                observed.set(key, known);
            }
            return known.sourceMap
                ? [{ ...entry, sourceMap: known.sourceMap }]
                : [];
        });
    if (prepared.length)
        await new CoverageReport(browserCoverage()).add(prepared);
}

export function authoredSources(): string[] {
    return readdirSync("src", { recursive: true })
        .map((file) => `web/src/${String(file).replaceAll("\\", "/")}`)
        .filter(authoredSource)
        .sort(byCodeUnit);
}

function builtScripts(part: BuildPart): Preparation<Script>[] {
    const root = buildRoot(part);
    const load = part === "static" ? browserMap : serverMap;
    return readdirSync(root, { recursive: true, withFileTypes: true })
        .filter(
            (item) =>
                (item.isFile() || item.isSymbolicLink()) &&
                item.name.endsWith(".js"),
        )
        .map((item) =>
            builtUrl(
                part,
                path.relative(root, path.join(item.parentPath, item.name)),
            ),
        )
        .sort(byCodeUnit)
        .flatMap((url) => {
            const source = readFileSync(builtFile(url, part), "utf8");
            const prepared = preparation({ url, source, empty: true }, load);
            return prepared ? [prepared] : [];
        });
}

function transpiled(key: string) {
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
    return preparedEntry(
        { url, source: output.outputText, empty: true },
        () => output.sourceMapText!,
    );
}

export function baselineEntries(sources: string[]) {
    const built = [...builtScripts("static"), ...builtScripts("server")];
    const named = new Set(built.flatMap(({ authored }) => authored));
    return [
        ...built.map(({ entry }) => entry),
        ...sources
            .filter((key) => !named.has(key))
            .flatMap((key) => transpiled(key) ?? []),
    ];
}
