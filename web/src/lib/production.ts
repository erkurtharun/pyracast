import Papa from "papaparse";
import type core from "../../messages/en/core.json";
import { loadCatalogue } from "@/i18n/catalogue";
import type { english } from "@/i18n/en";
import { languages } from "@/i18n/language";
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

export type ImportWords = {
    columns: ReadonlyMap<string, Column>;
    statuses: ReadonlyMap<string, Quality | "mixed">;
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
    const statuses = new Map<string, Quality | "mixed">([
        ...QUALITIES.map((quality) => [quality, quality] as const),
        ["mixed", "mixed"],
    ]);
    for (const { CsvFile, Enums } of catalogues) {
        const named = CsvFile.columns;
        columns.set(named.start, { role: "start" });
        columns.set(named.end, { role: "end" });
        columns.set(named.actual_mwh, { role: "energy", unit: "MWh" });
        columns.set(named.actual_kwh, { role: "energy", unit: "kWh" });
        columns.set(named.status, { role: "status" });
        columns.set(named.source, { role: "source" });
        for (const quality of QUALITIES)
            statuses.set(Enums.ObservationQuality[quality], quality);
        statuses.set(CsvFile.words.mixed, "mixed");
    }
    return { columns, statuses };
}

export async function productionWords(): Promise<ImportWords> {
    return importWords(
        await Promise.all(languages.map((language) => loadCatalogue(language))),
    );
}

const EXAMPLE = ["start", "end", "actual_mwh", "status", "source"] as const;

export function importExample(file: (key: string) => string, normal: string) {
    return [
        EXAMPLE.map((column) => file(`columns.${column}`)).join(","),
        `2026-01-01T09:00:00+03:00,2026-01-01T10:00:00+03:00,0.25,${normal},${file("words.manual")}`,
    ];
}

function fileColumns(fields: readonly string[], words: ImportWords) {
    const names = new Map<Role, string>();
    let unit: Unit | undefined;
    for (const field of fields) {
        const column = words.columns.get(field);
        if (!column || names.has(column.role)) return null;
        names.set(column.role, field);
        unit ??= column.unit;
    }
    const name = (role: Role) => names.get(role) ?? "";
    return unit && REQUIRED.every((role) => names.has(role))
        ? { unit, name }
        : null;
}

export type CsvTexts = (
    key: keyof typeof core.ProductionCsv,
    values?: Record<string, string | number>,
) => string;

export function parseProduction(
    text: string,
    t: CsvTexts,
    words: ImportWords,
): ProductionFile {
    if (new TextEncoder().encode(text).length > 2 * 1024 * 1024)
        throw new DisplayError(t("size"));
    const parsed = Papa.parse<Record<string, string>>(text, {
        header: true,
        delimiter: ",",
        dynamicTyping: false,
        skipEmptyLines: "greedy",
        transformHeader: (value) => value.trim(),
        transform: (value) => value.trim(),
    });
    const columns = fileColumns(parsed.meta.fields ?? [], words);
    if (!columns) throw new DisplayError(t("header"));
    const { name } = columns;
    const [error] = parsed.errors;
    if (error) throw new DisplayError(t("parse", { reason: error.message }));
    if (parsed.data.length < 1 || parsed.data.length > 2000)
        throw new DisplayError(t("count"));
    const rows = parsed.data.map((cells, index) => {
        const row = index + 2;
        const cell = (role: Role) => cells[name(role)] ?? "";
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
        if (status === "mixed") throw new DisplayError(t("mixed", { row }));
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
                quality: status,
                source,
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
