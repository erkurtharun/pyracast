const SAVED_FIGURES = true;

const databaseName = "pyracast-saved";
const format = 1;
const holderKey = "holder";
const storeNames = ["holder", "plants", "days"];
const dayMs = 86_400_000;
const keptForMs = 90 * dayMs;
const answerLimitMs = 2_000;
const latestInstant = 8_640_000_000_000_000;

type Holder = {
    user: number;
    language: "en" | "tr";
    session: string;
    active: boolean;
    confirmedAt: number;
    removal: string;
    retryAt: number | null;
};
export type CopyState = "off" | "on" | "paused" | "unavailable";
export type Observation = {
    user: number;
    session: string;
    language: Holder["language"];
    adoptedAt: number;
    present: () => boolean;
};
export type Removal = {
    outstanding: () => string | null;
    acknowledge: (removal: string) => void;
};
type Handoff = "verified" | "cleared" | "superseded" | "unverified";
type Outcome = "done" | "absent" | "unavailable" | "failed" | "pending";

function deadline() {
    let timer: ReturnType<typeof setTimeout> | undefined;
    const reached = new Promise<"pending">((resolve) => {
        timer = setTimeout(() => resolve("pending"), answerLimitMs);
    });
    return { reached, clear: () => clearTimeout(timer) };
}

function openExisting(
    reached: Promise<unknown>,
): Promise<IDBDatabase | "absent" | "unavailable"> {
    return new Promise((resolve) => {
        let request: IDBOpenDBRequest;
        try {
            request = indexedDB.open(databaseName, format);
        } catch {
            resolve("unavailable");
            return;
        }
        let answered = false;
        let absent = false;
        const answer = (value: IDBDatabase | "absent" | "unavailable") => {
            answered = true;
            resolve(value);
        };
        request.onupgradeneeded = () => {
            absent = true;
            request.transaction?.abort();
        };
        request.onsuccess = () => {
            const database = request.result;
            if (answered) {
                database.close();
                return;
            }
            database.onversionchange = () => database.close();
            answer(database);
        };
        request.onerror = () => answer(absent ? "absent" : "unavailable");
        void reached.then(() => answer("unavailable"));
    });
}

function run(
    database: IDBDatabase,
    mode: IDBTransactionMode,
    work: (transaction: IDBTransaction) => void,
): Promise<Outcome> {
    return new Promise<Outcome>((resolve) => {
        try {
            const transaction = database.transaction(storeNames, mode);
            transaction.oncomplete = () => resolve("done");
            transaction.onabort = () => resolve("failed");
            try {
                work(transaction);
            } catch {
                transaction.abort();
            }
        } catch {
            resolve("failed");
        }
    }).finally(() => database.close());
}

// A transaction still waiting at the limit goes on; work that must not land late re-checks inside it.
async function transact(
    mode: IDBTransactionMode,
    work: (transaction: IDBTransaction) => void,
): Promise<Outcome> {
    const limit = deadline();
    try {
        const database = await openExisting(limit.reached);
        if (typeof database === "string") return database;
        return await Promise.race([run(database, mode, work), limit.reached]);
    } finally {
        limit.clear();
    }
}

function withHolder(
    transaction: IDBTransaction,
    read: (holder: unknown, store: IDBObjectStore) => void,
) {
    const store = transaction.objectStore("holder");
    const request = store.get(holderKey);
    request.onsuccess = () => {
        try {
            read(request.result, store);
        } catch {
            transaction.abort();
        }
    };
}

function isInstant(value: unknown): value is number {
    return (
        typeof value === "number" &&
        Number.isInteger(value) &&
        Math.abs(value) <= latestInstant
    );
}

function isHolder(value: unknown): value is Holder {
    if (typeof value !== "object" || value === null) return false;
    const { user, language, session, active, confirmedAt, removal, retryAt } =
        value as Record<string, unknown>;
    return (
        typeof user === "number" &&
        Number.isInteger(user) &&
        user > 0 &&
        (language === "en" || language === "tr") &&
        typeof session === "string" &&
        typeof active === "boolean" &&
        isInstant(confirmedAt) &&
        typeof removal === "string" &&
        /^[0-9a-f]{32}$/.test(removal) &&
        (retryAt === null || isInstant(retryAt))
    );
}

