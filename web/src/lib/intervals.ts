export function dataIntervalRange<T>(
    rows: T[],
    hasData: (row: T) => boolean,
): T[] {
    const first = rows.findIndex(hasData);
    if (first === -1) return [];
    const last = rows.findLastIndex(hasData);
    return rows.slice(first, last + 1);
}
