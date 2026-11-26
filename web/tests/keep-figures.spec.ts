import type { BrowserContext } from "@playwright/test";
import { expect, instant, test, type Page } from "./support/test";
import { errorBody } from "./support/errors";
import {
    firstAccount,
    invitationPreview,
    mockApi,
    paged,
    region,
    tenantRow,
    plant,
    type AccountState,
} from "./support/workspace";
import {
    focusWindow,
    isMarked,
    markDocument,
    serveSession,
} from "./support/session";
import {
    controlVisibility,
    daysAnswer,
    heldPlantOf,
    holderOf,
    holdHolderWrite,
    loadStore,
    onBlankPage,
    plantKey,
    readCopy,
    readHeld,
    seedCopy,
    supplyRegistration,
    watchStoreChunk,
    writeStore,
} from "./support/saved-copy";
import { savedFiguresOff } from "./support/worker-state";

test.skip(
    savedFiguresOff,
    "The switch and the refresh exist only while saved figures are on.",
);

const minute = 60_000;
const title = "Keep figures on this device";
const statement =
    "Anyone who can open this browser on this device can read these figures without signing in, until they are removed or you sign out. Do not turn this on for a shared device.";
const daysPath = /\/api\/tenants\/\d+\/regions\/\d+\/plants\/\d+\/days\/$/;
const owner = { user: 1, session: "tag-a" };
const storeLock = "pyracast.saved-copy";
const refreshLock = "pyracast-saved-refresh";

async function signedIn(page: Page, account?: AccountState) {
    const state = await mockApi(page, account && { account });
    await serveSession(page, state);
    return state;
}

async function pauseClock(page: Page) {
    const now = await page.evaluate(() => Date.now());
    await page.clock.pauseAt(now + 1_000);
}

function lockClient(page: Page) {
    return page.evaluate(() =>
        navigator.locks.request(`probe-${Math.random()}`, async (lock) => {
            const { held = [] } = await navigator.locks.query();
            return (
                held.find((entry) => entry.name === lock?.name)?.clientId ?? ""
            );
        }),
    );
}

function waitingFor(page: Page, client: string) {
    return page.evaluate(
        async ({ client, name }) => {
            const { pending = [] } = await navigator.locks.query();
            return pending.filter(
                (entry) => entry.name === name && entry.clientId === client,
            ).length;
        },
        { client, name: storeLock },
    );
}

// A blank page takes the copy's lock at each `take`, queued behind every request already waiting, and gives back the oldest hold at each `release`.
async function lockTaker(context: BrowserContext) {
    const holder = await context.newPage();
    await onBlankPage(holder);
    const client = await lockClient(holder);
    return {
        waiting: () => waitingFor(holder, client),
        waitingFor: (other: string) => waitingFor(holder, other),
        granted: () =>
            holder.evaluate(
                () =>
                    (Reflect.get(window, "granted") as number | undefined) ?? 0,
            ),
        take: () =>
            holder.evaluate((name) => {
                const holds = (Reflect.get(window, "holds") ??
                    []) as (() => void)[];
                Object.assign(window, { holds });
                void navigator.locks.request(
                    name,
                    () =>
                        new Promise<void>((resolve) => {
                            const granted = Reflect.get(window, "granted") as
                                number | undefined;
                            Object.assign(window, {
                                granted: (granted ?? 0) + 1,
                            });
                            holds.push(resolve);
                        }),
                );
            }, storeLock),
        release: () =>
            holder.evaluate(() =>
                (Reflect.get(window, "holds") as (() => void)[]).shift()?.(),
            ),
    };
}

function refreshIdle(page: Page) {
    return page.evaluate(async (name) => {
        const { held = [], pending = [] } = await navigator.locks.query();
        return ![...held, ...pending].some((entry) => entry.name === name);
    }, refreshLock);
}

async function watchAction(page: Page, name: string) {
    await page.getByRole("button", { name }).evaluate((button) =>
        new MutationObserver((records) => {
            if (records.some((record) => record.oldValue !== null))
                Object.assign(window, { actionEnded: true });
        }).observe(button, {
            attributeFilter: ["disabled"],
            attributeOldValue: true,
        }),
    );
    return () => page.evaluate(() => "actionEnded" in window);
}

function plants(page: Page) {
    return page.getByRole("heading", { name: "Plants", exact: true });
}

function plantHeading(page: Page) {
    return page.getByRole("heading", { name: "East array", level: 1 });
}

type DaysRequest = { path: string; at: number };

async function serveDays(
    page: Page,
    answer: (path: string) => number | object = () => daysAnswer(),
) {
    const requests: DaysRequest[] = [];
    await page.route(daysPath, async (route) => {
        const path = new URL(route.request().url()).pathname;
        requests.push({
            path,
            at: await page.evaluate(() => Date.now()),
        });
        const result = answer(path);
        if (typeof result === "number")
            await route.fulfill({
                status: result,
                json: errorBody(
                    result === 429 ? "throttled" : "busy",
                    "Refused.",
                    result === 429 ? { retry_after: 600 } : {},
                ),
            });
        else await route.fulfill({ json: result });
    });
    return requests;
}

function countRequests(page: Page, pathname: string) {
    let count = 0;
    page.on("request", (request) => {
        if (new URL(request.url()).pathname === pathname) count += 1;
    });
    return () => count;
}

