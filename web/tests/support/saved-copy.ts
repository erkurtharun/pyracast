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
    marked?: boolean;
};

export async function onBlankPage(page: Page) {
    await page.route(`**${blank}`, (route) =>
        route.fulfill({ contentType: "text/html", body: "<html></html>" }),
    );
    await page.goto(blank);
}

// Writes a copy in format 1 from a blank page of the application's origin.
export async function seedCopy(
    page: Page,
    {
        holder = holderOf(),
        plants = [heldPlant],
        days = heldDays,
        version = 1,
        flagged = true,
        marked = false,
    }: Copy = {},
) {
    await onBlankPage(page);
    await page.evaluate(
        ({ holder, plants, days, version, flagged, marked }) =>
            new Promise<void>((resolve, reject) => {
                if (flagged) window.localStorage.setItem("pyracast.saved", "1");
                if (marked)
                    window.localStorage.setItem("pyracast.savedRemoval", "1");
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
        { holder, plants, days, version, flagged, marked },
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

// Reads without creating: a database that is gone stays gone.
export function readCopy(page: Page): Promise<ReadCopy> {
    return page.evaluate(async () => {
        const flag = window.localStorage.getItem("pyracast.saved");
        const mark = window.localStorage.getItem("pyracast.savedRemoval");
        const listed = (await indexedDB.databases()).find(
            (database) => database.name === "pyracast-saved",
        );
        if (!listed) return { plants: 0, days: 0, flag, mark };
        return new Promise<ReadCopy>((resolve, reject) => {
            const request = indexedDB.open("pyracast-saved");
            request.onupgradeneeded = () => request.transaction?.abort();
            request.onerror = () => resolve({ plants: 0, days: 0, flag, mark });
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

// Holds a write transaction over the copy's plants in another page until `release`.
export async function holdCopy(page: Page) {
    const other = await page.context().newPage();
    await onBlankPage(other);
    await other.evaluate(
        () =>
            new Promise<void>((resolve) => {
                const request = indexedDB.open("pyracast-saved");
                request.onsuccess = () => {
                    const database = request.result;
                    const store = database
                        .transaction(["plants"], "readwrite")
                        .objectStore("plants");
                    const keep = () => {
                        if ("copyReleased" in window) return;
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
