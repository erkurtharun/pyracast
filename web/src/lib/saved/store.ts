import type { Deadline } from "./deadline";

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
// `off`: the owner's person turned keeping off on this device.
export type CopyView = { state: CopyState; plants: number; off: boolean };
type Observation = Owner & {
    language: Holder["language"];
    adoptedAt: number;
    present: () => boolean;
};
export type PlantKey = { tenant: number; site: number; plant: number };
export type HeldPlant = PlantKey & {
    name: string;
    workspace: string;
    openedAt: number | null;
    attemptedAt: number | null;
    refreshedAt: number | null;
};
type Held = { retryAt: number | null; plants: HeldPlant[] };
type Listed = {
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
type Handoff = "verified" | "superseded" | "blocked" | "unverified";
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

// With `abortLate` the deadline aborts the transaction unless its commit has begun, and the answer is its own end; any other transaction lands.
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
    if (abortLate) return run(database, mode, work, limit);
    return Promise.race([
        run(database, mode, work),
        limit.reached.then(() => "pending" as const),
    ]);
}

function whenRead(
    transaction: IDBTransaction,
    request: IDBRequest,
    then: () => void,
) {
    request.onsuccess = () => {
        try {
            then();
        } catch {
            transaction.abort();
        }
    };
}

function withHolder(
    transaction: IDBTransaction,
    read: (holder: unknown, store: IDBObjectStore) => void,
) {
    const store = transaction.objectStore("holder");
    const holder = store.get(holderKey);
    whenRead(transaction, holder, () => read(holder.result, store));
}

// The holder store also keeps the device's marks: `["off", user]` and `["ended", at, session]`.
function withMarks(
    transaction: IDBTransaction,
    read: (holder: unknown, store: IDBObjectStore, marks: unknown[]) => void,
) {
    const store = transaction.objectStore("holder");
    const holder = store.get(holderKey);
    const marks = store.getAllKeys();
    whenRead(transaction, marks, () =>
        read(holder.result, store, marks.result),
    );
}

function isOff(marks: unknown[], user: number) {
    return marks.some(
        (mark) => Array.isArray(mark) && mark[0] === "off" && mark[1] === user,
    );
}