async function openSwitch(page: Page) {
    await page.getByText(title, { exact: true }).click();
    await expect(page.getByText(statement)).toBeVisible();
}

function heldTotals(page: Page) {
    return readHeld(page).then(({ days }) =>
        days.map((day) => [day.kind, day.date, day.total_mwh]),
    );
}

async function heldCopy(page: Page, plants: unknown[]) {
    await seedCopy(page, {
        holder: holderOf({ confirmedAt: instant.getTime() }),
        plants,
        days: [],
    });
}

test("without the worker's registration the shell offers no switch and loads no store", async ({
    page,
}) => {
    await signedIn(page);
    const chunk = await watchStoreChunk(page);
    await page.goto("/app/w/1/");
    await expect(plants(page)).toBeVisible();
    await expect(page.getByText(title)).toHaveCount(0);
    expect(chunk.requested()).toBe(false);
});

test("the switch turned on in an open document holds the workspace's plant and its figures without a reload, and removing them leaves nothing", async ({
    page,
}) => {
    await supplyRegistration(page);
    await signedIn(page);
    const requests = await serveDays(page);
    await page.goto("/app/w/1/");
    await expect(plants(page)).toBeVisible();
    await markDocument(page);
    expect(await readCopy(page)).toEqual({
        plants: 0,
        days: 0,
        flag: null,
        marks: [],
    });
    await openSwitch(page);
    await page.getByRole("button", { name: "Turn on" }).click();
    await expect
        .poll(() => heldTotals(page))
        .toEqual([
            ["forecast", "2026-11-11", 1.5],
            ["production", "2026-11-09", 1.2],
        ]);
    expect(requests.map(({ path }) => path)).toEqual([
        "/api/tenants/1/regions/11/plants/21/days/",
    ]);
    expect(await readCopy(page)).toMatchObject({
        version: 1,
        holder: { user: 1, session: "tag-a", active: true, retryAt: null },
        plants: 1,
        days: 2,
    });
    expect((await readHeld(page)).plants).toEqual([
        expect.objectContaining({
            ...plantKey,
            name: "East array",
            workspace: "Sunfield Energy",
            openedAt: null,
        }),
    ]);
    expect(await isMarked(page)).toBe(true);
    await page.getByText(title, { exact: true }).click();
    await openSwitch(page);
    await expect(
        page.getByText("Figures of 1 plant are kept on this device."),
    ).toBeVisible();
    await page.getByRole("button", { name: "Remove from this device" }).click();
    await expect(page.getByRole("button", { name: "Turn on" })).toBeVisible();
    expect(await readCopy(page)).toEqual({
        plants: 0,
        days: 0,
        flag: null,
        marks: [],
    });
});

test("Turn on in a browser whose local storage refuses the flag keeps no figures and leaves the switch off", async ({
    page,
}) => {
    await page.addInitScript(() => {
        const original = (name: "getItem" | "setItem") =>
            Object.getOwnPropertyDescriptor(Storage.prototype, name)!.value as (
                this: Storage,
                ...operands: string[]
            ) => unknown;
        const [getItem, setItem] = [original("getItem"), original("setItem")];
        const refused = (key: string) => {
            if (key === "pyracast.saved")
                throw new DOMException("Refused.", "SecurityError");
        };
        Storage.prototype.getItem = function (this: Storage, key: string) {
            refused(key);
            return getItem.call(this, key) as string | null;
        };
        Storage.prototype.setItem = function (
            this: Storage,
            key: string,
            value: string,
        ) {
            refused(key);
            setItem.call(this, key, value);
        };
    });
    await supplyRegistration(page);
    await signedIn(page);
    const requests = await serveDays(page);
    await page.goto("/app/w/1/");
    await expect(plants(page)).toBeVisible();
    await openSwitch(page);
    const ended = await watchAction(page, "Turn on");
    await page.getByRole("button", { name: "Turn on" }).click();
    await expect.poll(ended).toBe(true);
    await expect(page.getByRole("button", { name: "Turn on" })).toBeVisible();
    await expect(
        page.getByRole("button", { name: "Remove from this device" }),
    ).toHaveCount(0);
    expect(
        await page.evaluate(async () =>
            (await indexedDB.databases()).map(({ name }) => name),
        ),
    ).not.toContain("pyracast-saved");
    expect(requests).toEqual([]);
});

