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

export type SavedRecords = {
    holder?: unknown;
    plants?: object[];
    days?: object[];
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
    if (!path) return key as Key;
    return path.map((field) => (value as Record<string, Key>)[field] as Key);
}

function filled(records: SavedRecords): Stores {
    const stores: Stores = {
        holder: new Map(),
        plants: new Map(),
        days: new Map(),
    };
    const put = (store: string, value: unknown, key?: Key) => {
        const stored = keyOf(store, value, key);
        entriesOf(stores, store).set(JSON.stringify(stored), {
            key: stored,
            value: structuredClone(value),
        });
    };
    if (records.holder !== undefined) put("holder", records.holder, "holder");
    for (const plant of records.plants ?? []) put("plants", plant);
    for (const day of records.days ?? []) put("days", day);
    return stores;
}

const copied = (stores: Stores): Stores =>
    Object.fromEntries(
        Object.entries(stores).map(([name, entries]) => [
            name,
            new Map(structuredClone([...entries])),
        ]),
    );

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
    let connections = 0;

    function transaction(names: string[], mode: string) {
        transactions.push(`${mode} ${names.join(",")}`);
        const working = mode === "readwrite" ? copied(stores!) : stores!;
        let pending = 0;
        let finished = false;
        const handle = {
            error: null,
            oncomplete: null as Handler,
            onerror: null as Handler,
            onabort: null as Handler,
            objectStore: (name: string) => store(name),
        };
        const settle = () =>
            queueMicrotask(() => {
                if (pending > 0 || finished) return;
                finished = true;
                if (mode === "readwrite") stores = working;
                handle.oncomplete?.({});
            });
        const request = (run: () => unknown) => {
            if (finished)
                throw new DOMException("finished", "InvalidStateError");
            const answer = {
                result: undefined as unknown,
                onsuccess: null as Handler,
            };
            pending++;
            queueMicrotask(() => {
                answer.result = run();
                answer.onsuccess?.({ target: answer });
                pending--;
                settle();
            });
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
                    .map(({ value }) => structuredClone(value));
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
                getAll: (range?: KeyRange) => request(() => sorted(range)),
                clear: () => {
                    writable();
                    return request(() => entries().clear());
                },
                put: (value: unknown, key?: Key) => {
                    writable();
                    return request(() => {
                        const stored = keyOf(name, value, key);
                        entries().set(JSON.stringify(stored), {
                            key: stored,
                            value: structuredClone(value),
                        });
                    });
                },
            };
        };
        settle();
        return handle;
    }

    function connection() {
        connections++;
        let closed = false;
        return {
            version,
            onversionchange: null as Handler,
            transaction: (names: string[], mode = "readonly") => {
                if (closed)
                    throw new DOMException("closed", "InvalidStateError");
                return transaction(names, mode);
            },
            close: () => {
                if (!closed) connections--;
                closed = true;
            },
        };
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
            if (options.answers === false) return request;
            queueMicrotask(() => {
                if (!stores) {
                    let aborted = false;
                    request.transaction = { abort: () => (aborted = true) };
                    request.onupgradeneeded?.({ oldVersion: 0 });
                    if (aborted) {
                        request.error = new DOMException(
                            "aborted",
                            "AbortError",
                        );
                        request.onerror?.({});
                        return;
                    }
                    stores = filled({});
                }
                request.result = connection();
                request.onsuccess?.({});
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
        openConnections: () => connections,
        exists: () => stores !== undefined,
        holder: () => values("holder")[0],
        plants: () => values("plants"),
        days: () => values("days"),
    };
}

export type SavedDatabase = ReturnType<typeof savedDatabase>;
