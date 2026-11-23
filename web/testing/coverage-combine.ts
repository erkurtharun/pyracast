type Location = { line: number; column: number };
type Span = { start: Location; end: Location };

export type FileCoverage = {
    path: string;
    statementMap: Record<string, Span>;
    s: Record<string, number>;
    fnMap: Record<string, unknown>;
    f: Record<string, number>;
    branchMap: Record<string, unknown>;
    b: Record<string, number[]>;
};
export type CoverageMap = Record<string, FileCoverage>;

function statementsOf(file: FileCoverage): [Span, number][] {
    return Object.entries(file.statementMap).map(([key, span]) => [
        span,
        file.s[key] ?? 0,
    ]);
}

function statementTables(
    statements: [Span, number][],
): Pick<FileCoverage, "statementMap" | "s"> {
    const merged = new Map<string, [Span, number]>();
    for (const [span, count] of statements) {
        const key = `${span.start.line}:${span.start.column}-${span.end.line}:${span.end.column}`;
        merged.set(key, [span, (merged.get(key)?.[1] ?? 0) + count]);
    }
    const ordered = [...merged.values()].sort(
        ([a], [b]) =>
            a.start.line - b.start.line ||
            a.start.column - b.start.column ||
            a.end.line - b.end.line ||
            a.end.column - b.end.column,
    );
    return {
        statementMap: Object.fromEntries(
            ordered.map(([span], index) => [String(index), span]),
        ),
        s: Object.fromEntries(
            ordered.map(([, count], index) => [String(index), count]),
        ),
    };
}

// Monocart gives a source that no generated statement maps to one statement over its whole text, counted as run.
export function withoutInventedStatements(
    file: FileCoverage,
    text: string,
): FileCoverage {
    const lines = text.split("\n");
    const invented = (span: Span) =>
        span.start.line === 1 &&
        span.start.column === 0 &&
        span.end.line === lines.length &&
        span.end.column === lines.at(-1)!.length;
    return {
        ...file,
        ...statementTables(
            statementsOf(file).filter(([span]) => !invented(span)),
        ),
    };
}

export function executed(file: FileCoverage | undefined): boolean {
    return (
        !!file &&
        (Object.values(file.s).some((count) => count > 0) ||
            Object.values(file.f).some((count) => count > 0) ||
            Object.values(file.b).some((arms) =>
                arms.some((count) => count > 0),
            ))
    );
}

function zeroed(file: FileCoverage): FileCoverage {
    const zero = (counts: Record<string, number>) =>
        Object.fromEntries(Object.keys(counts).map((key) => [key, 0]));
    return {
        ...file,
        s: zero(file.s),
        f: zero(file.f),
        b: Object.fromEntries(
            Object.entries(file.b).map(([key, arms]) => [
                key,
                arms.map(() => 0),
            ]),
        ),
    };
}

// Functions and branches of two compilations cannot be matched, so one side's tables are kept whole.
export function combineCoverage(sides: {
    browser: CoverageMap;
    server: CoverageMap;
    baseline: CoverageMap;
}): CoverageMap {
    return Object.fromEntries(
        Object.keys(sides.baseline)
            .sort()
            .map((name) => {
                const measured = [sides.browser[name], sides.server[name]];
                const tables = measured.find(executed);
                if (!tables) return [name, zeroed(sides.baseline[name]!)];
                return [
                    name,
                    {
                        ...tables,
                        path: name,
                        ...statementTables(
                            measured.flatMap((file) =>
                                file ? statementsOf(file) : [],
                            ),
                        ),
                    },
                ];
            }),
    );
}
