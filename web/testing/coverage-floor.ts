const counters = {
    lines: ["LF", "LH"],
    branches: ["BRF", "BRH"],
    functions: ["FNF", "FNH"],
} as const;

type Metric = keyof typeof counters;
export type Floor = Record<Metric, number>;
type Totals = Record<Metric, { found: number; hit: number }>;

const metrics = Object.keys(counters) as Metric[];

export function lcovTotals(lcov: string): Totals {
    const sums = new Map<string, number>();
    for (const [, key, value] of lcov.matchAll(/^([A-Z]+):(\d+)$/gm))
        sums.set(key!, (sums.get(key!) ?? 0) + Number(value));
    return Object.fromEntries(
        metrics.map((metric) => {
            const [found, hit] = counters[metric];
            return [
                metric,
                { found: sums.get(found) ?? 0, hit: sums.get(hit) ?? 0 },
            ];
        }),
    ) as Totals;
}

// Percentages with one decimal, rounded down, so a floor copied from a report is met by it.
export function coverageShortfalls(lcov: string, floor: Floor): string[] {
    const totals = lcovTotals(lcov);
    return metrics.flatMap((metric) => {
        const { found, hit } = totals[metric];
        const percent = found ? Math.floor((hit * 1000) / found) / 10 : 0;
        return percent < floor[metric]
            ? [`${metric} ${percent}% is below the floor of ${floor[metric]}%`]
            : [];
    });
}
