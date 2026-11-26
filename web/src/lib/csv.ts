import { fileNumber } from "./format/number";
import type { Vocabulary } from "./vocabulary";

export type CsvValue = string | number | null;

export type CsvColumn =
    | "plant"
    | "data"
    | "start"
    | "end"
    | "forecast_mwh"
    | "energy_mwh"
    | "power_mw"
    | "irradiance"
    | "without_outage_mwh"
    | "with_outage_mwh"
    | "power_without_outage_mw"
    | "power_with_outage_mw"
    | "actual_mwh"
    | "status"
    | "source"
    | "type"
    | "title"
    | "panels_in_service_mw"
    | "inverter_in_service_mw"
    | "export_in_service_mw"
    | "notes"
    | "latitude"
    | "longitude"
    | "time_zone"
    | "panel_capacity_mwp"
    | "inverter_capacity_mw"
    | "export_limit_mw"
    | "meter_point"
    | "mounting"
    | "tilt"
    | "azimuth"
    | "comparison"
    | "date"
    | "reason"
    | "expected_mwh"
    | "forecast_difference_mwh"
    | "expected_difference_mwh"
    | "realtime"
    | "number"
    | "period_start"
    | "period_end"
    | "in_use"
    | "preliminary"
    | "factor"
    | "calibration_days"
    | "test_days"
    | "error_without_mwh"
    | "error_with_mwh";

export type CsvWord =
    | "yes"
    | "no"
    | "compared"
    | "not_compared"
    | "mixed"
    | "settlement"
    | "provisional"
    | "manual"
    | "net_export"
    | "ac_generation"
    | "fixed"
    | "tracking"
    | "unknown";

type Group = keyof Vocabulary;
type Label = { [G in Group]: { group: G; value: Vocabulary[G] } }[Group];

export type CsvText =
    | { kind: "word"; word: CsvWord }
    | ({ kind: "label" } & Label)
    | { kind: "reason"; reason: string | null };

export type CsvCell = CsvValue | boolean | CsvText;

export type CsvExport = {
    filename: string;
    header: readonly CsvColumn[];
    rows: readonly (readonly CsvCell[])[];
};

export type CsvTexts = {
    column: (column: CsvColumn) => string;
    text: (text: CsvText) => string;
};

export function readable(file: CsvExport, texts: CsvTexts) {
    const value = (cell: CsvCell): CsvValue =>
        typeof cell === "boolean"
            ? texts.text({ kind: "word", word: cell ? "yes" : "no" })
            : cell !== null && typeof cell === "object"
              ? texts.text(cell)
              : cell;
    return {
        header: file.header.map(texts.column),
        rows: file.rows.map((row) => row.map(value)),
    };
}

const INVISIBLE = /^[\p{White_Space}\p{Cc}\p{Cf}]$/u;
const FORMULA_START = "=+-@";
const PLAIN_NUMBER = /^[+-]?\d+(?:\.\d+)?$/;

function startsFormula(value: string): boolean {
    for (const character of value) {
        if (!INVISIBLE.test(character))
            return FORMULA_START.includes(character);
    }
    return false;
}

// Looks past apostrophes already in front, so unguarded() removes only the one added.
function guarded(value: string): boolean {
    const text = value.replace(/^'+/, "");
    return startsFormula(text) && !PLAIN_NUMBER.test(text);
}

export function unguarded(value: string): string {
    return value.startsWith("'") && guarded(value) ? value.slice(1) : value;
}

function cell(value: CsvValue): string {
    if (value === null) return "";
    if (typeof value === "number") return fileNumber(value);
    // Quoting alone does not stop a spreadsheet from evaluating a leading formula character.
    const safe = guarded(value) ? `'${value}` : value;
    return `"${safe.replaceAll('"', '""')}"`;
}

export function csvDocument(
    header: readonly string[],
    rows: readonly (readonly CsvValue[])[],
): string {
    const lines = [header, ...rows].map((row) => row.map(cell).join(","));
    return `\uFEFF${lines.join("\r\n")}\r\n`;
}

const NAME_EDGE = /^[\s.-]$/u;

function trimNameEdges(part: string): string {
    let start = 0;
    let end = part.length;
    while (start < end && NAME_EDGE.test(part.charAt(start))) start += 1;
    while (end > start && NAME_EDGE.test(part.charAt(end - 1))) end -= 1;
    return part.slice(start, end);
}

const NAME_LIMIT = 150;

// Windows refuses these characters and trailing dots or spaces in file names; plant names keep their letters.
export function csvFilename(...parts: string[]): string {
    const safe = parts
        .map((part) => trimNameEdges(part.replace(/[<>:"/\\|?*\p{Cc}]/gu, "-")))
        .filter(Boolean);
    return `${safe.join("-").slice(0, NAME_LIMIT)}.csv`;
}

function asciiSlug(name: string): string {
    return name
        .toLocaleLowerCase("tr")
        .replaceAll("ı", "i")
        .normalize("NFKD")
        .replace(/\p{M}/gu, "")
        .replace(/[^a-z0-9]+/g, "-")
        .replace(/^-|-$/g, "");
}

// `pyracast-{plant}-{dates}-{kind}.csv` in ASCII; only the plant slug gives way to the name limit.
export function plantFilename(
    plant: string,
    kind: string,
    ...dates: string[]
): string {
    const tail = [...dates, kind].filter(Boolean).join("-");
    const room = NAME_LIMIT - `pyracast--${tail}`.length;
    const slug = asciiSlug(plant).slice(0, Math.max(room, 0)).replace(/-$/, "");
    const name = slug || "plant";
    if (name.length > room)
        throw new Error("The file kind and dates leave no room for the plant.");
    return csvFilename("pyracast", name, tail);
}
