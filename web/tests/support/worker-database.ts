type Key = number | string | Key[];
type Entry = { key: Key; value: unknown };
type Stores = Record<string, Map<string, Entry>>;
type Handler = ((event: object) => void) | null;
type Request = {
    result: unknown;
    error: unknown;
    transaction: { abort: () => void } | null;
    onsuccess: Handler;
    onerror: Handler;
    onupgradeneeded: Handler;
};
type Stage = "open" | "readonly" | "readwrite";
type State = "waiting" | "running" | "committing" | "finished";

export type SavedRecords = {
    holder?: unknown;
    plants?: object[];
    days?: object[];
};

type Pause = {
    created: Promise<void>;
    reached: Promise<void>;
    release: () => void;
    fail: () => void;
};

const KEY_PATHS: Record<string, string[] | null> = {
    holder: null,
    plants: ["tenant", "site", "plant"],
    days: ["tenant", "site", "plant", "kind", "date"],
};

function rank(key: Key) {
    return typeof key === "number" ? 0 : typeof key === "string" ? 1 : 2;
}

function compareKeys(first: Key, second: Key): number {
    if (rank(first) !== rank(second)) return rank(first) - rank(second);
    if (Array.isArray(first) && Array.isArray(second)) {
        for (const [index, part] of first.entries()) {
            const other = second[index];
            if (other === undefined) return 1;
            const order = compareKeys(part, other);
            if (order !== 0) return order;
        }
        return first.length - second.length;
    }
    return first < second ? -1 : first > second ? 1 : 0;
}

function entriesOf(stores: Stores, name: string) {
    const entries = stores[name];
    if (!entries) throw new DOMException(name, "NotFoundError");
    return entries;
}

class KeyRange {
    constructor(
        readonly lower: Key,
        readonly upper: Key,
    ) {}

    includes(key: Key) {
        return (
            compareKeys(this.lower, key) <= 0 &&
            compareKeys(key, this.upper) <= 0
        );
    }
}

function keyOf(store: string, value: unknown, key?: Key): Key {
    const path = KEY_PATHS[store];
    if (key !== undefined || !path) return key as Key;
    return path.map((field) => (value as Record<string, Key>)[field] as Key);
}

function putInto(stores: Stores, store: string, value: unknown, key?: Key) {
    const stored = keyOf(store, value, key);
    entriesOf(stores, store).set(JSON.stringify(stored), {
        key: stored,
        value: structuredClone(value),
    });
}

function filled(records: SavedRecords): Stores {
    const stores: Stores = {
        holder: new Map(),
        plants: new Map(),
        days: new Map(),
    };
    if (records.holder !== undefined)
        putInto(stores, "holder", records.holder, "holder");
    for (const plant of records.plants ?? []) putInto(stores, "plants", plant);
    for (const day of records.days ?? []) putInto(stores, "days", day);
    return stores;
}

const copied = (stores: Stores): Stores =>
    Object.fromEntries(
        Object.entries(stores).map(([name, entries]) => [
            name,
            new Map(structuredClone([...entries])),
        ]),
    );

function gate() {
    let create = () => {};
    let reach = () => {};
    let decided: boolean | undefined;
    let waiting: ((passes: boolean) => void) | undefined;
    const decide = (passes: boolean) => {
        if (decided !== undefined) return;
        decided = passes;
        waiting?.(passes);
    };
    const pause: Pause = {
        created: new Promise<void>((resolve) => (create = resolve)),
        reached: new Promise<void>((resolve) => (reach = resolve)),
        release: () => decide(true),
        fail: () => decide(false),
    };
    return {
        pause,
        create: () => create(),
        hold: (go: (passes: boolean) => void) => {
            reach();
            if (decided === undefined) waiting = go;
            else go(decided);
        },
    };
}

