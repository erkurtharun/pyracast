type Location = { line: number; column: number };
type Span = { start: Location; end: Location };
type FunctionMeta = { name?: string; decl?: Span; loc?: Span; line?: number };
type BranchMeta = {
    type?: string;
    loc?: Span;
    locations?: Partial<Span>[];
    line?: number;
};

export type FileCoverage = {
    path: string;
    statementMap: Record<string, Span>;
    s: Record<string, number>;
    fnMap: Record<string, FunctionMeta>;
    f: Record<string, number>;
    branchMap: Record<string, BranchMeta>;
    b: Record<string, number[]>;
};
export type CoverageMap = Record<string, FileCoverage>;
export type ScriptRecords = Record<string, Record<string, FileCoverage>>;
type Sides = {
    sources: string[];
    browser: ScriptRecords;
    server: ScriptRecords;
    baseline: ScriptRecords;
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

function recordsOf(sides: Sides, name: string): FileCoverage[] {
    const scripts = (records: Record<string, FileCoverage> | undefined) =>
        Object.keys(records ?? {})
            .sort(byCodeUnit)
            .map((script) => records![script]!);
    return [
        ...scripts(sides.browser[name]),
        ...scripts(sides.server[name]),
        ...scripts(sides.baseline[name]).map(zeroed),
    ];
}

// Equal locations within one record stay distinct items: merged, one would be credited by the other.
function located<T>(items: T[], location: (item: T) => unknown): [string, T][] {
    const seen = new Map<string, number>();
    return items.map((item) => {
        const identity = JSON.stringify(location(item));
        const occurrence = seen.get(identity) ?? 0;
        seen.set(identity, occurrence + 1);
        return [`${identity}#${occurrence}`, item];
    });
}

function byPosition<T>(
    items: Map<string, T>,
    location: (item: T) => Partial<Span> | undefined,
): T[] {
    const start = (item: T) => location(item)?.start;
    return [...items]
        .sort(
            ([a, x], [b, y]) =>
                (start(x)?.line ?? Infinity) - (start(y)?.line ?? Infinity) ||
                (start(x)?.column ?? Infinity) -
                    (start(y)?.column ?? Infinity) ||
                byCodeUnit(a, b),
        )
        .map(([, item]) => item);
}

type Arm = { location: Partial<Span>; count: number; records: number };
type Group = { branch: BranchMeta; records: number; arms: Map<string, Arm> };

function united(records: FileCoverage[]) {
    const functions = new Map<string, { fn: FunctionMeta; count: number }>();
    const groups = new Map<string, Group>();
    for (const record of records) {
        for (const [identity, [key, fn]] of located(
            Object.entries(record.fnMap),
            ([, { decl, loc }]) => [decl, loc],
        )) {
            const known = functions.get(identity);
            functions.set(identity, {
                fn: known?.fn ?? fn,
                count: (known?.count ?? 0) + (record.f[key] ?? 0),
            });
        }
        for (const [identity, [key, branch]] of located(
            Object.entries(record.branchMap),
            ([, { type, loc }]) => [type, loc],
        )) {
            const group = groups.get(identity) ?? {
                branch,
                records: 0,
                arms: new Map<string, Arm>(),
            };
            group.records += 1;
            const counts = record.b[key] ?? [];
            for (const [arm, [index, location]] of located(
                [...(branch.locations ?? []).entries()],
                ([, location]) => location,
            )) {
                const known = group.arms.get(arm);
                group.arms.set(arm, {
                    location: known?.location ?? location,
                    count: (known?.count ?? 0) + (counts[index] ?? 0),
                    records: (known?.records ?? 0) + 1,
                });
            }
            groups.set(identity, group);
        }
    }
    return { functions, groups };
}

function tables(
    records: FileCoverage[],
): Pick<FileCoverage, "fnMap" | "f" | "branchMap" | "b"> {
    const { functions, groups } = united(records);
    const fns = byPosition(functions, ({ fn }) => fn.decl);
    const branches = byPosition(groups, ({ branch }) => branch.loc).map(
        ({ branch, arms }) => ({
            branch,
            arms: byPosition(arms, ({ location }) => location),
        }),
    );
    return {
        fnMap: Object.fromEntries(
            fns.map(({ fn }, index) => [
                String(index),
                {
                    ...fn,
                    name: /^\(anonymous_\d+\)$/.test(fn.name ?? "")
                        ? `(anonymous_${index})`
                        : fn.name,
                },
            ]),
        ),
        f: Object.fromEntries(
            fns.map(({ count }, index) => [String(index), count]),
        ),
        branchMap: Object.fromEntries(
            branches.map(({ branch, arms }, index) => [
                String(index),
                { ...branch, locations: arms.map(({ location }) => location) },
            ]),
        ),
        b: Object.fromEntries(
            branches.map(({ arms }, index) => [
                String(index),
                arms.map(({ count }) => count),
            ]),
        ),
    };
}

export function combineCoverage(sides: Sides): CoverageMap {
    return Object.fromEntries(
        sides.sources.toSorted(byCodeUnit).map((name) => {
            const records = recordsOf(sides, name);
            return [
                name,
                {
                    path: name,
                    ...statementTables(records.flatMap(statementsOf)),
                    ...tables(records),
                },
            ];
        }),
    );
}

// Two compilations that place one authored arm at two locations count it twice; this bounds that excess.
export function differingArms(sides: Sides): {
    groups: number;
    untaken: number;
} {
    const residue = { groups: 0, untaken: 0 };
    for (const name of sides.sources) {
        for (const group of united(recordsOf(sides, name)).groups.values()) {
            const partial = [...group.arms.values()].filter(
                (arm) => arm.records < group.records,
            );
            if (!partial.length) continue;
            residue.groups += 1;
            residue.untaken += partial.filter((arm) => !arm.count).length;
        }
    }
    return residue;
}