test("a Turn on that waited while another window changed the session changes nothing, and the new session turns on its own figures", async ({
    page,
    context,
}) => {
    const account: AccountState = { current: firstAccount };
    await onBlankPage(page);
    await page.evaluate(() =>
        window.localStorage.setItem("pyracast.saved", "1"),
    );
    const other = await context.newPage();
    for (const tab of [page, other]) {
        await supplyRegistration(tab);
        await signedIn(tab, account);
        await serveDays(tab);
        await tab.goto("/app/w/1/");
        await expect(plants(tab)).toBeVisible();
        await expect(tab.getByText(title, { exact: true })).toBeVisible();
    }
    await openSwitch(page);
    await markDocument(other);
    const [mine, theirs] = [await lockClient(page), await lockClient(other)];
    const lock = await lockTaker(context);
    await lock.take();
    await expect.poll(lock.granted).toBe(1);
    await pauseClock(page);
    account.current = { ...firstAccount, tag: "tag-b" };
    await focusWindow(other);
    await expect.poll(() => lock.waitingFor(theirs)).toBe(1);
    await lock.take();
    await expect.poll(lock.waiting).toBe(1);
    await page.getByRole("button", { name: "Turn on" }).click();
    await expect.poll(() => lock.waitingFor(mine)).toBe(1);
    await lock.release();
    await expect.poll(lock.granted).toBe(2);
    await expect
        .poll(() =>
            page.evaluate(() =>
                window.localStorage.getItem("pyracast.sessionTag"),
            ),
        )
        .toBe("tag-b");
    await lock.release();
    await page.clock.resume();
    await expect.poll(() => isMarked(other).catch(() => "loading")).toBe(false);
    await expect(plants(other)).toBeVisible();
    await openSwitch(other);
    await other.getByRole("button", { name: "Turn on" }).click();
    await expect.poll(async () => (await readCopy(other)).days).toBe(2);
    expect((await readCopy(other)).holder).toMatchObject({
        session: "tag-b",
        active: true,
    });
});

test("a Turn on that waits while another window signs out and finds no figures to remove keeps no figures and reads no days", async ({
    page,
    context,
}) => {
    const account: AccountState = { current: firstAccount };
    await onBlankPage(page);
    await page.evaluate(() =>
        window.localStorage.setItem("pyracast.saved", "1"),
    );
    const other = await context.newPage();
    const reads: DaysRequest[][] = [];
    for (const tab of [page, other]) {
        await supplyRegistration(tab);
        await signedIn(tab, account);
        reads.push(await serveDays(tab));
        await tab.goto("/app/w/1/");
        await expect(plants(tab)).toBeVisible();
        await expect(tab.getByText(title, { exact: true })).toBeVisible();
    }
    let reach!: () => void;
    const reached = new Promise<void>((resolve) => (reach = resolve));
    let answer!: () => void;
    const answered = new Promise<void>((resolve) => (answer = resolve));
    await other.route("**/api/auth/browser/v1/auth/session", async (route) => {
        if (route.request().method() !== "DELETE") return route.fallback();
        reach();
        await answered;
        await route.fallback();
    });
    await openSwitch(page);
    const [mine, theirs] = [await lockClient(page), await lockClient(other)];
    const lock = await lockTaker(context);
    await lock.take();
    await expect.poll(lock.granted).toBe(1);
    await pauseClock(page);
    await other.getByRole("button", { name: "Sign out", exact: true }).click();
    await expect.poll(() => lock.waitingFor(theirs)).toBe(1);
    await lock.take();
    await expect.poll(lock.waiting).toBe(1);
    await page.getByRole("button", { name: "Turn on" }).click();
    await expect.poll(() => lock.waitingFor(mine)).toBe(1);
    await lock.release();
    await reached;
    await expect.poll(lock.granted).toBe(2);
    await lock.release();
    await expect(page.getByRole("button", { name: "Turn on" })).toBeEnabled();
    expect((await readCopy(page)).holder).toBeUndefined();
    expect(reads.flat()).toEqual([]);
    answer();
    await page.clock.resume();
    await expect(
        other.getByRole("heading", { name: "Welcome back" }),
    ).toBeVisible();
});

test("a Turn on that commits after another window read the switch shows there once it commits", async ({
    page,
    context,
}) => {
    const write = await holdHolderWrite(page);
    const other = await context.newPage();
    for (const tab of [page, other]) {
        await supplyRegistration(tab);
        await signedIn(tab);
        await serveDays(tab);
        await tab.goto("/app/w/1/");
        await expect(plants(tab)).toBeVisible();
    }
    await openSwitch(other);
    await expect(other.getByRole("button", { name: "Turn on" })).toBeVisible();
    await openSwitch(page);
    const theirs = await lockClient(other);
    await write.arm();
    await pauseClock(page);
    await page.getByRole("button", { name: "Turn on" }).click();
    await expect.poll(write.held).toBe(true);
    await other.getByText(title, { exact: true }).click();
    await openSwitch(other);
    await expect.poll(() => waitingFor(other, theirs)).toBe(1);
    await page.clock.runFor(2_000);
    await expect(other.getByText(title, { exact: true })).toHaveCount(0);
    await write.release();
    await page.clock.resume();
    await openSwitch(other);
    await expect(
        other.getByRole("button", { name: "Remove from this device" }),
    ).toBeVisible();
});

for (const answer of ["Not now", "Turn on"])
    test(`${answer} in the offer of one installed window takes the offer away in another`, async ({
        page,
        context,
    }) => {
        const windows = [page, await context.newPage()];
        for (const tab of windows) {
            await tab.addInitScript(() =>
                Object.defineProperty(navigator, "standalone", { value: true }),
            );
            await supplyRegistration(tab);
            await signedIn(tab);
            await serveDays(tab);
            await tab.goto("/app/w/1/");
            await expect(
                tab.getByRole("region", { name: title }),
            ).toBeVisible();
        }
        await page
            .getByRole("region", { name: title })
            .getByRole("button", { name: answer })
            .click();
        for (const tab of windows)
            await expect(tab.getByRole("region", { name: title })).toHaveCount(
                0,
            );
        await expect
            .poll(async () => (await readCopy(page)).days)
            .toBe(answer === "Turn on" ? 2 : 0);
    });