function clearFigures(transaction: IDBTransaction) {
    transaction.objectStore("plants").clear();
    transaction.objectStore("days").clear();
}

function clearAll(transaction: IDBTransaction) {
    transaction.objectStore("holder").clear();
    clearFigures(transaction);
}

function pause(
    transaction: IDBTransaction,
    holder: unknown,
    store: IDBObjectStore,
) {
    clearFigures(transaction);
    if (isHolder(holder)) store.put({ ...holder, active: false }, holderKey);
    else if (holder !== undefined) store.delete(holderKey);
}

function current(confirmedAt: number, now: number) {
    return now - confirmedAt <= keptForMs && confirmedAt - now <= dayMs;
}

function deleteDatabase(): Promise<"deleted" | "failed" | "pending"> {
    const limit = deadline();
    return new Promise<"deleted" | "failed" | "pending">((resolve) => {
        try {
            const request = indexedDB.deleteDatabase(databaseName);
            request.onsuccess = () => resolve("deleted");
            request.onerror = () => resolve("failed");
        } catch {
            resolve("failed");
        }
        void limit.reached.then(resolve);
    }).finally(limit.clear);
}

async function removeCopy(): Promise<"deleted" | "kept"> {
    const outcome = await transact("readwrite", clearAll);
    if (outcome === "absent") return "deleted";
    return outcome === "done" && (await deleteDatabase()) === "deleted"
        ? "deleted"
        : "kept";
}

export async function deleteCopy() {
    return (await deleteDatabase()) === "deleted";
}

async function removeOutstanding(removal: Removal) {
    if (removal.outstanding() === null) return;
    await transact("readwrite", (transaction) =>
        withHolder(transaction, (holder, store) => {
            const outstanding = removal.outstanding();
            if (outstanding === null) return;
            pause(transaction, holder, store);
            transaction.addEventListener("complete", () =>
                removal.acknowledge(outstanding),
            );
        }),
    );
}

export async function removeFigures(committed: () => void) {
    const outcome = await transact("readwrite", (transaction) => {
        transaction.addEventListener("complete", committed);
        withHolder(transaction, (holder, store) =>
            pause(transaction, holder, store),
        );
    });
    return outcome === "done" || outcome === "absent";
}

export async function start(removal: Removal): Promise<"deleted" | "kept"> {
    if (!SAVED_FIGURES) return removeCopy();
    await removeOutstanding(removal);
    return "kept";
}

export async function adopt(
    observation: Observation,
    removal: Removal,
): Promise<Handoff> {
    const read: { handoff: Handoff } = { handoff: "verified" };
    const outcome = await transact("readwrite", (transaction) =>
        withHolder(transaction, (holder, store) => {
            if (!observation.present()) {
                read.handoff = "superseded";
                return;
            }
            const outstanding = removal.outstanding();
            if (outstanding !== null)
                transaction.addEventListener("complete", () =>
                    removal.acknowledge(outstanding),
                );
            if (!isHolder(holder) || holder.user !== observation.user) {
                read.handoff = "cleared";
                clearAll(transaction);
                return;
            }
            if (
                outstanding !== null ||
                !current(holder.confirmedAt, Date.now())
            )
                clearFigures(transaction);
            store.put(
                {
                    ...holder,
                    ...(outstanding !== null && { active: false }),
                    confirmedAt: observation.adoptedAt,
                    language: observation.language,
                    ...(holder.session !== observation.session && {
                        session: observation.session,
                        active: true,
                    }),
                },
                holderKey,
            );
        }),
    );
    if (outcome === "absent") return "verified";
    return outcome === "done" ? read.handoff : "unverified";
}

export async function copyState(): Promise<CopyState> {
    const read: { holder?: unknown } = {};
    const outcome = await transact("readonly", (transaction) =>
        withHolder(transaction, (holder) => {
            read.holder = holder;
        }),
    );
    if (outcome === "absent") return "off";
    if (outcome !== "done") return "unavailable";
    if (!isHolder(read.holder)) return "off";
    return read.holder.active ? "on" : "paused";
}
