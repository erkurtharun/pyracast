import Papa from "papaparse";
export type Observation = {
    starts_at: string;
    ends_at: string;
    energy: number;
    quality: string;
    source: string;
};
export function parseProduction(text: string): Observation[] {
    if (new TextEncoder().encode(text).length > 2 * 1024 * 1024)
        throw new Error("Choose a CSV no larger than 2 MiB.");
    const parsed = Papa.parse<Record<string, string>>(text, {
        header: true,
        delimiter: ",",
        dynamicTyping: false,
        skipEmptyLines: "greedy",
        transformHeader: (value) => value.trim(),
        transform: (value) => value.trim(),
    });
    if (
        parsed.meta.fields?.join(",") !==
        "starts_at,ends_at,energy,quality,source"
    )
        throw new Error(
            "Use the CSV header: starts_at,ends_at,energy,quality,source",
        );
    if (parsed.errors.length)
        throw new Error(`CSV could not be parsed: ${parsed.errors[0].message}`);
    if (parsed.data.length < 1 || parsed.data.length > 2000)
        throw new Error("Import between 1 and 2,000 observations per batch.");
    const rows = parsed.data.map((row, index) => {
        const { starts_at, ends_at, energy: rawEnergy, quality, source } = row;
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
            throw new Error(
                `Row ${index + 2}: use timezone-aware ISO timestamps for a 30 or 60 minute interval.`,
            );
        if (end > Date.now())
            throw new Error(
                `Row ${index + 2}: production intervals must be completed.`,
            );
        if (!rawEnergy || !Number.isFinite(energy) || energy < 0)
            throw new Error(
                `Row ${index + 2}: energy must be a non-negative number.`,
            );
        if (
            !["valid", "outage", "curtailed", "invalid"].includes(quality) ||
            !source ||
            source.length > 200
        )
            throw new Error(
                `Row ${index + 2}: include a valid quality and source (up to 200 characters).`,
            );
        return { starts_at, ends_at, energy, quality, source };
    });
    const sorted = [...rows].sort(
        (a, b) => Date.parse(a.starts_at) - Date.parse(b.starts_at),
    );
    if (
        sorted.some(
            (row, index) =>
                index > 0 &&
                Date.parse(row.starts_at) <
                    Date.parse(sorted[index - 1].ends_at),
        )
    )
        throw new Error("The CSV contains overlapping intervals.");
    return rows;
}