test("the offer is put once, in an installed window only, and Not now keeps the device without a copy", async ({
    page,
    context,
}) => {
    await supplyRegistration(page);
    await signedIn(page);
    await serveDays(page);
    await page.goto("/app/w/1/");
    await expect(plants(page)).toBeVisible();
    await expect(page.getByText(title, { exact: true })).toBeVisible();
    await expect(page.getByRole("region", { name: title })).toHaveCount(0);
    const installed = await context.newPage();
    await installed.addInitScript(() =>
        Object.defineProperty(navigator, "standalone", { value: true }),
    );
    await supplyRegistration(installed);
    await signedIn(installed);
    await installed.goto("/app/w/1/");
    const offer = installed.getByRole("region", { name: title });
    await expect(offer).toBeVisible();
    await expect(offer.getByText(statement)).toBeVisible();
    await expect(installed.locator("body")).toBeFocused();
    await offer.getByRole("button", { name: "Not now" }).click();
    await expect(offer).toHaveCount(0);
    await installed.reload();
    await expect(plants(installed)).toBeVisible();
    await expect(installed.getByText(title, { exact: true })).toBeVisible();
    await expect(offer).toHaveCount(0);
    expect(await readCopy(installed)).toEqual({
        plants: 0,
        days: 0,
        flag: null,
        marks: [],
    });
});

test("Turn on in the offer keeps the workspace's figures and the offer is not put again", async ({
    page,
}) => {
    await page.addInitScript(() =>
        Object.defineProperty(navigator, "standalone", { value: true }),
    );
    await supplyRegistration(page);
    await signedIn(page);
    await serveDays(page);
    await page.goto("/app/w/1/");
    const offer = page.getByRole("region", { name: title });
    await offer.getByRole("button", { name: "Turn on" }).click();
    await expect(offer).toHaveCount(0);
    await expect.poll(async () => (await readCopy(page)).days).toBe(2);
    await page.reload();
    await expect(plants(page)).toBeVisible();
    await expect(offer).toHaveCount(0);
});

test("a run reads a plant once, not again before its hour has passed and not while the window is hidden", async ({
    page,
}) => {
    await heldCopy(page, [
        heldPlantOf({ refreshedAt: null, attemptedAt: null }),
    ]);
    await supplyRegistration(page);
    const visibility = await controlVisibility(page);
    await signedIn(page);
    const requests = await serveDays(page);
    await page.goto("/app/w/1/");
    await expect(plants(page)).toBeVisible();
    await expect.poll(() => requests.length).toBe(1);
    await expect.poll(async () => (await readCopy(page)).days).toBe(2);
    await page.clock.fastForward(5 * minute);
    await page.clock.fastForward(50 * minute);
    expect(requests).toHaveLength(1);
    await visibility.hide();
    await page.clock.fastForward(10 * minute);
    expect(requests).toHaveLength(1);
    await visibility.show();
    await expect.poll(() => requests.length).toBe(2);
    const [first, second] = requests.map(({ at }) => at);
    expect((second ?? 0) - (first ?? 0)).toBeGreaterThanOrEqual(60 * minute);
});

async function readAgainWhileBusy(
    page: Page,
    context: BrowserContext,
    account: AccountState,
    tag: string,
) {
    await heldCopy(page, [
        heldPlantOf({ refreshedAt: null, attemptedAt: null }),
    ]);
    await supplyRegistration(page);
    await signedIn(page, account);
    const requests = await serveDays(page);
    await page.goto("/app/w/1/");
    await expect(plants(page)).toBeVisible();
    await expect.poll(() => requests.length).toBe(1);
    await expect.poll(async () => (await readCopy(page)).days).toBe(2);
    await expect.poll(() => refreshIdle(page)).toBe(true);
    const mine = await lockClient(page);
    const lock = await lockTaker(context);
    await lock.take();
    await expect.poll(lock.granted).toBe(1);
    account.current = { ...firstAccount, tag };
    await focusWindow(page);
    await expect.poll(() => lock.waitingFor(mine)).toBe(1);
    await page.clock.runFor(2_000);
    await lock.release();
    return requests;
}

test("the same session read again while the store is busy past its limit keeps the screen, and the next due read is sent", async ({
    page,
    context,
}) => {
    const account: AccountState = { current: firstAccount };
    const requests = await readAgainWhileBusy(page, context, account, "tag-a");
    await page.clock.fastForward(60 * minute);
    await expect.poll(() => requests.length).toBe(2);
    await expect(plants(page)).toBeVisible();
    await expect(
        page.getByRole("heading", { name: "Pyracast could not be opened" }),
    ).toHaveCount(0);
});

test("a changed session answered while the store is busy past its limit covers the application", async ({
    page,
    context,
}) => {
    const account: AccountState = { current: firstAccount };
    await readAgainWhileBusy(page, context, account, "tag-b");
    await expect(
        page.getByRole("heading", { name: "Pyracast could not be opened" }),
    ).toBeVisible();
    await expect(plants(page)).toHaveCount(0);
});

