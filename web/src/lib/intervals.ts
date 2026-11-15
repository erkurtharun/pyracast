export function dataIntervalRange<T>(
    rows: T[],
    hasData: (row: T) => boolean,
): T[] {
    const first = rows.findIndex(hasData);
    if (first === -1) return [];
    const last = rows.findLastIndex(hasData);
    return rows.slice(first, last + 1);
}

export function intervalTimeFormatter(
    locale: string,
    timeZone: string,
    timestamps: Iterable<string>,
): Intl.DateTimeFormat {
    const offsetFormatter = new Intl.DateTimeFormat(locale, {
        timeZone,
        timeZoneName: "shortOffset",
    });
    const offsets = new Set(
        Array.from(
            timestamps,
            (value) =>
                offsetFormatter
                    .formatToParts(new Date(value))
                    .find((part) => part.type === "timeZoneName")?.value,
        ),
    );
    return new Intl.DateTimeFormat(locale, {
        timeZone,
        hour: "2-digit",
        minute: "2-digit",
        hourCycle: "h23",
        ...(offsets.size > 1 ? { timeZoneName: "shortOffset" as const } : {}),
    });
}
