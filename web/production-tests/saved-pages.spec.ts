import { expect, test, type Page } from "@playwright/test";
import { accessibilityViolations } from "../tests/support/accessibility";
import { workerWithdrawn } from "../tests/support/worker-state";

const NOTICE_POLICY =
    "default-src 'none'; style-src 'unsafe-inline'; form-action 'none'; base-uri 'none'; frame-ancestors 'none'";
const TOKEN = "0123456789abcdef0123456789abcdef";
const KEY = { tenant: 3, site: 5, plant: 8 };
const FORECAST = "/app/w/3/plants/5/forecast/";
const HOUR = 3_600_000;
const DAY = 24 * HOUR;
const EXPIRED =
    "The saved figures were removed from this device because no one signed in here for 90 days or the device clock was changed.";

type Marked = Window & { violations?: string[] };
type Seed = { confirmedAge: number; dates: [string, ...string[]] };

function rows(date: string, extra: object) {
    const start = Date.parse(`${date}T09:00:00Z`);
    return [0, 1].map((index) => ({
        start: new Date(start + index * HOUR).toISOString(),
        end: new Date(start + (index + 1) * HOUR).toISOString(),
        mwh: 0.5 + index,
        ...extra,
    }));
}

const forecastDay = (date: string) => ({
    ...KEY,
    kind: "forecast",
    date,
    timezone: "Europe/Istanbul",
    source_kind: "forecast",
    freshness: "fresh",
    stale_reason: null,
    unavailable_reason: null,
    collected_at: `${date}T03:00:00+03:00`,
    total_mwh: 2,
    partial: false,
    affected: null,
    rows: rows(date, { affected_mwh: null, sources: null }),
});

const productionDay = (date: string) => ({
    ...KEY,
    kind: "production",
    date,
    timezone: "Europe/Istanbul",
    total_mwh: 2,
    complete: true,
    rows: rows(date, { quality: "valid", source: "epias", provisional: true }),
});

async function controlled(page: Page) {
    await page.goto("/app/");
    await page.evaluate(() => navigator.serviceWorker.ready);
    await page.reload();
    expect(
        await page.evaluate(() => navigator.serviceWorker.controller !== null),
    ).toBe(true);
}

async function seed(page: Page, { confirmedAge, dates }: Seed) {
    const records = {
        holder: {
            user: 7,
            language: "en",
            session: "tag-1",
            active: true,
            removal: TOKEN,
            retryAt: null,
        },
        plant: {
            ...KEY,
            name: "North field",
            workspace: "Solar Co",
            openedAt: null,
            attemptedAt: null,
        },
        days: [...dates.map(forecastDay), productionDay(dates[0])],
    };
    await page.evaluate(
        async ({ holder, plant, days, confirmedAge }) => {
            const request = indexedDB.open("pyracast-saved", 1);
            request.onupgradeneeded = () => {
                request.result.createObjectStore("holder");
                request.result.createObjectStore("plants", {
                    keyPath: ["tenant", "site", "plant"],
                });
                request.result.createObjectStore("days", {
                    keyPath: ["tenant", "site", "plant", "kind", "date"],
                });
            };
            const database = await new Promise<IDBDatabase>(
                (resolve, reject) => {
                    request.onsuccess = () => resolve(request.result);
                    request.onerror = () =>
                        reject(new Error("The copy could not be created."));
                },
            );
            const now = Date.now();
            const transaction = database.transaction(
                ["holder", "plants", "days"],
                "readwrite",
            );
            transaction
                .objectStore("holder")
                .put({ ...holder, confirmedAt: now - confirmedAge }, "holder");
            transaction
                .objectStore("plants")
                .put({ ...plant, refreshedAt: now - 2 * 3_600_000 });
            for (const day of days) transaction.objectStore("days").put(day);
            await new Promise((resolve, reject) => {
                transaction.oncomplete = resolve;
                transaction.onerror = () =>
                    reject(new Error("The copy could not be written."));
            });
            database.close();
        },
        { ...records, confirmedAge },
    );
}

const stored = (page: Page) =>
    page.evaluate(async () => {
        const request = indexedDB.open("pyracast-saved");
        const database = await new Promise<IDBDatabase>((resolve, reject) => {
            request.onsuccess = () => resolve(request.result);
            request.onerror = () =>
                reject(new Error("The copy could not be opened."));
        });
        const transaction = database.transaction(
            ["holder", "plants", "days"],
            "readonly",
        );
        const read = (
            store: string,
            run: (store: IDBObjectStore) => IDBRequest,
        ) =>
            new Promise((resolve) => {
                run(transaction.objectStore(store)).onsuccess = (event) =>
                    resolve((event.target as IDBRequest).result);
            });
        const answer = {
            holder: (await read("holder", (store) => store.get("holder"))) as {
                active: boolean;
            },
            plants: await read("plants", (store) => store.count()),
            days: await read("days", (store) => store.count()),
        };
        database.close();
        return {
            active: answer.holder.active,
            plants: answer.plants,
            days: answer.days,
        };
    });