test("a window hidden while its run judges the held plants marks no attempt and reads the plant once it is visible again", async ({
    page,
}) => {
    await heldCopy(page, [
        heldPlantOf({ refreshedAt: null, attemptedAt: null }),
    ]);
    await supplyRegistration(page);
    const visibility = await controlVisibility(page);
    await page.addInitScript(() => {
        const getAll = Object.getOwnPropertyDescriptor(
            IDBObjectStore.prototype,
            "getAll",
        )!.value as (
            this: IDBObjectStore,
            ...operands: unknown[]
        ) => IDBRequest;
        let judged = 0;
        IDBObjectStore.prototype.getAll = function (
            this: IDBObjectStore,
            ...operands: unknown[]
        ) {
            if (this.name === "plants" && this.transaction.mode === "readonly")
                judged += 1;
            if (judged === 2) {
                judged += 1;
                Object.assign(window, { pageHidden: true });
                document.dispatchEvent(new Event("visibilitychange"));
            }
            return getAll.apply(this, operands);
        } as IDBObjectStore["getAll"];
    });
    await signedIn(page);
    const requests = await serveDays(page);
    await page.goto("/app/w/1/");
    await expect(plants(page)).toBeVisible();
    await expect
        .poll(() => page.evaluate(() => document.visibilityState === "hidden"))
        .toBe(true);
    await expect.poll(() => refreshIdle(page)).toBe(true);
    expect(requests).toHaveLength(0);
    await visibility.show();
    await expect.poll(() => requests.length).toBe(1);
});

test("a refused days read stops every read for the time it names, at least five minutes", async ({
    page,
}) => {
    await heldCopy(page, [
        heldPlantOf({ refreshedAt: null, attemptedAt: null }),
    ]);
    await supplyRegistration(page);
    await signedIn(page);
    let refuse = true;
    const requests = await serveDays(page, () => (refuse ? 429 : daysAnswer()));
    await page.goto("/app/w/1/");
    await expect(plants(page)).toBeVisible();
    await expect.poll(() => requests.length).toBe(1);
    await expect
        .poll(async () => (await readCopy(page)).holder?.retryAt ?? 0)
        .toBeGreaterThanOrEqual((requests[0]?.at ?? Infinity) + 10 * minute);
    refuse = false;
    await page.clock.fastForward(5 * minute);
    await page.clock.fastForward(4 * minute);
    expect(requests).toHaveLength(1);
    await page.clock.fastForward(5 * minute);
    await expect.poll(() => requests.length).toBe(2);
    await expect.poll(async () => (await readCopy(page)).days).toBe(2);
});

test("a days read refused for the workspace removes its plants, one for a missing plant removes that plant, and the run goes on", async ({
    page,
}) => {
    const other = { tenant: 2, site: 13, plant: 23 };
    await heldCopy(page, [
        heldPlantOf({ refreshedAt: null, attemptedAt: null, openedAt: 2 }),
        heldPlantOf({
            ...other,
            workspace: "Second",
            refreshedAt: null,
            attemptedAt: null,
            openedAt: 1,
        }),
    ]);
    await supplyRegistration(page);
    await signedIn(page);
    await page.route("**/api/tenants/", (route) =>
        route.fulfill({
            json: paged([
                tenantRow({ id: 1, name: "Sunfield Energy", role: "admin" }),
                tenantRow({ id: 2, name: "Second", role: "viewer" }),
            ]),
        }),
    );
    const requests = await serveDays(page, (path) =>
        path.startsWith("/api/tenants/2/") ? 403 : 404,
    );
    await page.goto("/app/w/1/");
    await expect(plants(page)).toBeVisible();
    await expect.poll(() => requests.length).toBe(2);
    await expect.poll(async () => (await readCopy(page)).plants).toBe(0);
    expect(requests.map(({ path }) => path)).toEqual([
        "/api/tenants/1/regions/11/plants/21/days/",
        "/api/tenants/2/regions/13/plants/23/days/",
    ]);
});

test("a days read that fails ends the run and leaves the held days", async ({
    page,
}) => {
    const other = { tenant: 1, site: 12, plant: 22 };
    await seedCopy(page, {
        holder: holderOf({ confirmedAt: instant.getTime() }),
        plants: [
            heldPlantOf({ openedAt: 2, refreshedAt: 1, attemptedAt: 1 }),
            heldPlantOf({ ...other, openedAt: 1, refreshedAt: null }),
        ],
        days: [
            {
                ...plantKey,
                ...daysAnswer(0.5).forecast[0],
                kind: "forecast",
            },
        ],
    });
    await supplyRegistration(page);
    await signedIn(page);
    await page.route("**/api/tenants/1/sites/", (route) =>
        route.fulfill({
            json: paged([
                { ...region, plant, reminder: null },
                {
                    ...region,
                    id: 12,
                    plant: { ...plant, id: 22, region_id: 12 },
                    reminder: null,
                },
            ]),
        }),
    );
    const requests = await serveDays(page, () => 500);
    await page.goto("/app/w/1/");
    await expect(plants(page)).toBeVisible();
    await expect.poll(() => requests.length).toBe(1);
    await page.clock.fastForward(minute);
    expect(requests).toHaveLength(1);
    expect(await heldTotals(page)).toEqual([["forecast", "2026-11-11", 0.5]]);
    expect(await readCopy(page)).toMatchObject({ plants: 2 });
});

