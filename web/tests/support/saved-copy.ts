import { readFileSync } from "node:fs";
import path from "node:path";
import ts from "typescript";
import { expect, instant, type Page } from "./test";

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

// The device's marks, kept as keys of the holder store beside the holder.
type Mark = ["off", number] | ["ended", number, string];

export function offMark(user = 1): Mark {
    return ["off", user];
}

export function endedMark(session: string, at = instant.getTime()): Mark {
    return ["ended", at, session];
}

// A mark the application writes at a time it reads from its clock.
export function anEndedMark(session: string) {
    return ["ended", expect.any(Number), session];
}

// Each case of the one rule, for a holder of user `user` and session `session`: whether its figures are shown.
export function refusalCases(user: number, session: string) {
    return [
        { name: "without a mark", marks: [], shown: true },
        {
            name: "turned off by its person",
            marks: [offMark(user)],
            shown: false,
        },
        {
            name: "turned off by another person",
            marks: [offMark(user + 1)],
            shown: true,
        },
        {
            name: "whose session ended",
            marks: [endedMark(session)],
            shown: false,
        },
        {
            name: "whose session ended long ago",
            marks: [endedMark(session, 1)],
            shown: false,
        },
        {
            name: "of which another session ended",
            marks: [endedMark(`${session}-other`)],
            shown: true,
        },
    ] satisfies { name: string; marks: Mark[]; shown: boolean }[];
}

export type Copy = {
    holder?: Holder | null;
    plants?: unknown[];
    days?: unknown[];
    version?: number;
    marks?: Mark[];
};

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
        marks = [],
    }: Copy = {},
) {
    await onBlankPage(page);
    await page.evaluate(
        ({ holder, plants, days, version, marks }) =>
            new Promise<void>((resolve, reject) => {
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
                    const holders = transaction.objectStore("holder");
                    if (holder) holders.put(holder, "holder");
                    for (const mark of marks) holders.put(true, mark);
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
        { holder, plants, days, version, marks },
    );
}

type ReadCopy = {
    version?: number;
    holder?: Holder;
    plants: number;
    days: number;
    marks: unknown[];
};

// Reads without creating: a database that is gone stays gone, and one that exists must open.
export async function readCopy(page: Page): Promise<ReadCopy> {
    return page.evaluate(async () => {
        const listed = async () =>
            (await indexedDB.databases()).some(
                (database) => database.name === "pyracast-saved",
            );
        const absent = { plants: 0, days: 0, marks: [] };
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
                const keys = transaction.objectStore("holder").getAllKeys();
                const plants = transaction.objectStore("plants").count();
                const days = transaction.objectStore("days").count();
                transaction.oncomplete = () => {
                    database.close();
                    resolve({
                        version: database.version,
                        holder: holder.result as Holder | undefined,
                        plants: plants.result,
                        days: days.result,
                        marks: keys.result.filter((key) => key !== "holder"),
                    });
                };
                transaction.onabort = () =>
                    reject(new Error("The copy could not be read or written."));
            };
        });
    });
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

type HeldKey = "holder" | "off" | "ended";

// Once armed in this tab, each write or removal of the holder or of a mark of that kind throws until allowed again.
export async function refuseWrites(page: Page, kind: HeldKey) {
    const armed = `refuseWrites-${kind}`;
    await page.addInitScript(
        ({ kind, armed }) => {
            const refused = (key: unknown) =>
                (Array.isArray(key) ? key[0] : key) === kind &&
                window.sessionStorage.getItem(armed) !== null;
            for (const name of ["put", "delete"] as const) {
                const original = Object.getOwnPropertyDescriptor(
                    IDBObjectStore.prototype,
                    name,
                )!.value as (
                    this: IDBObjectStore,
                    ...operands: unknown[]
                ) => IDBRequest;
                Object.defineProperty(IDBObjectStore.prototype, name, {
                    configurable: true,
                    writable: true,
                    value: function (
                        this: IDBObjectStore,
                        ...operands: unknown[]
                    ) {
                        if (refused(name === "put" ? operands[1] : operands[0]))
                            throw new DOMException("Refused.", "UnknownError");
                        return original.apply(this, operands);
                    },
                });
            }
        },
        { kind, armed },
    );
    return {
        refuse: () =>
            page.evaluate(
                (armed) => window.sessionStorage.setItem(armed, "1"),
                armed,
            ),
        allow: () =>
            page.evaluate(
                (armed) => window.sessionStorage.removeItem(armed),
                armed,
            ),
    };
}

// Counts the transactions of a mode each later document of this page opens, so a case can wait until its work is queued.
async function countTransactions(page: Page, mode: IDBTransactionMode) {
    const counter = `copyTransactions-${mode}`;
    await page.addInitScript(
        ({ mode, counter }) => {
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
                if ((operands[1] ?? "readonly") === mode) {
                    const count = Reflect.get(window, counter) as
                        number | undefined;
                    Object.assign(window, { [counter]: (count ?? 0) + 1 });
                }
                return transaction.apply(this, operands);
            } as IDBDatabase["transaction"];
        },
        { mode, counter },
    );
    return () =>
        page.evaluate(
            (counter) =>
                (Reflect.get(window, counter) as number | undefined) ?? 0,
            counter,
        );
}

export function countWrites(page: Page) {
    return countTransactions(page, "readwrite");
}

export function countReads(page: Page) {
    return countTransactions(page, "readonly");
}

