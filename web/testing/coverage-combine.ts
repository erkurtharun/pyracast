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

type Shapes = {
    contested: { functions: number; groups: number; arms: number };
    executedLeftOut: number;
    partialArms: number;
    otherTypePairs: number;
};
type Pairing<T> = {
    slots: Map<string, T[]>;
    contested: number;
    leftOut: T[];
    partial: number;
};

function append<T>(lists: Map<string, T[]>, key: string, item: T): void {
    const list = lists.get(key);
    if (list) list.push(item);
    else lists.set(key, [item]);
}

// One authored construct can compile to differently many items of one end, so a record with fewer cannot be matched.
function paired<T>(
    records: T[][],
    identity: (item: T) => string,
    range: (item: T) => Place | undefined,
): Pairing<T> {
    const start = (item: T) => range(item)?.start;
    const carried = new Map<string, T[][]>();
    for (const items of records) {
        const own = new Map<string, T[]>();
        for (const item of items.toSorted(
            (a, b) =>
                (start(a)?.line ?? Number.MAX_SAFE_INTEGER) -
                    (start(b)?.line ?? Number.MAX_SAFE_INTEGER) ||
                (start(a)?.column ?? Number.MAX_SAFE_INTEGER) -
                    (start(b)?.column ?? Number.MAX_SAFE_INTEGER),
        ))
            append(own, identity(item), item);
        for (const [key, list] of own) append(carried, key, list);
    }
    const pairing: Pairing<T> = {
        slots: new Map(),
        contested: 0,
        leftOut: [],
        partial: 0,
    };
    for (const [key, lists] of carried) {
        const most = Math.max(...lists.map(({ length }) => length));
        if (lists.some(({ length }) => length < most)) pairing.contested += 1;
        if (lists.length < records.length) pairing.partial += most;
        for (const list of lists) {
            if (list.length < most) pairing.leftOut.push(...list);
            else
                list.forEach((item, index) =>
                    append(pairing.slots, `${key}#${index}`, item),
                );
        }
    }
    return pairing;
}

const total = (items: { count: number }[]) =>
    items.reduce((sum, { count }) => sum + count, 0);
const executions = (counts: number[]) =>
    counts.filter((count) => count > 0).length;

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

function unitedArms(
    groups: { branch: BranchMeta; counts: number[] }[],
    shapes: Shapes,
): Map<string, Arm> {
    const arms = paired(
        groups.map(({ branch, counts }) =>
            (branch.locations ?? []).map((location, index) => ({
                location,
                count: counts[index] ?? 0,
            })),
        ),
        ({ location }) => point(location.end),
        ({ location }) => location,
    );
    shapes.contested.arms += arms.contested;
    shapes.executedLeftOut += executions(
        arms.leftOut.map(({ count }) => count),
    );
    shapes.partialArms += arms.partial;
    return new Map(
        [...arms.slots].map(([slot, items]) => [
            slot,
            { location: items[0]!.location, count: total(items) },
        ]),
    );
}

function tables(
    records: FileCoverage[],
    shapes: Shapes,
): Pick<FileCoverage, "fnMap" | "f" | "branchMap" | "b"> {
    const functions = paired(
        records.map((record) =>
            Object.entries(record.fnMap).map(([key, fn]) => ({
                fn,
                count: record.f[key] ?? 0,
            })),
        ),
        ({ fn }) => point(fn.loc?.end),
        ({ fn }) => fn.loc,
    );
    shapes.contested.functions += functions.contested;
    shapes.executedLeftOut += executions(
        functions.leftOut.map(({ count }) => count),
    );
    const groups = paired(
        records.map((record) =>
            Object.entries(record.branchMap).map(([key, branch]) => ({
                branch,
                counts: record.b[key] ?? [],
            })),
        ),
        ({ branch }) => `${branch.type ?? ""}@${point(branch.loc?.end)}`,
        ({ branch }) => branch.loc,
    );
    shapes.contested.groups += groups.contested;
    shapes.executedLeftOut += executions(
        groups.leftOut.flatMap(({ counts }) => counts),
    );
    const fns = byPosition(
        new Map(
            [...functions.slots].map(([slot, items]) => [
                slot,
                { fn: items[0]!.fn, count: total(items) },
            ]),
        ),
        ({ fn }) => fn.decl,
    );
    const branches = byPosition(
        new Map(
            [...groups.slots].map(([slot, items]) => [
                slot,
                { branch: items[0]!.branch, arms: unitedArms(items, shapes) },
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

function otherTypePairs(records: FileCoverage[]): number {
    const typesAtEnd = new Map<string, Set<string>[]>();
    for (const record of records) {
        const own = new Map<string, Set<string>>();
        for (const { type, loc } of Object.values(record.branchMap)) {
            const end = point(loc?.end);
            own.set(end, (own.get(end) ?? new Set()).add(type ?? ""));
        }
        for (const [end, types] of own) append(typesAtEnd, end, types);
    }
    let pairs = 0;
    for (const sets of typesAtEnd.values()) {
        const types = [...new Set(sets.flatMap((set) => [...set]))];
        const without = (one: string, other: string) =>
            sets.some((set) => set.has(one) && !set.has(other));
        types.forEach((one, index) => {
            for (const other of types.slice(index + 1))
                if (without(one, other) && without(other, one)) pairs += 1;
        });
    }
    return pairs;
}

export function combineCoverage(sides: Sides): {
    coverage: CoverageMap;
    shapes: Shapes;
} {
    const shapes: Shapes = {
        contested: { functions: 0, groups: 0, arms: 0 },
        executedLeftOut: 0,
        partialArms: 0,
        otherTypePairs: 0,
    };
    const coverage = Object.fromEntries(
        sides.sources.toSorted(byCodeUnit).map((name) => {
            const records = recordsOf(sides, name);
            shapes.otherTypePairs += otherTypePairs(records);
            return [
                name,
                {
                    path: name,
                    ...statementTables(records.flatMap(statementsOf)),
                    ...tables(records, shapes),
                },
            ];
        }),
    );
    return { coverage, shapes };
}