test("a plant opened a moment ago is read at once and is the most recently opened", async ({
    page,
}) => {
    const readAt = instant.getTime() - 20 * minute;
    await heldCopy(page, [
        heldPlantOf({ openedAt: 1, refreshedAt: readAt, attemptedAt: readAt }),
    ]);
    await supplyRegistration(page);
    await signedIn(page);
    const requests = await serveDays(page);
    await page.goto("/app/w/1/");
    await expect(plants(page)).toBeVisible();
    await page.clock.fastForward(minute);
    expect(requests).toHaveLength(0);
    await page
        .getByRole("button", { name: /East array|Antalya solar park/ })
        .click();
    await expect(plantHeading(page)).toBeVisible();
    await expect.poll(() => requests.length).toBe(1);
    const opened = (await readHeld(page)).plants.map(
        ({ openedAt }) => openedAt,
    );
    expect(opened).toHaveLength(1);
    expect(Number(opened[0])).toBeGreaterThanOrEqual(instant.getTime());
});

test("changed production on the plant's screen reads the plant once five minutes have passed since its last read, however often it changes", async ({
    page,
}) => {
    await heldCopy(page, [
        heldPlantOf({ refreshedAt: null, attemptedAt: null }),
    ]);
    await supplyRegistration(page);
    await signedIn(page);
    const requests = await serveDays(page);
    let total = 1;
    let productionReads = 0;
    await page.route(/\/plants\/21\/production\/\?/, (route) => {
        productionReads += 1;
        return route.fulfill({
            json: {
                timezone: region.timezone,
                available_start_date: null,
                available_end_date: null,
                start_date: "2026-11-09",
                end_date: "2026-11-09",
                days: [],
                intervals: [],
                total,
            },
        });
    });
    await page.goto("/app/w/1/plants/11/production/");
    await expect(plantHeading(page)).toBeVisible();
    await expect.poll(() => requests.length).toBe(1);
    await expect.poll(() => productionReads).toBe(1);
    for (const next of [2, 3]) {
        total = next;
        await page.clock.fastForward(2 * minute);
    }
    expect(requests).toHaveLength(1);
    await page.clock.fastForward(2 * minute);
    await expect.poll(() => requests.length).toBe(2);
    await page.clock.fastForward(5 * minute);
    expect(requests).toHaveLength(2);
});

test("a reason to read that comes while a run goes on starts another run once that run ends", async ({
    page,
}) => {
    await heldCopy(page, [
        heldPlantOf({ refreshedAt: null, attemptedAt: null }),
    ]);
    await supplyRegistration(page);
    await signedIn(page);
    let reach!: () => void;
    const reached = new Promise<void>((resolve) => (reach = resolve));
    let release!: () => void;
    const released = new Promise<void>((resolve) => (release = resolve));
    let sent = 0;
    await page.route(daysPath, async (route) => {
        sent += 1;
        if (sent > 1) return route.fulfill({ json: daysAnswer() });
        reach();
        await released;
        await route.fulfill({
            status: 500,
            json: errorBody("busy", "Refused."),
        });
    });
    await page.goto("/app/w/1/");
    await reached;
    await page.clock.fastForward(6 * minute);
    release();
    await expect.poll(() => sent).toBe(2);
    await expect.poll(async () => (await readCopy(page)).days).toBe(2);
});

const twoPlantSites = (page: Page) =>
    page.route("**/api/tenants/1/sites/", (route) =>
        route.fulfill({
            json: paged([
                { ...region, plant, reminder: null },
                {
                    ...region,
                    id: 12,
                    plant: { ...plant, id: 22, region_id: 12 },
                    reminder: null,
                },
            ]),
        }),
    );

test("two windows of one browser run one refresh at a time", async ({
    page,
    context,
}) => {
    await heldCopy(page, [
        heldPlantOf({ openedAt: 2, refreshedAt: null, attemptedAt: null }),
        heldPlantOf({
            site: 12,
            plant: 22,
            openedAt: 1,
            refreshedAt: null,
            attemptedAt: null,
        }),
    ]);
    let release!: () => void;
    const released = new Promise<void>((resolve) => (release = resolve));
    const sent: string[] = [];
    const second = await context.newPage();
    for (const [name, tab] of [
        ["first", page],
        ["second", second],
    ] as const) {
        await supplyRegistration(tab);
        await signedIn(tab);
        await twoPlantSites(tab);
        await tab.route(daysPath, async (route) => {
            sent.push(`${name} ${new URL(route.request().url()).pathname}`);
            await released;
            await route.fulfill({ json: daysAnswer() });
        });
    }
    await page.goto("/app/w/1/");
    await expect
        .poll(() => sent)
        .toEqual(["first /api/tenants/1/regions/11/plants/21/days/"]);
    await second.goto("/app/w/1/plants/11/forecast/");
    await expect(plantHeading(second)).toBeVisible();
    await expect
        .poll(
            async () =>
                (await readHeld(page)).plants.find(({ plant }) => plant === 21)
                    ?.openedAt,
        )
        .toBeGreaterThanOrEqual(instant.getTime());
    release();
    await expect
        .poll(() => sent)
        .toEqual([
            "first /api/tenants/1/regions/11/plants/21/days/",
            "first /api/tenants/1/regions/12/plants/22/days/",
        ]);
    await expect.poll(async () => (await readCopy(page)).days).toBe(4);
});

