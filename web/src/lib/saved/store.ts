import type { Deadline } from "./deadline";

const SAVED_FIGURES = true;

const databaseName = "pyracast-saved";
const format = 1;
const holderKey = "holder";
const storeNames = ["holder", "plants", "days"];
const dayMs = 86_400_000;
const keptForMs = 90 * dayMs;
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
type SignedOut = { user: number; session: string };
type Handoff = "verified" | "cleared" | "superseded" | "blocked" | "unverified";
type Outcome =
    "done" | "absent" | "blocked" | "unavailable" | "failed" | "pending";

function openExisting(
    reached: Promise<void>,
): Promise<IDBDatabase | "absent" | "blocked" | "unavailable"> {
    return new Promise((resolve) => {
        let request: IDBOpenDBRequest;
        try {
            request = indexedDB.open(databaseName, format);
        } catch {
            resolve("blocked");
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

function abortUnlessCommitting(transaction: IDBTransaction) {
    try {
        transaction.abort();
    } catch {
        return;
    }
}

function run(
    database: IDBDatabase,
    mode: IDBTransactionMode,
    work: (transaction: IDBTransaction) => void,
    abortAt?: Deadline,
): Promise<Outcome> {
    return new Promise<Outcome>((resolve) => {
        try {
            const transaction = database.transaction(storeNames, mode);
            transaction.oncomplete = () => resolve("done");
            transaction.onabort = () => resolve("failed");
            void abortAt?.reached.then(() =>
                abortUnlessCommitting(transaction),
            );
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

// At the deadline an adoption is aborted unless its commit has begun; any other transaction lands.
async function transact(
    mode: IDBTransactionMode,
    work: (transaction: IDBTransaction) => void,
    limit: Deadline,
    abortLate = false,
): Promise<Outcome> {
    if (limit.expired()) return "pending";
    const database = await openExisting(limit.reached);
    if (typeof database === "string") return database;
    if (limit.expired()) {
        database.close();
        return "pending";
    }
    return Promise.race([
        run(database, mode, work, abortLate ? limit : undefined),
        limit.reached.then(() => "pending" as const),
    ]);
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
        session !== "" &&
        typeof active === "boolean" &&
        isInstant(confirmedAt) &&
        typeof removal === "string" &&
        /^[0-9a-f]{32}$/i.test(removal) &&
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
    else store.delete(holderKey);
}

function leavingOf(mark: string): SignedOut | undefined {
    try {
        const { user, session } = JSON.parse(mark) as Record<string, unknown>;
        return typeof user === "number" && typeof session === "string"
            ? { user, session }
            : undefined;
    } catch {
        return undefined;
    }
}

// A removal whose departing session cannot be read applies to any holder.
function meantFor(leaving: SignedOut | undefined, holder: unknown) {
    return (
        !isHolder(holder) ||
        leaving === undefined ||
        (holder.user === leaving.user && holder.session === leaving.session)
    );
}

function current(confirmedAt: number, now: number) {
    return now - confirmedAt <= keptForMs && confirmedAt - now <= dayMs;
}

function deleteDatabase(
    limit: Deadline,
    deleted = () => {},
): Promise<"deleted" | "failed" | "pending"> {
    if (limit.expired()) return Promise.resolve("pending");
    return new Promise((resolve) => {
        try {
            const request = indexedDB.deleteDatabase(databaseName);
            request.onsuccess = () => {
                deleted();
                resolve("deleted");
            };
            request.onerror = () => resolve("failed");
        } catch {
            resolve("failed");
        }
        void limit.reached.then(() => resolve("pending"));
    });
}

async function removeCopy(limit: Deadline): Promise<"deleted" | "kept"> {
    const outcome = await transact("readwrite", clearAll, limit);
    if (outcome === "absent") return "deleted";
    return outcome === "done" && (await deleteDatabase(limit)) === "deleted"
        ? "deleted"
        : "kept";
}

// A deletion still waiting at the deadline goes on; `deleted` runs whenever it succeeds.
export async function deleteCopy(limit: Deadline, deleted: () => void) {
    await deleteDatabase(limit, deleted);
}

async function removeOutstanding(removal: Removal, limit: Deadline) {
    if (removal.outstanding() === null) return;
    await transact(
        "readwrite",
        (transaction) =>
            withHolder(transaction, (holder, store) => {
                const outstanding = removal.outstanding();
                if (outstanding === null) return;
                if (meantFor(leavingOf(outstanding), holder))
                    pause(transaction, holder, store);
                transaction.addEventListener("complete", () =>
                    removal.acknowledge(outstanding),
                );
            }),
        limit,
    );
}

export async function removeFigures(
    signedOut: SignedOut,
    committed: () => void,
    limit: Deadline,
) {
    const outcome = await transact(
        "readwrite",
        (transaction) => {
            transaction.addEventListener("complete", committed);
            withHolder(transaction, (holder, store) => {
                if (meantFor(signedOut, holder))
                    pause(transaction, holder, store);
            });
        },
        limit,
    );
    return outcome === "done" || outcome === "absent";
}

export async function start(
    removal: Removal,
    limit: Deadline,
): Promise<"deleted" | "kept"> {
    if (!SAVED_FIGURES) return removeCopy(limit);
    await removeOutstanding(removal, limit);
    return "kept";
}

export async function adopt(
    observation: Observation,
    removal: Removal,
    limit: Deadline,
): Promise<Handoff> {
    const read: { handoff: Handoff } = { handoff: "verified" };
    const outcome = await transact(
        "readwrite",
        (transaction) =>
            withHolder(transaction, (holder, store) => {
                if (limit.expired()) {
                    read.handoff = "unverified";
                    return;
                }
                if (!observation.present()) {
                    read.handoff = "superseded";
                    return;
                }
                const outstanding = removal.outstanding();
                if (outstanding !== null)
                    transaction.addEventListener("complete", () => {
                        if (!limit.expired()) removal.acknowledge(outstanding);
                    });
                if (!isHolder(holder) || holder.user !== observation.user) {
                    read.handoff = "cleared";
                    clearAll(transaction);
                    return;
                }
                const removed =
                    outstanding !== null &&
                    meantFor(leavingOf(outstanding), holder);
                if (
                    removed ||
                    !current(holder.confirmedAt, observation.adoptedAt)
                )
                    clearFigures(transaction);
                store.put(
                    {
                        ...holder,
                        ...(removed && { active: false }),
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
        limit,
        true,
    );
    if (outcome === "absent") return "verified";
    if (outcome === "blocked") return "blocked";
    return outcome === "done" ? read.handoff : "unverified";
}

export async function copyState(limit: Deadline): Promise<CopyState> {
    const read: { holder?: unknown } = {};
    const outcome = await transact(
        "readonly",
        (transaction) =>
            withHolder(transaction, (holder) => {
                read.holder = holder;
            }),
        limit,
    );
    if (outcome === "absent") return "off";
    if (outcome !== "done") return "unavailable";
    if (!isHolder(read.holder)) return "off";
    return read.holder.active ? "on" : "paused";
}
