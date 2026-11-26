import { readFileSync } from "node:fs";
import path from "node:path";
import ts from "typescript";
import type { Page } from "@playwright/test";
import { instant } from "./test";

export const dayMs = 86_400_000;
const blank = "/saved-copy-blank/";

export type Holder = {
    user: number;
    language: "en" | "tr";
    session: string;
    active: boolean;
    confirmedAt: number;
    removal: string;
    retryAt: number | null;
};

export function holderOf(fields: Partial<Holder> = {}): Holder {
    return {
        user: 1,
        language: "en",
        session: "tag-a",
        active: true,
        confirmedAt: instant.getTime() - dayMs,
        removal: "0123456789abcdef0123456789abcdef",
        retryAt: null,
        ...fields,
    };
}

const key = { tenant: 1, site: 7, plant: 7 };

const heldPlant = {
    ...key,
    name: "East array",
    workspace: "Sunfield Energy",
    openedAt: instant.getTime() - dayMs,
    attemptedAt: instant.getTime() - dayMs,
    refreshedAt: instant.getTime() - dayMs,
};

export const heldDays = [
    {
        ...key,
        kind: "forecast",
        date: "2026-11-10",
        timezone: "Europe/Istanbul",
        source_kind: "forecast",
        freshness: "fresh",
        stale_reason: null,
        unavailable_reason: null,
        collected_at: "2026-11-10T06:00:00+03:00",
        total_mwh: 1.5,
        partial: false,
        affected: null,
        rows: [
            {
                start: "2026-11-10T12:00:00+03:00",
                end: "2026-11-10T13:00:00+03:00",
                mwh: 1.5,
                affected_mwh: null,
                sources: null,
            },
        ],
    },
    {
        ...key,
        kind: "production",
        date: "2026-11-09",
        timezone: "Europe/Istanbul",
        total_mwh: 1.2,
        complete: false,
        rows: [
            {
                start: "2026-11-09T12:00:00+03:00",
                end: "2026-11-09T13:00:00+03:00",
                mwh: 1.2,
                quality: "valid",
                source: "meter",
                provisional: false,
            },
        ],
    },
];

export type RemovalMark = { removal: string; user: number; session: string };

export type Copy = {
    holder?: Holder;
    plants?: unknown[];
    days?: unknown[];
    version?: number;
    flagged?: boolean;
    marks?: RemovalMark[];
};

const markPrefix = "pyracast.savedRemoval.";

export function writeMarks(page: Page, marks: RemovalMark[]) {
    return page.evaluate(
        ({ marks, prefix }) => {
            for (const mark of marks)
                window.localStorage.setItem(
                    prefix + mark.removal,
                    JSON.stringify(mark),
                );
        },
        { marks, prefix: markPrefix },
    );
}

export function removalMarks(page: Page): Promise<unknown[]> {
    return page.evaluate((prefix) => {
        const keys = Array.from(
            { length: window.localStorage.length },
            (_, index) => window.localStorage.key(index) ?? "",
        )
            .filter((key) => key.startsWith(prefix))
            .sort();
        return keys.map((key) => {
            const value = window.localStorage.getItem(key) ?? "";
            try {
                return JSON.parse(value) as unknown;
            } catch {
                return value;
            }
        });
    }, markPrefix);
}

export async function onBlankPage(page: Page) {
    await page.route(`**${blank}`, (route) =>
        route.fulfill({ contentType: "text/html", body: "<html></html>" }),
    );
    await page.goto(blank);
}