test("a window whose refresh meets another window's run reads the plant on its screen once that run ends", async ({
    page,
    context,
}) => {
    const readAt = instant.getTime() - 20 * minute;
    await heldCopy(page, [
        heldPlantOf({ openedAt: 1, refreshedAt: readAt, attemptedAt: readAt }),
        heldPlantOf({
            site: 12,
            plant: 22,
            openedAt: 2,
            refreshedAt: null,
            attemptedAt: null,
        }),
    ]);
    let release!: () => void;
    const released = new Promise<void>((resolve) => (release = resolve));
    const sent: string[] = [];
    const second = await context.newPage();
    for (const [name, tab] of [
        ["first", page],
        ["second", second],
    ] as const) {
        await supplyRegistration(tab);
        await signedIn(tab);
        await twoPlantSites(tab);
        await tab.route(daysPath, async (route) => {
            sent.push(`${name} ${new URL(route.request().url()).pathname}`);
            await released;
            await route.fulfill({ json: daysAnswer() });
        });
    }
    await page.goto("/app/w/1/");
    await expect
        .poll(() => sent)
        .toEqual(["first /api/tenants/1/regions/12/plants/22/days/"]);
    await second.goto("/app/w/1/plants/11/forecast/");
    await expect(plantHeading(second)).toBeVisible();
    await expect
        .poll(
            async () =>
                (await readHeld(page)).plants.find(({ plant }) => plant === 21)
                    ?.openedAt,
        )
        .toBeGreaterThanOrEqual(instant.getTime());
    release();
    await expect
        .poll(() => sent)
        .toEqual([
            "first /api/tenants/1/regions/12/plants/22/days/",
            "second /api/tenants/1/regions/11/plants/21/days/",
        ]);
});

test("a workspace the workspace list leaves out loses its plants and days at once, also when the site list of the workspace on screen then fails", async ({
    page,
}) => {
    const readAt = instant.getTime();
    const omitted = { tenant: 2, site: 13, plant: 23 };
    const dayOf = (key: object) => ({
        ...key,
        ...daysAnswer().forecast[0],
        kind: "forecast",
    });
    await seedCopy(page, {
        holder: holderOf({ confirmedAt: readAt }),
        plants: [
            heldPlantOf({ refreshedAt: readAt, attemptedAt: readAt }),
            heldPlantOf({
                ...omitted,
                workspace: "Second",
                refreshedAt: readAt,
                attemptedAt: readAt,
            }),
        ],
        days: [dayOf(plantKey), dayOf(omitted)],
    });
    await supplyRegistration(page);
    await signedIn(page);
    await page.route("**/api/tenants/1/sites/", (route) =>
        route.fulfill({ status: 500, json: errorBody("busy", "Refused.") }),
    );
    await page.goto("/app/w/1/");
    const tenants = async () => {
        const { plants, days } = await readHeld(page);
        return [
            plants.map(({ tenant }) => tenant),
            days.map(({ tenant }) => tenant),
        ];
    };
    await expect.poll(tenants).toEqual([[1], [1]]);
});

test("a signed-in invitation page sends no request for the kept figures over an hour, and the workspace does", async ({
    page,
}) => {
    await heldCopy(page, [
        heldPlantOf({ refreshedAt: null, attemptedAt: null }),
    ]);
    await supplyRegistration(page);
    await signedIn(page);
    await page.route("**/api/invitations/preview/", (route) =>
        route.fulfill({ json: invitationPreview }),
    );
    const tenants = countRequests(page, "/api/tenants/");
    const requests = await serveDays(page);
    await page.goto("/app/invitation/#token=invite-secret");
    await expect(
        page.getByRole("button", { name: "Accept invitation" }),
    ).toBeVisible();
    for (let step = 0; step < 12; step += 1)
        await page.clock.fastForward(5 * minute);
    expect(tenants()).toBe(0);
    expect(requests).toEqual([]);
    await page.goto("/app/w/1/");
    await expect.poll(() => requests.length).toBe(1);
});

test("a window hidden while the refresh reads the workspace list sends no site list", async ({
    page,
}) => {
    const readAt = instant.getTime();
    await heldCopy(page, [
        heldPlantOf({ openedAt: 1, refreshedAt: readAt, attemptedAt: readAt }),
    ]);
    await supplyRegistration(page);
    const visibility = await controlVisibility(page);
    await signedIn(page);
    await serveDays(page);
    let holding = false;
    let reach!: () => void;
    const reached = new Promise<void>((resolve) => (reach = resolve));
    let release!: () => void;
    const released = new Promise<void>((resolve) => (release = resolve));
    await page.route("**/api/tenants/", async (route) => {
        if (holding) {
            reach();
            await released;
        }
        await route.fallback();
    });
    const sites = countRequests(page, "/api/tenants/1/sites/");
    await page.goto("/app/w/1/plants/11/forecast/");
    await expect(plantHeading(page)).toBeVisible();
    await expect
        .poll(async () => (await readHeld(page)).plants[0]?.openedAt)
        .toBeGreaterThanOrEqual(readAt);
    await expect.poll(() => refreshIdle(page)).toBe(true);
    await page.clock.fastForward(31_000);
    holding = true;
    const listed = sites();
    await page.evaluate(() => window.dispatchEvent(new Event("online")));
    await reached;
    await visibility.hide();
    release();
    await expect.poll(() => refreshIdle(page)).toBe(true);
    expect(sites()).toBe(listed);
});

