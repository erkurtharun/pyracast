export type CsvValue = string | number | boolean | null;

export type CsvExport = {
    filename: string;
    header: readonly string[];
    rows: readonly (readonly CsvValue[])[];
};

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

function cell(value: CsvValue): string {
    if (value === null) return "";
    if (typeof value === "number")
        return Number.isFinite(value) ? String(value) : "";
    if (typeof value === "boolean") return String(value);
    // Quoting alone does not stop a spreadsheet from evaluating a leading formula character.
    const safe =
        startsFormula(value) && !PLAIN_NUMBER.test(value) ? `'${value}` : value;
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

// Windows refuses these characters and trailing dots or spaces in file names; plant names keep their letters.
export function csvFilename(...parts: string[]): string {
    const safe = parts
        .map((part) => trimNameEdges(part.replace(/[<>:"/\\|?*\p{Cc}]/gu, "-")))
        .filter(Boolean);
    return `${safe.join("-").slice(0, 150)}.csv`;
}