export async function seedCopy(
    page: Page,
    {
        holder = holderOf(),
        plants = [heldPlant],
        days = heldDays,
        version = 1,
        flagged = true,
        marks = [],
    }: Copy = {},
) {
    await onBlankPage(page);
    await writeMarks(page, marks);
    await page.evaluate(
        ({ holder, plants, days, version, flagged }) =>
            new Promise<void>((resolve, reject) => {
                if (flagged) window.localStorage.setItem("pyracast.saved", "1");
                const request = indexedDB.open("pyracast-saved", version);
                request.onupgradeneeded = () => {
                    const database = request.result;
                    database.createObjectStore("holder");
                    database.createObjectStore("plants", {
                        keyPath: ["tenant", "site", "plant"],
                    });
                    database.createObjectStore("days", {
                        keyPath: ["tenant", "site", "plant", "kind", "date"],
                    });
                };
                request.onerror = () =>
                    reject(new Error("The copy could not be opened."));
                request.onsuccess = () => {
                    const database = request.result;
                    const transaction = database.transaction(
                        ["holder", "plants", "days"],
                        "readwrite",
                    );
                    transaction.objectStore("holder").put(holder, "holder");
                    for (const plant of plants)
                        transaction.objectStore("plants").put(plant);
                    for (const day of days)
                        transaction.objectStore("days").put(day);
                    transaction.oncomplete = () => {
                        database.close();
                        resolve();
                    };
                    transaction.onabort = () =>
                        reject(
                            new Error("The copy could not be read or written."),
                        );
                };
            }),
        { holder, plants, days, version, flagged },
    );
}

export type ReadCopy = {
    version?: number;
    holder?: Holder;
    plants: number;
    days: number;
    flag: string | null;
    marks: unknown[];
};

// Reads without creating: a database that is gone stays gone, and one that exists must open.
export async function readCopy(page: Page): Promise<ReadCopy> {
    const marks = await removalMarks(page);
    return page.evaluate(async (marks) => {
        const flag = window.localStorage.getItem("pyracast.saved");
        const listed = async () =>
            (await indexedDB.databases()).some(
                (database) => database.name === "pyracast-saved",
            );
        const absent = { plants: 0, days: 0, flag, marks };
        if (!(await listed())) return absent;
        return new Promise<ReadCopy>((resolve, reject) => {
            const request = indexedDB.open("pyracast-saved");
            request.onupgradeneeded = () => request.transaction?.abort();
            request.onerror = () =>
                void listed().then(
                    (exists) =>
                        exists
                            ? reject(
                                  new Error(
                                      "The copy exists but cannot be opened.",
                                  ),
                              )
                            : resolve(absent),
                    reject,
                );
            request.onsuccess = () => {
                const database = request.result;
                database.onversionchange = () => database.close();
                const transaction = database.transaction(
                    ["holder", "plants", "days"],
                    "readonly",
                );
                const holder = transaction.objectStore("holder").get("holder");
                const plants = transaction.objectStore("plants").count();
                const days = transaction.objectStore("days").count();
                transaction.oncomplete = () => {
                    database.close();
                    resolve({
                        version: database.version,
                        holder: holder.result as Holder | undefined,
                        plants: plants.result,
                        days: days.result,
                        flag,
                        marks,
                    });
                };
                transaction.onabort = () =>
                    reject(new Error("The copy could not be read or written."));
            };
        });
    }, marks);
}

// Another page holds a write transaction until `release` commits it, with the holder given; later transactions queue behind it.
export async function holdCopy(page: Page) {
    const other = await page.context().newPage();
    await onBlankPage(other);
    await other.evaluate(
        () =>
            new Promise<void>((resolve) => {
                const request = indexedDB.open("pyracast-saved");
                request.onsuccess = () => {
                    const database = request.result;
                    database.onversionchange = () => database.close();
                    const transaction = database.transaction(
                        ["holder", "plants", "days"],
                        "readwrite",
                    );
                    const keep = () => {
                        const release = Reflect.get(window, "copyReleased") as
                            { holder?: unknown } | undefined;
                        if (release) {
                            if (release.holder)
                                transaction
                                    .objectStore("holder")
                                    .put(release.holder, "holder");
                            database.close();
                            return;
                        }
                        transaction.objectStore("plants").count().onsuccess =
                            keep;
                    };
                    keep();
                    resolve();
                };
            }),
    );
    return (holder?: Holder) =>
        other.evaluate(
            (written) =>
                Object.assign(window, { copyReleased: { holder: written } }),
            holder,
        );
}

