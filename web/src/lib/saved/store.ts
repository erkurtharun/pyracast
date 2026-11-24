const SAVED_FIGURES = true;

const databaseName = "pyracast-saved";
const format = 1;
const holderKey = "holder";
const storeNames = ["holder", "plants", "days"];
const dayMs = 86_400_000;
const keptForMs = 90 * dayMs;

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
type Outcome = "done" | "absent" | "unavailable" | "failed";

function openExisting(): Promise<IDBDatabase | "absent" | "unavailable"> {
    return new Promise((resolve) => {
        let request: IDBOpenDBRequest;
        try {
            request = indexedDB.open(databaseName, format);
        } catch {
            resolve("unavailable");
            return;
        }
        let absent = false;
        request.onupgradeneeded = () => {
            absent = true;
            request.transaction?.abort();
        };
        request.onsuccess = () => {
            const database = request.result;
            database.onversionchange = () => database.close();
            resolve(database);
        };
        request.onerror = () => resolve(absent ? "absent" : "unavailable");
    });
}

async function transact(
    mode: IDBTransactionMode,
    work: (transaction: IDBTransaction) => void,
): Promise<Outcome> {
    const database = await openExisting();
    if (typeof database === "string") return database;
    try {
        return await new Promise<Outcome>((resolve) => {
            const transaction = database.transaction(storeNames, mode);
            transaction.oncomplete = () => resolve("done");
            transaction.onabort = () => resolve("failed");
            try {
                work(transaction);
            } catch {
                transaction.abort();
            }
        });
    } catch {
        return "failed";
    } finally {
        database.close();
    }
}

function withHolder(
    transaction: IDBTransaction,
    read: (holder: Holder | undefined, store: IDBObjectStore) => void,
) {
    const store = transaction.objectStore("holder");
    const request = store.get(holderKey);
    request.onsuccess = () => read(request.result as Holder | undefined, store);
}

function clearFigures(transaction: IDBTransaction) {
    transaction.objectStore("plants").clear();
    transaction.objectStore("days").clear();
}

function clearAll(transaction: IDBTransaction) {
    transaction.objectStore("holder").clear();
    clearFigures(transaction);
}

function current(confirmedAt: number, now: number) {
    return now - confirmedAt <= keptForMs && confirmedAt - now <= dayMs;
}

function deleteDatabase(): Promise<boolean> {
    return new Promise((resolve) => {
        try {
            const request = indexedDB.deleteDatabase(databaseName);
            request.onsuccess = () => resolve(true);
            request.onerror = () => resolve(false);
        } catch {
            resolve(false);
        }
    });
}

async function removeCopy(): Promise<boolean> {
    const outcome = await transact("readwrite", clearAll);
    if (outcome === "unavailable") return false;
    return outcome === "absent" || deleteDatabase();
}

export async function removeFigures(): Promise<boolean> {
    const outcome = await transact("readwrite", (transaction) => {
        clearFigures(transaction);
        withHolder(transaction, (holder, store) => {
            if (holder) store.put({ ...holder, active: false }, holderKey);
        });
    });
    return outcome === "done" || outcome === "absent";
}

export async function start(marked: boolean) {
    if (!SAVED_FIGURES) return (await removeCopy()) ? "deleted" : "kept";
    return marked && (await removeFigures()) ? "removed" : "kept";
}

export async function adopt(
    user: number,
    session: string,
    language: Holder["language"],
) {
    const read = { foreign: false };
    const outcome = await transact("readwrite", (transaction) =>
        withHolder(transaction, (holder, store) => {
            if (!holder) return;
            if (holder.user !== user) {
                read.foreign = true;
                clearAll(transaction);
                return;
            }
            const now = Date.now();
            if (!current(holder.confirmedAt, now)) clearFigures(transaction);
            store.put(
                {
                    ...holder,
                    confirmedAt: now,
                    language,
                    ...(holder.session !== session && {
                        session,
                        active: true,
                    }),
                },
                holderKey,
            );
        }),
    );
    return outcome === "done" && read.foreign && (await deleteDatabase())
        ? "deleted"
        : "kept";
}

export async function copyState(): Promise<CopyState> {
    const read: { holder?: Holder } = {};
    const outcome = await transact("readonly", (transaction) =>
        withHolder(transaction, (holder) => {
            read.holder = holder;
        }),
    );
    if (outcome === "absent") return "off";
    if (outcome !== "done") return "unavailable";
    if (!read.holder) return "off";
    return read.holder.active ? "on" : "paused";
}