test("a site list the screen read less than thirty seconds ago is not read again for the copy", async ({
    page,
}) => {
    await supplyRegistration(page);
    await signedIn(page);
    await serveDays(page);
    const sites = countRequests(page, "/api/tenants/1/sites/");
    await page.goto("/app/w/1/");
    await expect(plants(page)).toBeVisible();
    expect(sites()).toBe(1);
    await openSwitch(page);
    await page.getByRole("button", { name: "Turn on" }).click();
    await expect.poll(async () => (await readCopy(page)).days).toBe(2);
    expect(sites()).toBe(1);
});

test("the store keeps eight plants: a ninth opened one removes one never opened before the least recently opened", async ({
    page,
}) => {
    await seedCopy(page, {
        holder: holderOf(),
        plants: [
            ...[1, 2, 3, 4, 5, 6, 7].map((site) =>
                heldPlantOf({ site, plant: site, openedAt: site }),
            ),
            heldPlantOf({ site: 8, plant: 8, openedAt: null }),
        ],
        days: [],
    });
    await loadStore(page, true);
    const open = (site: number) =>
        writeStore(
            page,
            "openPlant",
            owner,
            {
                tenant: 1,
                site,
                plant: site,
                name: `Plant ${site}`,
                workspace: "Sunfield Energy",
            },
            100 + site,
        );
    expect(await open(9)).toBe(true);
    const sites = async () =>
        (await readHeld(page)).plants.map(({ site }) => site);
    expect(await sites()).toEqual([1, 2, 3, 4, 5, 6, 7, 9]);
    expect(await open(10)).toBe(true);
    expect(await sites()).toEqual([2, 3, 4, 5, 6, 7, 9, 10]);
    expect(
        await writeStore(page, "settle", owner, {
            workspaces: [
                { tenant: 1, name: "Sunfield Energy" },
                { tenant: 2, name: "Second" },
            ],
            onScreen: {
                tenant: 2,
                plants: [{ tenant: 2, site: 11, plant: 11, name: "New" }],
            },
        }),
    ).toBe(true);
    expect(await sites()).toEqual([2, 3, 4, 5, 6, 7, 9, 10]);
    expect(
        await writeStore(page, "settle", owner, {
            workspaces: [{ tenant: 1, name: "Sunfield Energy" }],
            onScreen: {
                tenant: 1,
                plants: [2, 3].map((site) => ({
                    tenant: 1,
                    site,
                    plant: site,
                    name: `Plant ${site}`,
                })),
            },
        }),
    ).toBe(true);
    expect(await sites()).toEqual([2, 3]);
});

test("the store writes a plant's days only for the attempt it marked, under the holder's session, and replaces every day at once", async ({
    page,
}) => {
    await seedCopy(page, {
        holder: holderOf(),
        plants: [heldPlantOf()],
        days: [
            {
                ...plantKey,
                ...daysAnswer(0.5, "2026-11-01").forecast[0],
                kind: "forecast",
            },
        ],
    });
    await loadStore(page, true);
    const write = (as: object, attempt: number, total: number) =>
        writeStore(page, "writeDays", as, plantKey, attempt, daysAnswer(total));
    expect(await writeStore(page, "markAttempt", owner, plantKey, 10)).toBe(
        true,
    );
    expect(await writeStore(page, "markAttempt", owner, plantKey, 20)).toBe(
        true,
    );
    expect(await write(owner, 10, 1)).toBe(false);
    expect(await write({ ...owner, session: "tag-old" }, 20, 2)).toBe(false);
    expect(
        await writeStore(
            page,
            "writeDays",
            owner,
            { ...plantKey, plant: 99 },
            20,
            daysAnswer(3),
        ),
    ).toBe(false);
    expect(await heldTotals(page)).toEqual([["forecast", "2026-11-01", 0.5]]);
    expect(await write(owner, 20, 4)).toBe(true);
    expect(await heldTotals(page)).toEqual([
        ["forecast", "2026-11-11", 4],
        ["production", "2026-11-09", 1.2],
    ]);
});

for (const [path, sentences] of [
    [
        "/privacy/",
        [
            "Anyone who can open that browser on that device can read them without signing in.",
            "They are kept only where you turned this on.",
            "After 90 days without a sign-in on that device they are no longer shown",
        ],
    ],
    [
        "/tr/privacy/",
        [
            "O cihazda o tarayıcıyı açabilen herkes bu değerleri giriş yapmadan okuyabilir.",
            "Değerler yalnızca bu seçeneği açtığınız yerde saklanır.",
            "O cihazda 90 gün boyunca giriş yapılmazsa değerler artık gösterilmez",
        ],
    ],
] as const)
    test(`the privacy notice at ${path} says what the device keeps, who can read it and when it goes`, async ({
        page,
    }) => {
        await page.goto(path);
        for (const sentence of sentences)
            await expect(page.getByText(sentence)).toBeVisible();
    });
