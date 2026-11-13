type CsvRecord = Record<string, unknown>;

function flatten(record: CsvRecord, prefix = ""): CsvRecord {
    return Object.fromEntries(
        Object.entries(record).flatMap(([key, value]) => {
            if (value === undefined) return [];
            const name = prefix ? `${prefix}.${key}` : key;
            return value !== null &&
                typeof value === "object" &&
                !Array.isArray(value)
                ? Object.entries(flatten(value as CsvRecord, name))
                : [[name, value]];
        }),
    );
}

function cell(value: unknown): string {
    if (value === null || value === undefined) return "";
    if (typeof value === "number" || typeof value === "boolean")
        return String(value);
    const text = typeof value === "string" ? value : JSON.stringify(value);
    // Quoting alone does not prevent spreadsheet formula execution.
    const safe = /^[\p{White_Space}\p{Cc}]*[=+\-@]/u.test(text)
        ? `'${text}`
        : text;
    return `"${safe.replaceAll('"', '""')}"`;
}

export function csvDocument(
    records: CsvRecord[],
    metadata: CsvRecord = {},
): string {
    const rows = (records.length ? records : [{}]).map((row) =>
        flatten({ ...metadata, ...row }),
    );
    const headers = [...new Set(rows.flatMap((row) => Object.keys(row)))];
    return `\uFEFF${[headers.map(cell).join(","), ...rows.map((row) => headers.map((key) => cell(row[key])).join(","))].join("\r\n")}\r\n`;
}
