export type CsvValue = string | number | boolean | null;

export type CsvExport = {
    filename: string;
    header: readonly string[];
    rows: readonly (readonly CsvValue[])[];
};

const FORMULA = /^[\p{White_Space}\p{Cc}\p{Cf}]*[=+\-@]/u;
const PLAIN_NUMBER = /^[+-]?\d+(?:\.\d+)?$/;

function cell(value: CsvValue): string {
    if (value === null) return "";
    if (typeof value === "number")
        return Number.isFinite(value) ? String(value) : "";
    if (typeof value === "boolean") return String(value);
    // Quoting alone does not stop a spreadsheet from evaluating a leading formula character.
    const safe =
        FORMULA.test(value) && !PLAIN_NUMBER.test(value) ? `'${value}` : value;
    return `"${safe.replaceAll('"', '""')}"`;
}

export function csvDocument(
    header: readonly string[],
    rows: readonly (readonly CsvValue[])[],
): string {
    const lines = [header, ...rows].map((row) => row.map(cell).join(","));
    return `\uFEFF${lines.join("\r\n")}\r\n`;
}

// Windows refuses these characters and trailing dots or spaces in file names; plant names keep their letters.
export function csvFilename(...parts: string[]): string {
    const safe = parts
        .map((part) =>
            part
                .replace(/[<>:"/\\|?*\p{Cc}]/gu, "-")
                .replace(/^[\s.-]+|[\s.-]+$/gu, ""),
        )
        .filter(Boolean);
    return `${safe.join("-").slice(0, 150)}.csv`;
}