// Another page keeps a connection open that ignores a request to delete the copy until `release`.
export async function blockDeletion(page: Page) {
    const other = await page.context().newPage();
    await onBlankPage(other);
    await other.evaluate(
        () =>
            new Promise<void>((resolve) => {
                const request = indexedDB.open("pyracast-saved");
                request.onsuccess = () => {
                    const database = request.result;
                    database.onversionchange = () =>
                        Object.assign(window, { deletionAsked: true });
                    Object.assign(window, { heldCopy: database });
                    resolve();
                };
            }),
    );
    return {
        asked: () => other.evaluate(() => "deletionAsked" in window),
        release: () =>
            other.evaluate(() =>
                (Reflect.get(window, "heldCopy") as IDBDatabase).close(),
            ),
    };
}

export async function refuseClears(page: Page) {
    await page.addInitScript(() => {
        const clear = Object.getOwnPropertyDescriptor(
            IDBObjectStore.prototype,
            "clear",
        )!.value as (this: IDBObjectStore) => IDBRequest<undefined>;
        IDBObjectStore.prototype.clear = function (this: IDBObjectStore) {
            if (window.localStorage.getItem("refuseClears") !== null)
                throw new DOMException("Clear refused.", "UnknownError");
            return clear.call(this);
        };
    });
    await page.evaluate(() => window.localStorage.setItem("refuseClears", "1"));
    return () =>
        page.evaluate(() => window.localStorage.removeItem("refuseClears"));
}

// Counts the write transactions each later document of this page opens, so a case can wait until its work is queued.
export async function countWrites(page: Page) {
    await page.addInitScript(() => {
        const transaction = Object.getOwnPropertyDescriptor(
            IDBDatabase.prototype,
            "transaction",
        )!.value as (
            this: IDBDatabase,
            ...operands: unknown[]
        ) => IDBTransaction;
        IDBDatabase.prototype.transaction = function (
            this: IDBDatabase,
            ...operands: unknown[]
        ) {
            if (operands[1] === "readwrite") {
                const count = Reflect.get(window, "copyWrites") as
                    number | undefined;
                Object.assign(window, { copyWrites: (count ?? 0) + 1 });
            }
            return transaction.apply(this, operands);
        } as IDBDatabase["transaction"];
    });
    return () =>
        page.evaluate(
            () =>
                (Reflect.get(window, "copyWrites") as number | undefined) ?? 0,
        );
}

export function markOf(
    session: string,
    removal = "fedcba9876543210fedcba9876543210",
): RemovalMark {
    return { removal, user: 1, session };
}

// Once armed, the next write of a holder keeps its transaction open until `release` or an abort.
export async function holdHolderWrite(page: Page) {
    await page.addInitScript(() => {
        const put = Object.getOwnPropertyDescriptor(
            IDBObjectStore.prototype,
            "put",
        )!.value as (
            this: IDBObjectStore,
            ...operands: unknown[]
        ) => IDBRequest;
        IDBObjectStore.prototype.put = function (
            this: IDBObjectStore,
            ...operands: unknown[]
        ) {
            const request = put.apply(this, operands);
            if (this.name === "holder" && Reflect.get(window, "holdWrite")) {
                Reflect.deleteProperty(window, "holdWrite");
                const { transaction } = this;
                const keep = () => {
                    if (!Reflect.get(window, "writeReleased"))
                        transaction.objectStore("plants").count().onsuccess =
                            keep;
                };
                keep();
                Object.assign(window, { writeHeld: true });
            }
            return request;
        } as IDBObjectStore["put"];
    });
    return {
        arm: () =>
            page.evaluate(() => Object.assign(window, { holdWrite: true })),
        held: () => page.evaluate(() => "writeHeld" in window),
        release: () =>
            page.evaluate(() => Object.assign(window, { writeReleased: true })),
    };
}