async function offlineWithCopy(page: Page, seeded: Seed) {
    await controlled(page);
    await seed(page, seeded);
    await page.addInitScript(() => {
        const marked = window as Marked;
        marked.violations = [];
        document.addEventListener("securitypolicyviolation", (event) =>
            marked.violations?.push(event.effectiveDirective),
        );
    });
    await page.context().setOffline(true);
}

async function expectSavedPage(page: Page) {
    expect(await page.evaluate(() => document.scripts.length)).toBe(0);
    expect(await page.evaluate(() => (window as Marked).violations)).toEqual(
        [],
    );
    expect(await accessibilityViolations(page)).toEqual([]);
}

const fresh: Seed = { confirmedAge: DAY, dates: ["2026-11-11", "2026-11-12"] };

if (!workerWithdrawn)
    test.describe("saved pages of the worker", () => {
        test("a held plant's address shows its saved figures and its links lead to the other held days", async ({
            page,
        }) => {
            await offlineWithCopy(page, fresh);
            const answer = await page.goto(`${FORECAST}?date=2026-11-11`);
            expect(answer?.status()).toBe(503);
            expect(answer?.fromServiceWorker()).toBe(true);
            expect(answer?.headers()["content-security-policy"]).toBe(
                NOTICE_POLICY,
            );
            expect(answer?.headers()["cache-control"]).toBe("no-store");
            await expect(
                page.getByRole("heading", {
                    name: "North field, Solar Co: Forecast",
                }),
            ).toBeVisible();
            await expect(
                page.getByRole("heading", { name: "November 11, 2026" }),
            ).toBeVisible();
            await expect(
                page.getByText(/^Read from the server on /),
            ).toBeVisible();
            await expect(
                page.getByRole("img", {
                    name: /^Energy per interval of North field/,
                }),
            ).toBeVisible();
            await expect(page.getByRole("row")).toHaveCount(3);
            await expectSavedPage(page);

            await page.getByRole("link", { name: "Next saved day" }).click();
            await expect(page).toHaveURL(`${FORECAST}?date=2026-11-12`);
            await expect(
                page.getByRole("heading", { name: "November 12, 2026" }),
            ).toBeVisible();

            await page.getByRole("link", { name: "Production" }).click();
            await expect(page).toHaveURL("/app/w/3/plants/5/production/");
            await expect(
                page.getByRole("heading", {
                    name: "North field, Solar Co: Production",
                }),
            ).toBeVisible();
            await expect(page.getByText("Complete day")).toBeVisible();
            await expect(
                page.getByRole("cell", { name: "Provisional" }),
            ).toHaveCount(2);
            await expectSavedPage(page);
        });

        test("the application's address lists the saved plants", async ({
            page,
        }) => {
            await offlineWithCopy(page, fresh);
            const answer = await page.goto("/app/");
            expect(answer?.status()).toBe(503);
            await expect(
                page.getByRole("heading", {
                    name: "Figures saved on this device",
                }),
            ).toBeVisible();
            await page.getByRole("link", { name: "North field" }).click();
            await expect(page).toHaveURL(FORECAST);
            await expect(
                page.getByRole("heading", {
                    name: "North field, Solar Co: Forecast",
                }),
            ).toBeVisible();
        });

        test("a copy outside its 90 days is emptied and the notice says so once", async ({
            page,
        }) => {
            await offlineWithCopy(page, { ...fresh, confirmedAge: 91 * DAY });
            await page.goto(`${FORECAST}?date=2026-11-11`);
            await expect(page.getByText(EXPIRED)).toBeVisible();
            await expect(page.getByText("North field")).toHaveCount(0);
            expect(await stored(page)).toEqual({
                active: false,
                plants: 0,
                days: 0,
            });
            await page.reload();
            await expect(
                page.getByRole("heading", {
                    name: "Pyracast cannot be reached",
                }),
            ).toBeVisible();
            await expect(page.getByText(EXPIRED)).toHaveCount(0);
        });

        test("removing the saved figures leaves no saved page", async ({
            page,
        }) => {
            await offlineWithCopy(page, fresh);
            await page.goto(`${FORECAST}?date=2026-11-11`);
            await page
                .getByRole("link", {
                    name: "Remove saved figures from this device",
                })
                .click();
            await expect(
                page.getByRole("heading", {
                    name: "Remove saved figures from this device?",
                }),
            ).toBeVisible();
            expect((await stored(page)).plants).toBe(1);
            await page
                .getByRole("link", {
                    name: "Remove saved figures",
                    exact: true,
                })
                .click();
            await expect(
                page.getByText(
                    "The saved figures were removed from this device.",
                ),
            ).toBeVisible();
            expect(await stored(page)).toEqual({
                active: false,
                plants: 0,
                days: 0,
            });
            await page.goto(`${FORECAST}?date=2026-11-11`);
            await expect(
                page.getByRole("heading", {
                    name: "Pyracast cannot be reached",
                }),
            ).toBeVisible();
            await expect(page.getByText("North field")).toHaveCount(0);
            await expect(page.getByRole("table")).toHaveCount(0);
        });
    });