// The one rule of the copy: no figures are kept, refreshed or shown for a person who turned keeping off here or for a session that is ending or ended.
function refuses(marks: unknown[], { user, session }: Owner) {
    return (
        isOff(marks, user) ||
        marks.some(
            (mark) =>
                Array.isArray(mark) &&
                mark[0] === "ended" &&
                mark[2] === session,
        )
    );
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

function dropHolder(transaction: IDBTransaction, store: IDBObjectStore) {
    store.delete(holderKey);
    clearFigures(transaction);
}

function heldFor(holder: unknown, owner: Owner): holder is Holder {
    return (
        isHolder(holder) &&
        holder.user === owner.user &&
        holder.session === owner.session
    );
}

function owns(
    holder: unknown,
    owner: Owner,
    marks: unknown[],
): holder is Holder {
    return heldFor(holder, owner) && holder.active && !refuses(marks, owner);
}

function current(confirmedAt: number, now: number) {
    return now - confirmedAt <= keptForMs && confirmedAt - now <= dayMs;
}

function deleteDatabase(limit: Deadline) {
    return new Promise<void>((resolve) => {
        try {
            const request = indexedDB.deleteDatabase(databaseName);
            request.onsuccess = request.onerror = () => resolve();
        } catch {
            resolve();
        }
        void limit.reached.then(() => resolve());
    });
}

// A withdrawn build removes the marks with the figures; a deletion still waiting at the deadline goes on.
export async function removeCopy(limit: Deadline) {
    const outcome = await transact(
        "readwrite",
        (transaction) => {
            transaction.objectStore("holder").clear();
            clearFigures(transaction);
        },
        limit,
    );
    if (outcome === "done") await deleteDatabase(limit);
}

// The mark and the removal it implies commit together, so no figures outlive a record that refuses them.
export async function endSession(leaving: Owner, limit: Deadline) {
    const read = { done: false };
    const outcome = await transact(
        "readwrite",
        (transaction) =>
            withHolder(transaction, (holder, store) => {
                const now = Date.now();
                store.delete(
                    IDBKeyRange.bound(["ended"], ["ended", now - keptForMs]),
                );
                store.put(true, ["ended", now, leaving.session]);
                if (heldFor(holder, leaving)) {
                    clearFigures(transaction);
                    store.put({ ...holder, active: false }, holderKey);
                }
                read.done = true;
            }),
        limit,
        { creating: true, abortLate: true },
    );
    return written(outcome, read);
}

export async function setKept(user: number, kept: boolean, limit: Deadline) {
    const read = { done: false };
    const outcome = await transact(
        "readwrite",
        (transaction) =>
            withHolder(transaction, (holder, store) => {
                if (kept) store.delete(["off", user]);
                else {
                    store.put(true, ["off", user]);
                    if (isHolder(holder) && holder.user === user)
                        dropHolder(transaction, store);
                }
                read.done = true;
            }),
        limit,
        { creating: !kept, abortLate: true },
    );
    return written(outcome, read) || (kept && outcome === "absent");
}

export async function adopt(
    observation: Observation,
    limit: Deadline,
): Promise<Handoff> {
    const read: { handoff: Handoff } = { handoff: "verified" };
    const outcome = await transact(
        "readwrite",
        (transaction) =>
            withMarks(transaction, (holder, store, marks) => {
                if (limit.expired()) {
                    read.handoff = "unverified";
                    return;
                }
                if (!observation.present()) {
                    read.handoff = "superseded";
                    return;
                }
                if (!isHolder(holder) || holder.user !== observation.user) {
                    dropHolder(transaction, store);
                    return;
                }
                if (!current(holder.confirmedAt, observation.adoptedAt))
                    clearFigures(transaction);
                store.put(
                    {
                        ...holder,
                        session: observation.session,
                        active:
                            !refuses(marks, observation) &&
                            (holder.active ||
                                holder.session !== observation.session),
                        confirmedAt: observation.adoptedAt,
                        language: observation.language,
                    },
                    holderKey,
                );
            }),
        limit,
        { abortLate: true },
    );
    if (outcome === "absent") return "verified";
    if (outcome === "blocked") return "blocked";
    return outcome === "done" ? read.handoff : "unverified";
}

export async function copyState(
    owner: Owner,
    limit: Deadline,
): Promise<CopyView> {
    const read: { holder?: unknown; marks: unknown[]; plants: number } = {
        marks: [],
        plants: 0,
    };
    const outcome = await transact(
        "readonly",
        (transaction) => {
            withMarks(transaction, (holder, _, marks) => {
                read.holder = holder;
                read.marks = marks;
            });
            transaction.objectStore("plants").count().onsuccess = (event) => {
                read.plants = (event.target as IDBRequest<number>).result;
            };
        },
        limit,
    );
    if (outcome === "absent") return { state: "off", plants: 0, off: false };
    if (outcome !== "done")
        return { state: "unavailable", plants: 0, off: false };
    const { holder, marks, plants } = read;
    const off = isOff(marks, owner.user);
    if (!isHolder(holder)) return { state: "off", plants: 0, off };
    return { state: owns(holder, owner, marks) ? "on" : "paused", plants, off };
}

// Answers false only when this device refused to create or write the copy; a declined or unfinished turn-on is no refusal.
export async function turnOn(
    owner: Pick<Holder, "user" | "language" | "session" | "removal">,
    present: () => boolean,
    committed: () => void,
    limit: Deadline,
) {
    const outcome = await transact(
        "readwrite",
        (transaction) =>
            withMarks(transaction, (holder, store, marks) => {
                if (!present() || refuses(marks, owner)) return;
                transaction.addEventListener("complete", committed);
                if (!heldFor(holder, owner)) clearFigures(transaction);
                store.put(
                    {
                        ...owner,
                        active: true,
                        confirmedAt: Date.now(),
                        retryAt: null,
                    } satisfies Holder,
                    holderKey,
                );
            }),
        limit,
        { creating: true },
    );
    const refused =
        outcome === "failed" ||
        outcome === "unavailable" ||
        outcome === "blocked";
    return !refused || limit.expired();
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
    withMarks(transaction, (holder, _, marks) => {
        if (!owns(holder, owner, marks)) return;
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
            withMarks(transaction, (holder, store, marks) => {
                if (!owns(holder, owner, marks)) return;
                store.put({ ...holder, retryAt: at }, holderKey);
                read.done = true;
            }),
        limit,
    );
    return written(outcome, read);
}
