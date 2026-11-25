import { expect, instant, test, type Page } from "./support/test";
import { errorBody } from "./support/errors";
import { mockApi, paged, region, tenantRow, plant } from "./support/workspace";
import { isMarked, markDocument, serveSession } from "./support/session";
import {
    controlVisibility,
    daysAnswer,
    heldPlantOf,
    holderOf,
    loadStore,
    plantKey,
    readCopy,
    readHeld,
    seedCopy,
    supplyRegistration,
    watchStoreChunk,
    writeStore,
} from "./support/saved-copy";

const minute = 60_000;
const title = "Keep figures on this device";
const statement =
    "Anyone who can open this browser on this device can read these figures without signing in, until they are removed or you sign out. Do not turn this on for a shared device.";
const daysPath = /\/api\/tenants\/\d+\/regions\/\d+\/plants\/\d+\/days\/$/;
const owner = { user: 1, session: "tag-a" };

async function signedIn(page: Page) {
    const account = await mockApi(page);
    await serveSession(page, account);
    return account;
}

function plants(page: Page) {
    return page.getByRole("heading", { name: "Plants", exact: true });
}

function plantHeading(page: Page) {
    return page.getByRole("heading", { name: "East array", level: 1 });
}

type DaysRequest = { path: string; at: number };

// Answers every days read with `answer` (a status, or the figures) and records when each arrived.
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

// A copy this session holds, with the plants given and none of their days.
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
    await page.route(/\/plants\/21\/production\/\?/, (route) =>
        route.fulfill({
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
        }),
    );
    await page.goto("/app/w/1/plants/11/production/");
    await expect(plantHeading(page)).toBeVisible();
    await expect.poll(() => requests.length).toBe(1);
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

test("two windows of one browser run one refresh at a time", async ({
    page,
    context,
}) => {
    await heldCopy(page, [
        heldPlantOf({ refreshedAt: null, attemptedAt: null }),
    ]);
    let release!: () => void;
    const released = new Promise<void>((resolve) => (release = resolve));
    const paths: string[] = [];
    const second = await context.newPage();
    for (const tab of [page, second]) {
        await supplyRegistration(tab);
        await signedIn(tab);
        await tab.route(daysPath, async (route) => {
            paths.push(new URL(route.request().url()).pathname);
            await released;
            await route.fulfill({ json: daysAnswer() });
        });
        await tab.goto("/app/w/1/");
        await expect(plants(tab)).toBeVisible();
    }
    await expect.poll(() => paths.length).toBe(1);
    for (const tab of [second, page])
        await tab.evaluate(() => window.dispatchEvent(new Event("online")));
    release();
    await expect.poll(async () => (await readCopy(page)).days).toBe(2);
    expect(paths).toHaveLength(1);
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
