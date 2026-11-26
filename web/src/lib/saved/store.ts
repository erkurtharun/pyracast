import type { Deadline } from "./deadline";
import { SAVED_FIGURES } from "./feature";

const databaseName = "pyracast-saved";
const format = 1;
const holderKey = "holder";
const storeNames = ["holder", "plants", "days"];
const dayMs = 86_400_000;
const keptForMs = 90 * dayMs;
const latestInstant = 8_640_000_000_000_000;
const plantBound = 8;
const dayBounds = { forecast: 22, production: 30 } as const;
const rowBound = 52;

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
export type Owner = { user: number; session: string };
export type CopyView = { state: CopyState; plants: number; holder?: Owner };
export type Observation = Owner & {
    language: Holder["language"];
    adoptedAt: number;
    present: () => boolean;
};
// A departure without a user and a session tag applies to any holder.
export type Departure = { id: string; user?: number; session?: string };
export type Removal = {
    outstanding: () => Departure[];
    acknowledge: (ids: string[]) => void;
};
export type PlantKey = { tenant: number; site: number; plant: number };
export type HeldPlant = PlantKey & {
    name: string;
    workspace: string;
    openedAt: number | null;
    attemptedAt: number | null;
    refreshedAt: number | null;
};
export type Held = { retryAt: number | null; plants: HeldPlant[] };
export type Listed = {
    workspaces: { tenant: number; name: string }[];
    onScreen?: {
        tenant: number;
        plants: (PlantKey & { name: string })[];
    };
};
export type DaysAnswer = {
    name: string;
    forecast: unknown[];
    production: unknown[];
};
type Handoff = "verified" | "cleared" | "superseded" | "blocked" | "unverified";
type Outcome =
    "done" | "absent" | "blocked" | "unavailable" | "failed" | "pending";

