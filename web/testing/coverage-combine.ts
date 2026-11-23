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
export type ScriptRecords = Record<string, Record<string, FileCoverage>>;
export type BranchEvidence = {
    groups: number;
    arms: number;
    taken: number;
    sources: number;
};

export function byCodeUnit(a: string, b: string): number {
    if (a === b) return 0;
    return a < b ? -1 : 1;
}

// The line terminators of monocart-locator (lib/line-parser.js), which places the converter's positions.
export function lineCount(text: string): number {
    return text.split(/\r\n|[\r\n\u{2028}\u{2029}]/u).length;
}

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

// `converted` is the text the converter received; its statement for an unmapped source ends inside the last line.
export function withoutFallback(
    file: FileCoverage,
    converted: string,
): FileCoverage {
    const last = lineCount(converted);
    return {
        ...file,
        ...statementTables(
            statementsOf(file).filter(
                ([{ end }]) => end.line !== last || end.column === 0,
            ),
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

function added(first: FileCoverage, second: FileCoverage): FileCoverage {
    return {
        ...first,
        ...statementTables([...statementsOf(first), ...statementsOf(second)]),
        f: Object.fromEntries(
            Object.entries(first.f).map(([key, count]) => [
                key,
                count + (second.f[key] ?? 0),
            ]),
        ),
        b: Object.fromEntries(
            Object.entries(first.b).map(([key, arms]) => [
                key,
                arms.map((count, arm) => count + (second.b[key]?.[arm] ?? 0)),
            ]),
        ),
    };
}

function compilations(
    records: Record<string, FileCoverage> | undefined,
): FileCoverage[] {
    const groups = new Map<string, FileCoverage>();
    for (const script of Object.keys(records ?? {}).sort(byCodeUnit)) {
        const record = records![script]!;
        const tables = JSON.stringify([
            Object.values(record.fnMap),
            Object.values(record.branchMap),
        ]);
        const group = groups.get(tables);
        groups.set(tables, group ? added(group, record) : record);
    }
    return [...groups.values()];
}

const noRecord = (name: string): FileCoverage => ({
    path: name,
    statementMap: {},
    s: {},
    fnMap: {},
    f: {},
    branchMap: {},
    b: {},
});

export function combineCoverage(sides: {
    sources: string[];
    browser: ScriptRecords;
    server: ScriptRecords;
    baseline: ScriptRecords[];
}): CoverageMap {
    return Object.fromEntries(
        sides.sources.toSorted(byCodeUnit).map((name) => {
            const baseline = sides.baseline.map((records) =>
                compilations(records[name]).map(zeroed),
            );
            const measured = [sides.browser[name], sides.server[name]];
            const tables =
                compilations(sides.browser[name]).find(executed) ??
                compilations(sides.server[name]).find(executed) ??
                baseline.flat()[0] ??
                noRecord(name);
            return [
                name,
                {
                    path: name,
                    fnMap: tables.fnMap,
                    f: tables.f,
                    branchMap: tables.branchMap,
                    b: tables.b,
                    ...statementTables(
                        [
                            ...baseline.flat(),
                            ...measured.flatMap((records) =>
                                Object.values(records ?? {}),
                            ),
                        ].flatMap(statementsOf),
                    ),
                },
            ];
        }),
    );
}

function branchIdentity(branch: unknown): string {
    const { type, loc, locations } = branch as Record<string, unknown>;
    return JSON.stringify([type, loc, locations]);
}

export function branchEvidence(
    sides: { browser: ScriptRecords; server: ScriptRecords },
    combined: CoverageMap,
): BranchEvidence {
    const evidence = { groups: 0, arms: 0, taken: 0, sources: 0 };
    for (const [name, file] of Object.entries(combined)) {
        const records = [sides.browser[name], sides.server[name]].flatMap(
            (scripts) => Object.values(scripts ?? {}),
        );
        if (!records.some(executed)) continue;
        const selected = new Set(
            Object.values(file.branchMap).map(branchIdentity),
        );
        const absent = new Map<string, number[]>();
        for (const record of records)
            for (const [key, branch] of Object.entries(record.branchMap)) {
                const identity = branchIdentity(branch);
                if (selected.has(identity)) continue;
                const counts = record.b[key] ?? [];
                const known = absent.get(identity);
                absent.set(
                    identity,
                    known
                        ? known.map((count, arm) => count + (counts[arm] ?? 0))
                        : [...counts],
                );
            }
        if (!absent.size) continue;
        evidence.sources += 1;
        evidence.groups += absent.size;
        for (const arms of absent.values()) {
            evidence.arms += arms.length;
            evidence.taken += arms.filter((count) => count > 0).length;
        }
    }
    return evidence;
}
