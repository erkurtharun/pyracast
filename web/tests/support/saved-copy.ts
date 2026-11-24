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

export type Copy = {
    holder?: Holder;
    plants?: unknown[];
    days?: unknown[];
    version?: number;
    flagged?: boolean;
    mark?: string;
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
        flagged = true,
        mark,
    }: Copy = {},
) {
    await onBlankPage(page);
    await page.evaluate(
        ({ holder, plants, days, version, flagged, mark }) =>
            new Promise<void>((resolve, reject) => {
                if (flagged) window.localStorage.setItem("pyracast.saved", "1");
                if (mark)
                    window.localStorage.setItem("pyracast.savedRemoval", mark);
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
        { holder, plants, days, version, flagged, mark },
    );
}

export type ReadCopy = {
    version?: number;
    holder?: Holder;
    plants: number;
    days: number;
    flag: string | null;
    mark: string | null;
};

// Reads without creating: a database that is gone stays gone, and one that exists must open.
export function readCopy(page: Page): Promise<ReadCopy> {
    return page.evaluate(async () => {
        const flag = window.localStorage.getItem("pyracast.saved");
        const mark = window.localStorage.getItem("pyracast.savedRemoval");
        const listed = async () =>
            (await indexedDB.databases()).some(
                (database) => database.name === "pyracast-saved",
            );
        const absent = { plants: 0, days: 0, flag, mark };
        if (!(await listed())) return absent;
        return new Promise<ReadCopy>((resolve, reject) => {
            const request = indexedDB.open("pyracast-saved");
            request.onupgradeneeded = () => request.transaction?.abort();
            request.onerror = () =>
                void listed().then((exists) =>
                    exists
                        ? reject(
                              new Error(
                                  "The copy exists but cannot be opened.",
                              ),
                          )
                        : resolve(absent),
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
                        mark,
                    });
                };
                transaction.onabort = () =>
                    reject(new Error("The copy could not be read or written."));
            };
        });
    });
}

// Holds a write transaction over the copy's plants in another page until `release`, which closes it.
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
                    const store = database
                        .transaction(["plants"], "readwrite")
                        .objectStore("plants");
                    const keep = () => {
                        if ("copyReleased" in window) {
                            database.close();
                            return;
                        }
                        store.count().onsuccess = keep;
                    };
                    keep();
                    resolve();
                };
            }),
    );
    return () =>
        other.evaluate(() => Object.assign(window, { copyReleased: true }));
}

// Keeps a connection to the copy open in another page that ignores a request to delete it.
export async function blockDeletion(page: Page) {
    const other = await page.context().newPage();
    await onBlankPage(other);
    await other.evaluate(
        () =>
            new Promise<void>((resolve) => {
                const request = indexedDB.open("pyracast-saved");
                request.onsuccess = () => {
                    request.result.onversionchange = () =>
                        Object.assign(window, { deletionAsked: true });
                    resolve();
                };
            }),
    );
    return () => other.evaluate(() => "deletionAsked" in window);
}

// Every clear of an object store throws in this page's documents while the storage key is set.
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

const constant = "const SAVED_FIGURES = true;";

// The store module as source in a blank page of the origin, with its withdrawal constant set.
export async function loadStore(page: Page, savedFigures: boolean) {
    const file = path.resolve(__dirname, "../../src/lib/saved/store.ts");
    const source = readFileSync(file, "utf8");
    if (!source.includes(constant))
        throw new Error(`${file} no longer declares ${constant}`);
    const { outputText } = ts.transpileModule(
        source.replace(constant, `const SAVED_FIGURES = ${savedFigures};`),
        {
            compilerOptions: {
                target: ts.ScriptTarget.ES2022,
                module: ts.ModuleKind.CommonJS,
            },
        },
    );
    await onBlankPage(page);
    await page.evaluate(
        `(() => { const exports = {}; ${outputText}\nwindow.savedStore = exports; })()`,
    );
}

type LoadedStore = Record<
    "start" | "copyState",
    (removal?: unknown) => Promise<string>
>;

// Calls an operation of the loaded store and keeps its answer in the page until `answer` reads it.
export async function callStore(page: Page, operation: keyof LoadedStore) {
    await page.evaluate((name) => {
        const store = Reflect.get(window, "savedStore") as LoadedStore;
        const none = { outstanding: () => null, acknowledge: () => {} };
        Object.assign(window, { storeAnswer: store[name](none) });
    }, operation);
    return () =>
        page.evaluate(
            () => Reflect.get(window, "storeAnswer") as Promise<string>,
        );
}
