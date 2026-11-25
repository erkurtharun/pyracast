import Papa from "papaparse";
import type core from "../../messages/en/core.json";
import { loadCatalogue } from "@/i18n/catalogue";
import type { english } from "@/i18n/en";
import { isLanguage, languages } from "@/i18n/language";
import { DisplayError } from "@/lib/api/errors";
import type { components } from "./schema";
export const QUALITIES = ["valid", "outage", "curtailed", "invalid"] as const;
export type Quality = (typeof QUALITIES)[number];
export function qualityOf(value: string | null | undefined): Quality {
    if (value === null || value === undefined) return "valid";
    const selected = QUALITIES.find((quality) => quality === value);
    if (selected === undefined)
        throw new Error("Unrecognized production quality.");
    return selected;
}

type Interval = components["schemas"]["ProductionIntervalOutput"];
export type ProductionSource = "settlement" | "provisional" | "manual";

export function productionSource(
    row: Pick<Interval, "observation" | "effective">,
): ProductionSource {
    if (row.observation != null || row.effective?.aggregated === true)
        return "manual";
    if (row.effective?.source_kind === "settlement_injection")
        return "settlement";
    if (row.effective?.source_kind === "realtime_generation")
        return "provisional";
    return "manual";
}

type Observation = {
    starts_at: string;
    ends_at: string;
    energy: number;
    quality: Quality;
    source: string;
    quality_label: boolean;
    source_label: ProductionSource | null;
};
type Unit = "MWh" | "kWh";
export type ProductionFile = { unit: Unit; observations: Observation[] };

type Role = "start" | "end" | "energy" | "status" | "source" | "timezone";
type Column = { role: Role; unit?: Unit };
const REQUIRED: readonly Role[] = [
    "start",
    "end",
    "energy",
    "status",
    "source",
];
const SOURCES: readonly ProductionSource[] = [
    "settlement",
    "provisional",
    "manual",
];

type Status = { quality: Quality | "mixed"; label: boolean };

export type ImportWords = {
    columns: ReadonlyMap<string, Column>;
    statuses: ReadonlyMap<string, Status>;
    sources: ReadonlyMap<string, ProductionSource>;
};

export function importWords(
    catalogues: readonly (typeof english)[],
): ImportWords {
    const columns = new Map<string, Column>([
        ["starts_at", { role: "start" }],
        ["ends_at", { role: "end" }],
        ["energy_mwh", { role: "energy", unit: "MWh" }],
        ["energy_kwh", { role: "energy", unit: "kWh" }],
        ["quality", { role: "status" }],
        ["source", { role: "source" }],
        ["timezone", { role: "timezone" }],
    ]);
    const statuses = new Map<string, Status>(
        [...QUALITIES, "mixed" as const].map((quality) => [
            quality,
            { quality, label: false },
        ]),
    );
    const sources = new Map<string, ProductionSource>();
    for (const { CsvFile, Enums } of catalogues) {
        const named = CsvFile.columns;
        columns.set(named.start, { role: "start" });
        columns.set(named.end, { role: "end" });
        columns.set(named.actual_mwh, { role: "energy", unit: "MWh" });
        columns.set(named.actual_kwh, { role: "energy", unit: "kWh" });
        columns.set(named.status, { role: "status" });
        columns.set(named.source, { role: "source" });
        for (const quality of QUALITIES)
            statuses.set(Enums.ObservationQuality[quality], {
                quality,
                label: true,
            });
        statuses.set(CsvFile.words.mixed, { quality: "mixed", label: true });
        for (const source of SOURCES)
            sources.set(CsvFile.words[source], source);
    }
    return { columns, statuses, sources };
}

const EXAMPLE = ["start", "end", "actual_mwh", "status", "source"] as const;

export function importExample(file: (key: string) => string, normal: string) {
    return [
        EXAMPLE.map((column) => file(`columns.${column}`)).join(","),
        `2026-01-01T09:00:00+03:00,2026-01-01T10:00:00+03:00,0.25,${normal},${file("words.manual")}`,
    ];
}

function fileColumns(fields: readonly string[], words: ImportWords) {
    const indexes = new Map<Role, number>();
    let unit: Unit | undefined;
    for (const [index, field] of fields.entries()) {
        const column = words.columns.get(field);
        if (!column || indexes.has(column.role)) return null;
        indexes.set(column.role, index);
        unit ??= column.unit;
    }
    const index = (role: Role) => indexes.get(role) ?? -1;
    const name = (role: Role) => fields[index(role)] ?? "";
    return unit && REQUIRED.every((role) => indexes.has(role))
        ? { unit, index, name }
        : null;
}