// Transactions run one at a time in creation order; a paused one waits before it starts, as behind a lock.
export function savedDatabase(
    options: {
        version?: number;
        records?: SavedRecords;
        answers?: boolean;
    } = {},
) {
    let stores: Stores | undefined =
        options.records || options.version
            ? filled(options.records ?? {})
            : undefined;
    const version = options.version ?? 1;
    const opens: string[] = [];
    const transactions: string[] = [];
    const created: Record<Stage, number> = {
        open: 0,
        readonly: 0,
        readwrite: 0,
    };
    const pauses = new Map<string, ReturnType<typeof gate>>();
    const queue: (() => void)[] = [];
    const idlers: (() => void)[] = [];
    let busy = false;
    let connections = 0;

    function pause(stage: Stage, nth = 0) {
        const held = gate();
        pauses.set(`${stage} ${created[stage] + nth}`, held);
        return held.pause;
    }

    function take(stage: Stage) {
        const name = `${stage} ${created[stage]++}`;
        const held = pauses.get(name);
        pauses.delete(name);
        held?.create();
        return held;
    }

    function next() {
        if (busy) return;
        const start = queue.shift();
        if (!start) return wake();
        busy = true;
        start();
    }

    function wake() {
        if (busy || queue.length > 0 || connections > 0) return;
        for (const resolve of idlers.splice(0)) resolve();
    }

    function transaction(names: string[], mode: "readonly" | "readwrite") {
        transactions.push(`${mode} ${names.join(",")}`);
        const held = take(mode);
        let state: State = "waiting";
        let lock: "waiting" | "held" | "released" = "waiting";
        let working = stores!;
        let pending = 0;
        const deferred: (() => void)[] = [];
        const release = () => {
            if (lock !== "held") return;
            lock = "released";
            busy = false;
            next();
        };
        const finish = (commit: boolean, error: unknown) => {
            if (state === "finished") return;
            state = "finished";
            if (commit && mode === "readwrite") stores = working;
            handle.error = error;
            queueMicrotask(() => {
                (commit ? handle.oncomplete : handle.onabort)?.({});
                release();
            });
        };
        const settle = () =>
            queueMicrotask(() => {
                if (state !== "running" || pending > 0) return;
                state = "committing";
                finish(true, null);
            });
        const run = () => {
            if (state !== "waiting") return;
            state = "running";
            working = mode === "readwrite" ? copied(stores!) : stores!;
            for (const perform of deferred.splice(0)) perform();
            settle();
        };
        const start = () => {
            lock = "held";
            if (state === "finished") release();
            else if (held)
                held.hold((passes) =>
                    passes
                        ? run()
                        : finish(
                              false,
                              new DOMException("write failed", "UnknownError"),
                          ),
                );
            else run();
        };
        const handle = {
            error: null as unknown,
            oncomplete: null as Handler,
            onerror: null as Handler,
            onabort: null as Handler,
            objectStore: (name: string) => store(name),
            abort: () => {
                if (state === "committing" || state === "finished")
                    throw new DOMException("finished", "InvalidStateError");
                finish(false, null);
            },
        };
        const request = (work: () => unknown) => {
            if (state === "committing" || state === "finished")
                throw new DOMException("finished", "TransactionInactiveError");
            const answer = {
                result: undefined as unknown,
                onsuccess: null as Handler,
            };
            pending++;
            const perform = () =>
                queueMicrotask(() => {
                    if (state !== "running") return;
                    answer.result = work();
                    answer.onsuccess?.({ target: answer });
                    pending--;
                    settle();
                });
            if (state === "running") perform();
            else deferred.push(perform);
            return answer;
        };
        const store = (name: string) => {
            if (!names.includes(name))
                throw new DOMException(name, "NotFoundError");
            const entries = () => entriesOf(working, name);
            const sorted = (range?: KeyRange) =>
                [...entries().values()]
                    .filter(({ key }) => !range || range.includes(key))
                    .sort((first, second) => compareKeys(first.key, second.key))
                    .map((entry) => structuredClone(entry));
            const writable = () => {
                if (mode !== "readwrite")
                    throw new DOMException(name, "ReadOnlyError");
            };
            return {
                get: (key: Key) =>
                    request(() =>
                        structuredClone(
                            entries().get(JSON.stringify(key))?.value,
                        ),
                    ),
                getAll: (range?: KeyRange) =>
                    request(() => sorted(range).map(({ value }) => value)),
                getAllKeys: (range?: KeyRange) =>
                    request(() => sorted(range).map(({ key }) => key)),
                clear: () => {
                    writable();
                    return request(() => entries().clear());
                },
                put: (value: unknown, key?: Key) => {
                    writable();
                    return request(() => putInto(working, name, value, key));
                },
            };
        };
        queue.push(start);
        next();
        return handle;
    }

    function connection() {
        connections++;
        let closed = false;
        return {
            version,
            onversionchange: null as Handler,
            transaction: (
                names: string[],
                mode: "readonly" | "readwrite" = "readonly",
            ) => {
                if (closed)
                    throw new DOMException("closed", "InvalidStateError");
                return transaction(names, mode);
            },
            close: () => {
                if (!closed) connections--;
                closed = true;
                wake();
            },
        };
    }

    function answer(request: Request) {
        if (!stores) {
            let aborted = false;
            request.transaction = { abort: () => (aborted = true) };
            request.onupgradeneeded?.({ oldVersion: 0 });
            if (aborted) {
                request.error = new DOMException("aborted", "AbortError");
                request.onerror?.({});
                return;
            }
            stores = filled({});
        }
        request.result = connection();
        request.onsuccess?.({});
    }

    const indexedDB = {
        open(name: string) {
            opens.push(name);
            const request: Request = {
                result: undefined,
                error: null,
                transaction: null,
                onsuccess: null,
                onerror: null,
                onupgradeneeded: null,
            };
            const held = take("open");
            if (options.answers === false) return request;
            queueMicrotask(() => {
                if (!held) answer(request);
                else
                    held.hold((passes) => {
                        if (passes) answer(request);
                        else {
                            request.error = new DOMException(
                                "denied",
                                "UnknownError",
                            );
                            request.onerror?.({});
                        }
                    });
            });
            return request;
        },
    };

    const values = (name: string) =>
        stores
            ? [...entriesOf(stores, name).values()].map(({ value }) => value)
            : [];

    return {
        indexedDB,
        IDBKeyRange: {
            bound: (lower: Key, upper: Key) => new KeyRange(lower, upper),
        },
        opens,
        transactions,
        pause,
        idle: () =>
            new Promise<void>((resolve) => {
                idlers.push(resolve);
                wake();
            }),
        put: (store: string, value: unknown, key?: Key) =>
            putInto(stores!, store, value, key),
        replace: (records: SavedRecords) => {
            stores = filled(records);
        },
        openConnections: () => connections,
        exists: () => stores !== undefined,
        holder: () => values("holder")[0],
        plants: () => values("plants"),
        days: () => values("days"),
    };
}

export type SavedDatabase = ReturnType<typeof savedDatabase>;
