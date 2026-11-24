import { expect, instant, test, type Page } from "./support/test";
import {
    firstAccount,
    mockApi,
    paged,
    tenantRow,
    type Account,
    type AccountState,
} from "./support/workspace";
import {
    isMarked,
    markDocument,
    secondAccount,
    serveSession,
} from "./support/session";
import { nativeProductionHistory } from "./support/native-production";
import {
    dayMs,
    heldDays,
    holderOf,
    holdCopy,
    loadStore,
    onBlankPage,
    readCopy,
    seedCopy,
} from "./support/saved-copy";

const idleLimit = 30 * 60_000;
const tick = 30_000;
const signOutPath = "**/api/auth/browser/v1/auth/session";

function plants(page: Page) {
    return page.getByRole("heading", { name: "Plants", exact: true });
}

function welcome(page: Page) {
    return page.getByRole("heading", { name: "Welcome back" });
}

async function signedIn(page: Page, account?: AccountState) {
    const state = await mockApi(page, account && { account });
    await serveSession(page, state);
    return state;
}

async function signOut(page: Page) {
    await page.getByRole("button", { name: "Sign out", exact: true }).click();
}

async function signInAs(page: Page, account: AccountState, next: Account) {
    await page.route("**/api/auth/browser/v1/auth/login", (route) => {
        account.current = next;
        return route.fulfill({
            json: { status: 200, meta: { is_authenticated: true } },
        });
    });
    await page.getByLabel("Email", { exact: true }).fill("first@example.test");
    await page
        .getByLabel("Password", { exact: true })
        .fill("Test-only-cobalt-79!");
    await page.locator("form").getByRole("button", { name: "Sign in" }).click();
    await expect(plants(page)).toBeVisible();
}

function abortSignOut(page: Page) {
    return page.route(signOutPath, (route) =>
        route.request().method() === "DELETE"
            ? route.abort()
            : route.fallback(),
    );
}

async function confirmedAt(page: Page) {
    return (await readCopy(page)).holder?.confirmedAt ?? 0;
}

// The fixture's clock starts at the shared instant, so a confirmation renewed by this run lies near it.
async function renewed(page: Page) {
    return (
        Math.abs((await confirmedAt(page)) - instant.getTime()) < 60 * 60_000
    );
}

async function expectHeldFigures(page: Page) {
    expect(await readCopy(page)).toMatchObject({ plants: 1, days: 2 });
}

test("without a copy a signed-in start, a sign-out and another user's sign-in create no database", async ({
    page,
}) => {
    const account = await signedIn(page);
    await page.goto("/app/w/1/");
    await expect(plants(page)).toBeVisible();
    await signOut(page);
    await expect(welcome(page)).toBeVisible();
    await signInAs(page, account, secondAccount);
    expect(await readCopy(page)).toEqual({
        plants: 0,
        days: 0,
        flag: null,
        mark: null,
    });
});

test("a flag without a database creates none at a signed-in start and a sign-out", async ({
    page,
}) => {
    await onBlankPage(page);
    await page.evaluate(() =>
        window.localStorage.setItem("pyracast.saved", "1"),
    );
    await signedIn(page);
    await page.goto("/app/w/1/");
    await expect(plants(page)).toBeVisible();
    await signOut(page);
    await expect(welcome(page)).toBeVisible();
    expect(await readCopy(page)).toEqual({
        plants: 0,
        days: 0,
        flag: "1",
        mark: null,
    });
});

test("a start as the holder with the stored tag keeps every figure and renews the confirmation and the language", async ({
    page,
}) => {
    await seedCopy(page, { holder: holderOf({ language: "tr" }) });
    await expectHeldFigures(page);
    await signedIn(page);
    await page.goto("/app/w/1/");
    await expect(plants(page)).toBeVisible();
    await expect.poll(() => renewed(page)).toBe(true);
    expect(await readCopy(page)).toMatchObject({
        holder: { session: "tag-a", active: true, language: "en" },
        plants: 1,
        days: 2,
        flag: "1",
    });
});

test("a start with a new tag activates a paused copy", async ({ page }) => {
    await seedCopy(page, {
        holder: holderOf({ session: "tag-old", active: false }),
        plants: [],
        days: [],
    });
    await signedIn(page);
    await page.goto("/app/w/1/");
    await expect(plants(page)).toBeVisible();
    await expect.poll(() => renewed(page)).toBe(true);
    expect((await readCopy(page)).holder).toMatchObject({
        session: "tag-a",
        active: true,
    });
});