// Once armed, the next write of the holder or of a mark of that kind keeps its transaction open until `release` or an abort.
export async function holdWrite(page: Page) {
    await page.addInitScript(() => {
        const put = Object.getOwnPropertyDescriptor(
            IDBObjectStore.prototype,
            "put",
        )!.value as (
            this: IDBObjectStore,
            ...operands: unknown[]
        ) => IDBRequest;
        const kind = (key: unknown) => (Array.isArray(key) ? key[0] : key);
        IDBObjectStore.prototype.put = function (
            this: IDBObjectStore,
            ...operands: unknown[]
        ) {
            const request = put.apply(this, operands);
            const armed: unknown = Reflect.get(window, "holdWrite");
            if (armed !== undefined && kind(operands[1]) === armed) {
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
        arm: (key: HeldKey = "holder") =>
            page.evaluate(
                (key) => Object.assign(window, { holdWrite: key }),
                key,
            ),
        held: () => page.evaluate(() => "writeHeld" in window),
        release: () =>
            page.evaluate(() => Object.assign(window, { writeReleased: true })),
    };
}

// The store chunk is the only script that creates the copy's stores; holding it holds the document's copy work.
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
        if (/"kind",\s*"date"/.test(body)) {
            requested = true;
            reach();
            await released;
        }
        await route.fulfill({ response, body });
    });
    return {
        requested: () => requested,
        reached,
        release: () => release(),
        stop: () => page.unrouteAll({ behavior: "wait" }),
    };
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

export async function loadStore(page: Page) {
    await onBlankPage(page);
    await page.evaluate(
        `(() => {
            const load = (text) => { const exports = {}; new Function("exports", text)(exports); return exports; };
            window.savedDeadline = load(${JSON.stringify(transpiled("deadline"))});
            window.savedStore = load(${JSON.stringify(transpiled("store"))});
        })()`,
    );
}

// Starts a store operation and answers a reader of its result; `copyState` answers for user 1 in session "tag-a".
export async function callStore(
    page: Page,
    operation: "removeCopy" | "copyState",
) {
    await page.evaluate((name) => {
        const store = Reflect.get(window, "savedStore") as Record<
            string,
            (...operands: unknown[]) => Promise<unknown>
        >;
        const { deadline } = Reflect.get(window, "savedDeadline") as {
            deadline: (limitMs: number) => unknown;
        };
        const limit = deadline(2_000);
        Object.assign(window, {
            storeAnswer:
                name === "removeCopy"
                    ? store.removeCopy!(limit)
                    : store.copyState!({ user: 1, session: "tag-a" }, limit),
        });
    }, operation);
    return () =>
        page.evaluate(
            () => Reflect.get(window, "storeAnswer") as Promise<unknown>,
        );
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

type HeldDay = {
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
    return worker.evaluate(() =>
        Math.floor(performance.timeOrigin + performance.now()),
    );
}

// A late registration is found only after `complete`, which also settles `ready` as a worker's activation does; after `fail` every lookup throws.
export async function supplyRegistration(page: Page, { late = false } = {}) {
    await page.addInitScript((late) => {
        const registration = {
            scope: `${location.origin}/app/`,
        } as ServiceWorkerRegistration;
        let registered = !late;
        let activate: (
            registration: ServiceWorkerRegistration,
        ) => void = () => {};
        const ready = new Promise<ServiceWorkerRegistration>(
            (resolve) => (activate = resolve),
        );
        let lookups = 0;
        Object.assign(window, {
            registrationLookups: () => lookups,
            completeRegistration: () => {
                registered = true;
                activate(registration);
            },
        });
        ServiceWorkerContainer.prototype.getRegistration = function (
            this: ServiceWorkerContainer,
            scope?: string,
        ) {
            lookups += 1;
            if (Reflect.get(window, "lookupsFail"))
                return Promise.reject(
                    new DOMException("Refused.", "InvalidStateError"),
                );
            return Promise.resolve(
                registered && scope === "/app/" ? registration : undefined,
            );
        };
        if (late)
            Object.defineProperty(ServiceWorkerContainer.prototype, "ready", {
                configurable: true,
                get: () => ready,
            });
    }, late);
    return {
        lookups: () =>
            page.evaluate(() =>
                (Reflect.get(window, "registrationLookups") as () => number)(),
            ),
        complete: () =>
            page.evaluate(() =>
                (Reflect.get(window, "completeRegistration") as () => void)(),
            ),
        fail: () =>
            page.evaluate(() => Object.assign(window, { lookupsFail: true })),
    };
}

const workerSource = readFileSync(
    path.resolve(__dirname, "../../public/app/sw.js"),
    "utf8",
);

// The worker's own script answers a navigation without a connection from this browser's copy, in a blank page of the same origin.
async function workerAnswer(page: Page, address = "/app/") {
    const blank = await page.context().newPage();
    await onBlankPage(blank);
    await blank.evaluate(() => {
        Object.assign(window, {
            registration: { scope: `${location.origin}/app/` },
            fetch: () => Promise.reject(new TypeError("offline")),
        });
    });
    await blank.addScriptTag({ content: workerSource });
    const result = await blank.evaluate(async (address) => {
        let answer: Promise<Response> | undefined;
        const navigation = Object.assign(new Event("fetch"), {
            request: {
                url: new URL(address, location.origin).href,
                method: "GET",
                mode: "navigate",
            },
            preloadResponse: Promise.resolve(undefined),
            respondWith: (response: Promise<Response>) => {
                answer = response;
            },
        });
        window.dispatchEvent(navigation);
        if (!answer) return undefined;
        const response = await answer;
        return { status: response.status, text: await response.text() };
    }, address);
    await blank.close();
    if (!result) throw new Error("The worker did not answer the navigation.");
    return result;
}

// Whether the worker shows a held plant's figures without a connection; otherwise it must answer with its offline notice.
export async function workerShows(page: Page, address = "/app/") {
    const { status, text } = await workerAnswer(page, address);
    expect(status).toBe(503);
    if (text.includes("East array")) return true;
    expect(text).toContain("The server did not answer. Try again in a moment.");
    return false;
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