// Records the flag at each success of a deletion this page asks for, before the page's own handler runs.
export async function watchDeletions(page: Page) {
    await page.addInitScript(() => {
        const deleteDatabase = Object.getOwnPropertyDescriptor(
            IDBFactory.prototype,
            "deleteDatabase",
        )!.value as (this: IDBFactory, name: string) => IDBOpenDBRequest;
        IDBFactory.prototype.deleteDatabase = function (
            this: IDBFactory,
            name: string,
        ) {
            const request = deleteDatabase.call(this, name);
            request.addEventListener("success", () => {
                const flags = (Reflect.get(window, "copyDeletions") ?? []) as (
                    string | null
                )[];
                flags.push(window.localStorage.getItem("pyracast.saved"));
                Object.assign(window, { copyDeletions: flags });
            });
            return request;
        };
    });
    return () =>
        page.evaluate(
            () =>
                (Reflect.get(window, "copyDeletions") ?? []) as (
                    string | null
                )[],
        );
}

// The store chunk is the only script that names the database; holding it holds the document's copy work.
export async function watchStoreChunk(page: Page, held = false) {
    let requested = false;
    let reach!: () => void;
    const reached = new Promise<void>((resolve) => (reach = resolve));
    let release = () => {};
    const released = held
        ? new Promise<void>((resolve) => (release = resolve))
        : Promise.resolve();
    await page.route("**/_next/static/chunks/**", async (route) => {
        const response = await route.fetch();
        const body = await response.text();
        if (body.includes("pyracast-saved")) {
            requested = true;
            reach();
            await released;
        }
        await route.fulfill({ response, body });
    });
    return { requested: () => requested, reached, release: () => release() };
}

function transpiled(name: string) {
    const file = path.resolve(__dirname, `../../src/lib/saved/${name}.ts`);
    return ts.transpileModule(readFileSync(file, "utf8"), {
        compilerOptions: {
            target: ts.ScriptTarget.ES2022,
            module: ts.ModuleKind.CommonJS,
        },
    }).outputText;
}

export async function loadStore(page: Page, savedFigures: boolean) {
    await onBlankPage(page);
    await page.evaluate(
        `(() => {
            const modules = { "./feature": { SAVED_FIGURES: ${savedFigures} } };
            const load = (text) => { const exports = {}; new Function("exports", "require", text)(exports, (name) => modules[name]); return exports; };
            window.savedDeadline = load(${JSON.stringify(transpiled("deadline"))});
            window.savedStore = load(${JSON.stringify(transpiled("store"))});
        })()`,
    );
}

type LoadedStore = Record<
    "start" | "copyState",
    (...operands: unknown[]) => Promise<unknown>
>;

export async function callStore(page: Page, operation: keyof LoadedStore) {
    await page.evaluate((name) => {
        const store = Reflect.get(window, "savedStore") as LoadedStore;
        const { deadline } = Reflect.get(window, "savedDeadline") as {
            deadline: (limitMs: number) => unknown;
        };
        const none = { outstanding: () => [], acknowledge: () => {} };
        const limit = deadline(2_000);
        const deleted = () => {
            const count = Reflect.get(window, "storeDeletions") as
                number | undefined;
            Object.assign(window, { storeDeletions: (count ?? 0) + 1 });
        };
        Object.assign(window, {
            storeAnswer:
                name === "start"
                    ? store.start(none, limit, deleted)
                    : store[name](limit),
        });
    }, operation);
    return () =>
        page.evaluate(async () => ({
            answer: await (Reflect.get(
                window,
                "storeAnswer",
            ) as Promise<unknown>),
            deleted:
                (Reflect.get(window, "storeDeletions") as number | undefined) ??
                0,
        }));
}

export function writeStore(
    page: Page,
    operation: string,
    ...operands: unknown[]
): Promise<unknown> {
    return page.evaluate(
        ({ operation, operands }) => {
            const store = Reflect.get(window, "savedStore") as Record<
                string,
                (...values: unknown[]) => Promise<unknown>
            >;
            const { deadline } = Reflect.get(window, "savedDeadline") as {
                deadline: (limitMs: number) => unknown;
            };
            const call = store[operation];
            if (!call) throw new Error(`The store has no ${operation}.`);
            return call(...operands, deadline(2_000));
        },
        { operation, operands },
    );
}