test("a sign-out whose request is lost removes every figure, a second window of that session keeps the copy paused and the next sign-in activates it", async ({
    page,
    context,
}) => {
    await seedCopy(page);
    await expectHeldFigures(page);
    const account: AccountState = { current: firstAccount };
    await signedIn(page, account);
    await page.goto("/app/w/1/");
    await expect(plants(page)).toBeVisible();
    await expect.poll(() => renewed(page)).toBe(true);
    await abortSignOut(page);
    await signOut(page);
    await expect(page.getByRole("button", { name: "Try again" })).toBeVisible();
    expect(await readCopy(page)).toMatchObject({
        holder: { session: "tag-a", active: false },
        plants: 0,
        days: 0,
        mark: null,
    });
    const before = await confirmedAt(page);
    await page.clock.fastForward(60_000);
    const other = await context.newPage();
    await signedIn(other, account);
    await other.goto("/app/w/1/");
    await expect(plants(other)).toBeVisible();
    await expect
        .poll(() => confirmedAt(page))
        .toBeGreaterThanOrEqual(before + 60_000);
    expect((await readCopy(page)).holder).toMatchObject({
        session: "tag-a",
        active: false,
    });
    await page.unroute(signOutPath);
    await page.getByRole("button", { name: "Try again" }).click();
    await expect(welcome(page)).toBeVisible();
    await signInAs(page, account, { ...firstAccount, tag: "tag-c" });
    await expect
        .poll(async () => (await readCopy(page)).holder?.session)
        .toBe("tag-c");
    expect((await readCopy(page)).holder?.active).toBe(true);
});

test("a sign-out whose removal does not finish within two seconds sends its request and leaves the removal to the next start", async ({
    page,
}) => {
    await seedCopy(page);
    await signedIn(page);
    await page.goto("/app/w/1/");
    await expect(plants(page)).toBeVisible();
    await expect.poll(() => renewed(page)).toBe(true);
    const release = await holdCopy(page);
    const deletes: string[] = [];
    page.on("request", (request) => {
        if (request.method() === "DELETE") deletes.push(request.url());
    });
    const now = await page.evaluate(() => Date.now());
    await page.clock.pauseAt(now + 1_000);
    await signOut(page);
    await expect(
        page.getByRole("heading", { name: "Signing out" }),
    ).toBeVisible();
    expect(deletes).toEqual([]);
    await page.clock.runFor(2_000);
    await page.clock.resume();
    await expect(welcome(page)).toBeVisible();
    expect(deletes).toHaveLength(1);
    expect(
        await page.evaluate(() =>
            window.localStorage.getItem("pyracast.savedRemoval"),
        ),
    ).toBe("1");
    await release();
    await expect.poll(async () => (await readCopy(page)).mark).toBeNull();
    expect(await readCopy(page)).toMatchObject({
        holder: { active: false },
        plants: 0,
        days: 0,
    });
});

test("a removal marked at a sign-out is carried out at the next start", async ({
    page,
}) => {
    await seedCopy(page, { marked: true });
    expect(await readCopy(page)).toMatchObject({ plants: 1, mark: "1" });
    await signedIn(page);
    await page.goto("/app/w/1/");
    await expect(plants(page)).toBeVisible();
    await expect.poll(async () => (await readCopy(page)).mark).toBeNull();
    expect(await readCopy(page)).toMatchObject({
        holder: { session: "tag-a", active: false },
        plants: 0,
        days: 0,
        flag: "1",
    });
});

test("a start under another user leaves no database and no flag", async ({
    page,
}) => {
    await seedCopy(page);
    await expectHeldFigures(page);
    await signedIn(page, { current: secondAccount });
    await page.goto("/app/w/1/");
    await expect(plants(page)).toBeVisible();
    await expect
        .poll(() => readCopy(page))
        .toEqual({
            plants: 0,
            days: 0,
            flag: null,
            mark: null,
        });
});

for (const [when, confirmed] of [
    ["91 days before the clock", instant.getTime() - 91 * dayMs],
    ["two days ahead of the clock", instant.getTime() + 2 * dayMs],
] as const) {
    test(`a copy confirmed ${when} is emptied when its confirmation is renewed`, async ({
        page,
    }) => {
        await seedCopy(page, { holder: holderOf({ confirmedAt: confirmed }) });
        await expectHeldFigures(page);
        await signedIn(page);
        await page.goto("/app/w/1/");
        await expect(plants(page)).toBeVisible();
        await expect.poll(() => renewed(page)).toBe(true);
        expect(await readCopy(page)).toMatchObject({
            holder: { session: "tag-a", active: true },
            plants: 0,
            days: 0,
        });
    });
}

