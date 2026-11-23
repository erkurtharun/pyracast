type Location = { line: number; column: number };
type Span = { start: Location; end: Location };
type Place = { start?: Partial<Location>; end?: Partial<Location> };
type FunctionMeta = { name?: string; decl?: Span; loc?: Span; line?: number };
type BranchMeta = {
    type?: string;
    loc?: Span;
    locations?: Place[];
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

const point = (at: Partial<Location> | undefined) =>
    at?.line === undefined ? "none" : `${at.line}:${at.column}`;
const extent = (place: Place | undefined) =>
    `${point(place?.start)}-${point(place?.end)}`;

type UnionStatistics = {
    unitedOnEqualRanges: { functions: number; groups: number };
    armsSomeRecordLacks: number;
};
type Member<T> = { item: T; record: number };
type Union<T> = { united: Map<string, Member<T>[]>; onEqualRanges: number };

// Different compilations can keep different siblings under one mapped end, so a repeated identity needs equal ranges.
function unite<T>(
    records: T[][],
    identity: (item: T) => string,
    range: (item: T) => string,
): Union<T> {
    const keyed = records.map((items, record) =>
        items.map((item) => ({ item, record, id: identity(item) })),
    );
    const repeated = new Set<string>();
    for (const items of keyed) {
        const seen = new Set<string>();
        for (const { id } of items) {
            if (seen.has(id)) repeated.add(id);
            seen.add(id);
        }
    }
    const united = new Map<string, Member<T>[]>();
    for (const { item, record, id } of keyed.flat()) {
        const key = repeated.has(id) ? `${id}=${range(item)}` : id;
        const members = united.get(key);
        if (members) members.push({ item, record });
        else united.set(key, [{ item, record }]);
    }
    return { united, onEqualRanges: repeated.size };
}

const total = (members: Member<{ count: number }>[]) =>
    members.reduce((sum, { item }) => sum + item.count, 0);

function byPosition<T>(
    items: Map<string, T>,
    location: (item: T) => Place | undefined,
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

type Arm = { location: Place; count: number };
type Group = { branch: BranchMeta; counts: number[] };

function unitedArms(
    groups: Member<Group>[],
    statistics: UnionStatistics,
): Map<string, Arm> {
    const arms = unite(
        groups.map(({ item: { branch, counts } }) =>
            (branch.locations ?? []).map((location, index) => ({
                location,
                count: counts[index] ?? 0,
            })),
        ),
        ({ location }) => point(location.end),
        ({ location }) => extent(location),
    );
    const records = (members: { record: number }[]) =>
        new Set(members.map(({ record }) => groups[record]!.record)).size;
    const present = new Set(groups.map(({ record }) => record)).size;
    return new Map(
        [...arms.united].map(([key, members]) => {
            if (records(members) < present) statistics.armsSomeRecordLacks += 1;
            return [
                key,
                { location: members[0]!.item.location, count: total(members) },
            ];
        }),
    );
}

function tables(
    records: FileCoverage[],
    statistics: UnionStatistics,
): Pick<FileCoverage, "fnMap" | "f" | "branchMap" | "b"> {
    const functions = unite(
        records.map((record) =>
            Object.entries(record.fnMap).map(([key, fn]) => ({
                fn,
                count: record.f[key] ?? 0,
            })),
        ),
        ({ fn }) => point(fn.loc?.end),
        ({ fn }) => `${extent(fn.decl)} ${extent(fn.loc)}`,
    );
    statistics.unitedOnEqualRanges.functions += functions.onEqualRanges;
    const groups = unite<Group>(
        records.map((record) =>
            Object.entries(record.branchMap).map(([key, branch]) => ({
                branch,
                counts: record.b[key] ?? [],
            })),
        ),
        ({ branch }) =>
            `${branch.type ?? ""}@${branch.loc?.start.line ?? "none"}@${point(branch.loc?.end)}`,
        ({ branch }) => `${branch.type ?? ""} ${extent(branch.loc)}`,
    );
    statistics.unitedOnEqualRanges.groups += groups.onEqualRanges;
    const fns = byPosition(
        new Map(
            [...functions.united].map(([key, members]) => [
                key,
                { fn: members[0]!.item.fn, count: total(members) },
            ]),
        ),
        ({ fn }) => fn.decl,
    );
    const branches = byPosition(
        new Map(
            [...groups.united].map(([key, members]) => [
                key,
                {
                    branch: members[0]!.item.branch,
                    arms: unitedArms(members, statistics),
                },
            ]),
        ),
        ({ branch }) => branch.loc,
    ).map(({ branch, arms }) => ({
        branch,
        arms: byPosition(arms, ({ location }) => location),
    }));
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

export function combineCoverage(sides: Sides): {
    coverage: CoverageMap;
    statistics: UnionStatistics;
} {
    const statistics: UnionStatistics = {
        unitedOnEqualRanges: { functions: 0, groups: 0 },
        armsSomeRecordLacks: 0,
    };
    const coverage = Object.fromEntries(
        sides.sources.toSorted(byCodeUnit).map((name) => {
            const records = recordsOf(sides, name);
            return [
                name,
                {
                    path: name,
                    ...statementTables(records.flatMap(statementsOf)),
                    ...tables(records, statistics),
                },
            ];
        }),
    );
    return { coverage, statistics };
}