export const plantKey = { tenant: 1, site: 11, plant: 21 };

export function heldPlantOf(fields: Record<string, unknown> = {}) {
    return { ...heldPlant, ...plantKey, ...fields };
}

export type HeldDay = {
    tenant: number;
    site: number;
    plant: number;
    kind: string;
    date: string;
    total_mwh: number | null;
};

export function readHeld(
    page: Page,
): Promise<{ plants: Record<string, unknown>[]; days: HeldDay[] }> {
    return page.evaluate(
        () =>
            new Promise((resolve, reject) => {
                const request = indexedDB.open("pyracast-saved");
                request.onupgradeneeded = () => request.transaction?.abort();
                request.onerror = () => resolve({ plants: [], days: [] });
                request.onsuccess = () => {
                    const database = request.result;
                    database.onversionchange = () => database.close();
                    const transaction = database.transaction(
                        ["plants", "days"],
                        "readonly",
                    );
                    const plants = transaction.objectStore("plants").getAll();
                    const days = transaction.objectStore("days").getAll();
                    transaction.oncomplete = () => {
                        database.close();
                        resolve({
                            plants: plants.result as Record<string, unknown>[],
                            days: days.result as HeldDay[],
                        });
                    };
                    transaction.onabort = () =>
                        reject(new Error("The copy could not be read."));
                };
            }),
    );
}

export function daysAnswer(total = 1.5, date = "2026-11-11") {
    return {
        plant_id: plantKey.plant,
        name: "East array",
        today: "2026-11-10",
        forecast: [
            {
                date,
                timezone: "Europe/Istanbul",
                source_kind: "forecast",
                freshness: "fresh",
                stale_reason: null,
                unavailable_reason: null,
                collected_at: "2026-11-10T06:00:00+03:00",
                total_mwh: total,
                partial: false,
                affected: null,
                rows: [
                    {
                        start: `${date}T12:00:00+03:00`,
                        end: `${date}T13:00:00+03:00`,
                        mwh: total,
                        affected_mwh: null,
                        sources: null,
                    },
                ],
            },
        ],
        production: [
            {
                date: "2026-11-09",
                timezone: "Europe/Istanbul",
                total_mwh: 1.2,
                complete: false,
                rows: [
                    {
                        start: "2026-11-09T12:00:00+03:00",
                        end: "2026-11-09T13:00:00+03:00",
                        mwh: 1.2,
                        quality: "valid",
                        source: "meter",
                        provisional: false,
                    },
                ],
            },
        ],
    };
}

// The worker judges the copy's times by its own clock, which the page clock does not move.
export async function workerClock(page: Page) {
    const context = page.context();
    const worker =
        context.serviceWorkers()[0] ??
        (await context.waitForEvent("serviceworker"));
    return worker.evaluate(() => Date.now());
}

export async function supplyRegistration(page: Page) {
    await page.addInitScript(() => {
        ServiceWorkerContainer.prototype.getRegistration = function (
            this: ServiceWorkerContainer,
            scope?: string,
        ) {
            return Promise.resolve(
                scope === "/app/"
                    ? ({
                          scope: `${location.origin}/app/`,
                      } as ServiceWorkerRegistration)
                    : undefined,
            );
        };
    });
}

export async function controlVisibility(page: Page) {
    await page.addInitScript(() => {
        Object.defineProperty(document, "visibilityState", {
            configurable: true,
            get: () =>
                Reflect.get(window, "pageHidden") ? "hidden" : "visible",
        });
    });
    const set = (hidden: boolean) =>
        page.evaluate((value) => {
            Object.assign(window, { pageHidden: value });
            document.dispatchEvent(new Event("visibilitychange"));
        }, hidden);
    return { hide: () => set(true), show: () => set(false) };
}