test("a database of a newer format is left as it is at a start and a sign-out", async ({
    page,
}) => {
    const holder = holderOf();
    await seedCopy(page, { holder, version: 2 });
    await signedIn(page);
    await page.goto("/app/w/1/");
    await expect(plants(page)).toBeVisible();
    await signOut(page);
    await expect(welcome(page)).toBeVisible();
    expect(await readCopy(page)).toEqual({
        version: 2,
        holder,
        plants: 1,
        days: heldDays.length,
        flag: "1",
        mark: "1",
    });
});

test("a browser whose database cannot be opened signs in and out as before", async ({
    page,
}) => {
    await onBlankPage(page);
    await page.evaluate(() =>
        window.localStorage.setItem("pyracast.saved", "1"),
    );
    await page.addInitScript(() => {
        IDBFactory.prototype.open = () => {
            throw new DOMException("Storage is blocked.", "SecurityError");
        };
    });
    const account = await signedIn(page);
    await page.goto("/app/w/1/");
    await expect(plants(page)).toBeVisible();
    await signOut(page);
    await expect(welcome(page)).toBeVisible();
    await signInAs(page, account, firstAccount);
    await signOut(page);
    await expect(welcome(page)).toBeVisible();
});

test("the store deletes a held copy at a start once the withdrawal constant is off", async ({
    page,
}) => {
    await seedCopy(page);
    const start = () =>
        page.evaluate(() =>
            (
                Reflect.get(window, "savedStore") as {
                    start: (marked: boolean) => Promise<string>;
                }
            ).start(false),
        );
    await loadStore(page, true);
    expect(await start()).toBe("kept");
    await expectHeldFigures(page);
    await loadStore(page, false);
    expect(await start()).toBe("deleted");
    expect((await readCopy(page)).version).toBeUndefined();
});

async function stagedCorrection(page: Page) {
    const account = await signedIn(page);
    await page.route("**/api/tenants/", (route) =>
        route.fulfill({
            json: paged([
                tenantRow({ id: 1, name: "Workspace", role: "viewer" }),
            ]),
        }),
    );
    await page.route("**/change-requests/?*", (route) =>
        route.fulfill({ json: paged([]) }),
    );
    await page.route("**/api/**/production/?*", (route) => {
        const date = new URL(route.request().url()).searchParams.get(
            "start_date",
        )!;
        return route.fulfill({
            json: nativeProductionHistory(date, "Europe/Istanbul", [
                {
                    starts_at: `${date}T08:00:00+03:00`,
                    ends_at: `${date}T08:30:00+03:00`,
                    effective: {
                        energy_mwh: 1,
                        quality: "valid",
                        source: "meter",
                        source_kind: "manual",
                        provisional: false,
                        aggregated: false,
                    },
                    can_edit: false,
                    can_propose: true,
                },
            ]),
        });
    });
    await page.goto("/app/");
    await page.getByRole("button", { name: /East array/ }).click();
    await page.getByRole("button", { name: "Production", exact: true }).click();
    await page
        .getByRole("button", { name: "Suggest correction for 08:00" })
        .click();
    const editor = page.locator(".production-row-editor");
    await editor.getByRole("textbox").first().fill("0,5");
    await editor
        .getByRole("button", { name: "Add to request", exact: true })
        .click();
    await expect(
        page.getByRole("region", { name: "Correction request", exact: true }),
    ).toBeVisible();
    return account;
}

test("a covered window with saved figures offers them and asks before it leaves a staged correction", async ({
    page,
}) => {
    await seedCopy(page);
    await stagedCorrection(page);
    await markDocument(page);
    await page.route("**/api/**", (route) => route.abort());
    const now = await page.evaluate(() => Date.now());
    await page.clock.pauseAt(now + idleLimit + tick);
    await expect(
        page.getByRole("heading", { name: "Could not confirm your session" }),
    ).toBeVisible();
    const showSaved = page.getByRole("button", {
        name: "Show saved figures",
        exact: true,
    });
    const questions: string[] = [];
    page.on("dialog", (dialog) => {
        questions.push(dialog.type());
        void dialog.dismiss();
    });
    await showSaved.click();
    await expect.poll(() => questions).toEqual(["beforeunload"]);
    expect(await isMarked(page)).toBe(true);
    await expect(showSaved).toBeVisible();
});