function openExisting(
    reached: Promise<void>,
    creating = false,
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
            if (creating) {
                create(request.result);
                return;
            }
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

function create(database: IDBDatabase) {
    database.createObjectStore("holder");
    database.createObjectStore("plants", {
        keyPath: ["tenant", "site", "plant"],
    });
    database.createObjectStore("days", {
        keyPath: ["tenant", "site", "plant", "kind", "date"],
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
    { abortLate = false, creating = false } = {},
): Promise<Outcome> {
    if (limit.expired()) return "pending";
    const database = await openExisting(limit.reached, creating);
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

function meantFor(leaving: Omit<Departure, "id">, holder: unknown) {
    return (
        !isHolder(holder) ||
        !isIdentified(leaving) ||
        (holder.user === leaving.user && holder.session === leaving.session)
    );
}

function isIdentified(leaving: Omit<Departure, "id">): leaving is Owner {
    return (
        typeof leaving.user === "number" &&
        Number.isInteger(leaving.user) &&
        leaving.user > 0 &&
        typeof leaving.session === "string" &&
        leaving.session !== ""
    );
}

function owns(holder: unknown, owner: Owner): holder is Holder {
    return (
        isHolder(holder) &&
        holder.active &&
        holder.user === owner.user &&
        holder.session === owner.session
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

// The figures are unreadable once the clear commits; a deletion still waiting at the deadline goes on and `deleted` runs whenever it succeeds.
export async function removeCopy(
    limit: Deadline,
    deleted: () => void,
    cleared = () => {},
) {
    const outcome = await transact(
        "readwrite",
        (transaction) => {
            transaction.addEventListener("complete", cleared);
            clearAll(transaction);
        },
        limit,
    );
    if (outcome === "absent") deleted();
    else if (outcome === "done") await deleteDatabase(limit, deleted);
}

export async function deleteCopy(limit: Deadline, deleted: () => void) {
    await deleteDatabase(limit, deleted);
}

function ids(departures: Departure[]) {
    return departures.map(({ id }) => id);
}

async function removeOutstanding(removal: Removal, limit: Deadline) {
    const observed = removal.outstanding();
    if (observed.length === 0) return;
    const outcome = await transact(
        "readwrite",
        (transaction) =>
            withHolder(transaction, (holder, store) => {
                const outstanding = removal.outstanding();
                if (outstanding.some((leaving) => meantFor(leaving, holder)))
                    pause(transaction, holder, store);
                transaction.addEventListener("complete", () =>
                    removal.acknowledge(ids(outstanding)),
                );
            }),
        limit,
    );
    if (outcome === "absent") removal.acknowledge(ids(observed));
}

export async function removeFigures(
    signedOut: Omit<Departure, "id">,
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
    deleted: () => void,
) {
    if (SAVED_FIGURES) await removeOutstanding(removal, limit);
    else await removeCopy(limit, deleted);
}

export async function adopt(
    observation: Observation,
    removal: Removal,
    limit: Deadline,
): Promise<Handoff> {
    const read: { handoff: Handoff } = { handoff: "verified" };
    const observed = removal.outstanding();
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
                transaction.addEventListener("complete", () => {
                    if (!limit.expired()) removal.acknowledge(ids(outstanding));
                });
                if (!isHolder(holder) || holder.user !== observation.user) {
                    read.handoff = "cleared";
                    clearAll(transaction);
                    return;
                }
                const removed = outstanding.some((leaving) =>
                    meantFor(leaving, holder),
                );
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
        { abortLate: true },
    );
    if (outcome === "absent") {
        if (!limit.expired()) removal.acknowledge(ids(observed));
        return "verified";
    }
    if (outcome === "blocked") return "blocked";
    return outcome === "done" ? read.handoff : "unverified";
}

export async function copyState(limit: Deadline): Promise<CopyView> {
    const read: { holder?: unknown; plants: number } = { plants: 0 };
    const outcome = await transact(
        "readonly",
        (transaction) => {
            withHolder(transaction, (holder) => {
                read.holder = holder;
            });
            transaction.objectStore("plants").count().onsuccess = (event) => {
                read.plants = (event.target as IDBRequest<number>).result;
            };
        },
        limit,
    );
    if (outcome === "absent") return { state: "off", plants: 0 };
    if (outcome !== "done") return { state: "unavailable", plants: 0 };
    const { holder, plants } = read;
    if (!isHolder(holder)) return { state: "off", plants: 0 };
    return {
        state: holder.active ? "on" : "paused",
        plants,
        holder: { user: holder.user, session: holder.session },
    };
}

// Without a copy the open is aborted before it creates one, which still shows that the browser answers.
export async function available(limit: Deadline) {
    if (!SAVED_FIGURES) return false;
    const outcome = await transact("readonly", () => {}, limit);
    return outcome === "absent" || outcome === "done";
}

export async function turnOn(
    owner: Pick<Holder, "user" | "language" | "session" | "removal">,
    present: () => boolean,
    committed: () => void,
    limit: Deadline,
) {
    const read = { done: false };
    const outcome = await transact(
        "readwrite",
        (transaction) =>
            withHolder(transaction, (holder, store) => {
                if (!present()) return;
                transaction.addEventListener("complete", committed);
                if (
                    !isHolder(holder) ||
                    holder.user !== owner.user ||
                    holder.session !== owner.session
                )
                    clearFigures(transaction);
                store.put(
                    {
                        ...owner,
                        active: true,
                        confirmedAt: Date.now(),
                        retryAt: null,
                    } satisfies Holder,
                    holderKey,
                );
                read.done = true;
            }),
        limit,
        { creating: true },
    );
    return written(outcome, read);
}

function isKeyPart(value: unknown): value is number {
    return typeof value === "number" && Number.isInteger(value) && value > 0;
}

function isPlant(value: unknown): value is HeldPlant {
    if (typeof value !== "object" || value === null) return false;
    const record = value as Record<string, unknown>;
    const time = (field: unknown) => field === null || isInstant(field);
    return (
        isKeyPart(record.tenant) &&
        isKeyPart(record.site) &&
        isKeyPart(record.plant) &&
        typeof record.name === "string" &&
        typeof record.workspace === "string" &&
        time(record.openedAt) &&
        time(record.attemptedAt) &&
        time(record.refreshedAt)
    );
}

function keyOf({ tenant, site, plant }: PlantKey) {
    return [tenant, site, plant];
}

function sameKey(one: PlantKey, other: PlantKey) {
    return (
        one.tenant === other.tenant &&
        one.site === other.site &&
        one.plant === other.plant
    );
}

function daysOf(
    transaction: IDBTransaction,
    { tenant, site, plant }: PlantKey,
) {
    return {
        store: transaction.objectStore("days"),
        range: IDBKeyRange.bound(
            [tenant, site, plant],
            [tenant, site, plant, []],
        ),
    };
}

function dropPlant(transaction: IDBTransaction, key: PlantKey) {
    transaction.objectStore("plants").delete(keyOf(key));
    const { store, range } = daysOf(transaction, key);
    store.delete(range);
}

function withPlants(
    transaction: IDBTransaction,
    owner: Owner,
    work: (plants: HeldPlant[], store: IDBObjectStore, holder: Holder) => void,
) {
    withHolder(transaction, (holder) => {
        if (!owns(holder, owner)) return;
        const store = transaction.objectStore("plants");
        const request = store.getAll();
        request.onsuccess = () => {
            try {
                work(
                    (request.result as unknown[]).filter(isPlant),
                    store,
                    holder,
                );
            } catch {
                transaction.abort();
            }
        };
    });
}

function written(outcome: Outcome, read: { done: boolean }) {
    return outcome === "done" && read.done;
}

export async function held(owner: Owner, limit: Deadline) {
    const read: { held?: Held } = {};
    await transact(
        "readonly",
        (transaction) =>
            withPlants(transaction, owner, (plants, _, holder) => {
                read.held = { retryAt: holder.retryAt, plants };
            }),
        limit,
    );
    return read.held;
}

function evicted(plants: HeldPlant[]) {
    return (
        plants.find((plant) => plant.openedAt === null) ??
        [...plants].sort((a, b) => (a.openedAt ?? 0) - (b.openedAt ?? 0))[0]
    );
}

export async function settle(owner: Owner, listed: Listed, limit: Deadline) {
    const read = { done: false };
    const outcome = await transact(
        "readwrite",
        (transaction) =>
            withPlants(transaction, owner, (plants, store) => {
                const names = new Map(
                    listed.workspaces.map(({ tenant, name }) => [tenant, name]),
                );
                const screen = listed.onScreen;
                const listedPlant = (plant: PlantKey) =>
                    screen?.plants.find((row) => sameKey(row, plant));
                let kept: HeldPlant[] = [];
                for (const plant of plants) {
                    const workspace = names.get(plant.tenant);
                    const onScreen = screen?.tenant === plant.tenant;
                    const row = onScreen ? listedPlant(plant) : undefined;
                    if (workspace === undefined || (onScreen && !row)) {
                        dropPlant(transaction, plant);
                        continue;
                    }
                    const renewed = {
                        ...plant,
                        workspace,
                        name: row?.name ?? plant.name,
                    };
                    store.put(renewed);
                    kept = [...kept, renewed];
                }
                const workspace = screen && names.get(screen.tenant);
                for (const row of screen?.plants ?? []) {
                    if (workspace === undefined || kept.length >= plantBound)
                        break;
                    if (kept.some((plant) => sameKey(plant, row))) continue;
                    const added: HeldPlant = {
                        ...row,
                        workspace,
                        openedAt: null,
                        attemptedAt: null,
                        refreshedAt: null,
                    };
                    store.put(added);
                    kept = [...kept, added];
                }
                read.done = true;
            }),
        limit,
    );
    return written(outcome, read);
}

export async function openPlant(
    owner: Owner,
    opened: PlantKey & { name: string; workspace: string },
    at: number,
    limit: Deadline,
) {
    const read = { done: false };
    const outcome = await transact(
        "readwrite",
        (transaction) =>
            withPlants(transaction, owner, (plants, store) => {
                const known = plants.find((plant) => sameKey(plant, opened));
                const others = plants.filter((plant) => plant !== known);
                store.put({
                    attemptedAt: null,
                    refreshedAt: null,
                    ...known,
                    ...opened,
                    openedAt: at,
                });
                const victim = others.length >= plantBound && evicted(others);
                if (victim) dropPlant(transaction, victim);
                read.done = true;
            }),
        limit,
    );
    return written(outcome, read);
}

export async function removeWorkspace(
    owner: Owner,
    tenant: number,
    limit: Deadline,
) {
    const read = { done: false };
    const outcome = await transact(
        "readwrite",
        (transaction) =>
            withPlants(transaction, owner, (plants) => {
                for (const plant of plants)
                    if (plant.tenant === tenant) dropPlant(transaction, plant);
                read.done = true;
            }),
        limit,
    );
    return written(outcome, read);
}

export async function removePlant(
    owner: Owner,
    key: PlantKey,
    limit: Deadline,
) {
    const read = { done: false };
    const outcome = await transact(
        "readwrite",
        (transaction) =>
            withPlants(transaction, owner, () => {
                dropPlant(transaction, key);
                read.done = true;
            }),
        limit,
    );
    return written(outcome, read);
}

export async function markAttempt(
    owner: Owner,
    key: PlantKey,
    at: number,
    limit: Deadline,
) {
    const read = { done: false };
    const outcome = await transact(
        "readwrite",
        (transaction) =>
            withPlants(transaction, owner, (plants, store) => {
                const plant = plants.find((row) => sameKey(row, key));
                if (!plant) return;
                store.put({ ...plant, attemptedAt: at });
                read.done = true;
            }),
        limit,
    );
    return written(outcome, read);
}

function boundedDays(kind: keyof typeof dayBounds, days: unknown[]) {
    return days
        .filter(
            (day) =>
                typeof day === "object" &&
                day !== null &&
                Array.isArray((day as { rows?: unknown }).rows) &&
                (day as { rows: unknown[] }).rows.length <= rowBound,
        )
        .slice(0, dayBounds[kind]) as Record<string, unknown>[];
}

export async function writeDays(
    owner: Owner,
    key: PlantKey,
    attemptedAt: number,
    answer: DaysAnswer,
    limit: Deadline,
) {
    const read = { done: false };
    const outcome = await transact(
        "readwrite",
        (transaction) =>
            withPlants(transaction, owner, (plants, store) => {
                const plant = plants.find((row) => sameKey(row, key));
                if (plant?.attemptedAt !== attemptedAt) return;
                const days = daysOf(transaction, key);
                days.store.delete(days.range);
                for (const kind of ["forecast", "production"] as const)
                    for (const day of boundedDays(kind, answer[kind]))
                        days.store.put({ ...day, ...key, kind });
                store.put({
                    ...plant,
                    name: answer.name,
                    refreshedAt: Date.now(),
                });
                read.done = true;
            }),
        limit,
    );
    return written(outcome, read);
}

export async function setRetryAt(owner: Owner, at: number, limit: Deadline) {
    const read = { done: false };
    const outcome = await transact(
        "readwrite",
        (transaction) =>
            withHolder(transaction, (holder, store) => {
                if (!owns(holder, owner)) return;
                store.put({ ...holder, retryAt: at }, holderKey);
                read.done = true;
            }),
        limit,
    );
    return written(outcome, read);
}