export type CsvTexts = (
    key: keyof typeof core.ProductionCsv,
    values?: Record<string, string | number>,
) => string;

type FileRecord = { line: number; cells: string[] };
type FileRecords = { fields: string[]; rows: FileRecord[]; readable: boolean };

function fileRecords(text: string, t: CsvTexts): FileRecords {
    if (new TextEncoder().encode(text).length > 2 * 1024 * 1024)
        throw new DisplayError(t("size"));
    const records: FileRecord[] = [];
    let readable = true;
    let line = 1;
    let start = 0;
    Papa.parse<string[]>(text, {
        delimiter: ",",
        dynamicTyping: false,
        transform: (value) => value.trim(),
        step: ({ data, errors, meta }) => {
            readable &&= errors.length === 0;
            if (data.some(Boolean)) records.push({ line, cells: data });
            line +=
                text.slice(start, meta.cursor).match(/\r\n|\r|\n/g)?.length ??
                0;
            start = meta.cursor;
        },
    });
    const [header, ...rows] = records;
    return { fields: header?.cells ?? [], rows, readable };
}

export function parseProduction(
    text: string,
    t: CsvTexts,
    words: ImportWords,
): ProductionFile {
    return productionFile(fileRecords(text, t), t, words);
}

export async function readProduction(
    text: string,
    t: CsvTexts,
    locale: string,
): Promise<ProductionFile> {
    const records = fileRecords(text, t);
    const language = isLanguage(locale) ? locale : "en";
    const active = await loadCatalogue(language);
    let words = importWords([active]);
    if (!fileColumns(records.fields, words)) {
        const others = await Promise.all(
            languages
                .filter((other) => other !== language)
                .map((other) => loadCatalogue(other)),
        ).catch(() => {
            throw new DisplayError(t("names"));
        });
        words = importWords([active, ...others]);
    }
    return productionFile(records, t, words);
}

function productionFile(
    { fields, rows: records, readable }: FileRecords,
    t: CsvTexts,
    words: ImportWords,
): ProductionFile {
    const columns = fileColumns(fields, words);
    if (!columns) throw new DisplayError(t("header"));
    const { index, name } = columns;
    if (
        !readable ||
        records.some(({ cells }) => cells.length !== fields.length)
    )
        throw new DisplayError(t("parse"));
    if (records.length < 1 || records.length > 2000)
        throw new DisplayError(t("count"));
    const rows = records.map(({ line: row, cells }) => {
        const cell = (role: Role) => cells[index(role)] ?? "";
        const starts_at = cell("start"),
            ends_at = cell("end"),
            rawEnergy = cell("energy"),
            source = cell("source");
        const start = Date.parse(starts_at),
            end = Date.parse(ends_at),
            energy = Number(rawEnergy);
        const aware = /(?:Z|[+-]\d{2}:\d{2})$/;
        if (
            !aware.test(starts_at) ||
            !aware.test(ends_at) ||
            !Number.isFinite(start) ||
            !Number.isFinite(end) ||
            ![1800000, 3600000].includes(end - start)
        )
            throw new DisplayError(
                t("interval", { row, start: name("start"), end: name("end") }),
            );
        if (end > Date.now()) throw new DisplayError(t("future", { row }));
        if (!rawEnergy || !Number.isFinite(energy) || energy < 0)
            throw new DisplayError(
                t("energy", { row, column: name("energy") }),
            );
        const status = words.statuses.get(cell("status"));
        if (status?.quality === "mixed")
            throw new DisplayError(t("mixed", { row }));
        if (!status || !source || source.length > 200)
            throw new DisplayError(
                t("quality", {
                    row,
                    status: name("status"),
                    source: name("source"),
                }),
            );
        return {
            row,
            observation: {
                starts_at,
                ends_at,
                energy,
                quality: status.quality,
                source,
                quality_label: status.label,
                source_label: words.sources.get(source) ?? null,
            },
        };
    });
    const sorted = [...rows].sort(
        (a, b) =>
            Date.parse(a.observation.starts_at) -
            Date.parse(b.observation.starts_at),
    );
    for (const [index, current] of sorted.entries()) {
        const previous = sorted[index - 1];
        if (
            previous &&
            Date.parse(current.observation.starts_at) <
                Date.parse(previous.observation.ends_at)
        )
            throw new DisplayError(
                t("overlap", {
                    first: Math.min(previous.row, current.row),
                    second: Math.max(previous.row, current.row),
                }),
            );
    }
    return {
        unit: columns.unit,
        observations: rows.map(({ observation }) => observation),
    };
}
