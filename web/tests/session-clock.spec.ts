import type { Route } from "@playwright/test";
import { expect, test, type Page } from "./support/test";
import { errorBody } from "./support/errors";
import { accountWith, mockApi, type AccountState } from "./support/workspace";
import {
    announceResentInvitation,
    expiredNotice,
    focusWindow,
    isMarked,
    liveRegion,
    markDocument,
    outstandingAnswers,
    resentNotice,
    sessionAnswer,
} from "./support/session";

const start = new Date("2026-11-20T09:00:00Z");
const idleLimit = 30 * 60_000;
const tick = 30_000;
const listPoll = 2 * 60_000;
const capacity = "DC nameplate capacity (MW)";

function cover(page: Page) {
    return page.getByRole("heading", {
        name: "Could not confirm your session",
    });
}

type SessionServer = {
    account: AccountState;
    failing: boolean;
    reads: number;
};

async function signedInOnClock(page: Page, expiresAt: string | null = null) {
    await page.clock.install({ time: start });
    const server: SessionServer = {
        account: await mockApi(page),
        failing: false,
        reads: 0,
    };
    await page.route("**/api/session/", (route) => {
        server.reads++;
        return server.failing
            ? route.fulfill({
                  status: 503,
                  json: errorBody("busy", "The session could not be read."),
              })
            : route.fulfill({
                  json: sessionAnswer(server.account.current, expiresAt),
              });
    });
    return server;
}

async function openSettingsForm(page: Page) {
    await page.goto("/app/");
    await page.getByRole("button", { name: /East array/ }).click();
    await page.getByRole("button", { name: "Settings", exact: true }).click();
    await page.getByLabel(capacity).fill("6.25");
}

const abort = (route: Route) => route.abort();

test("a window without an answer for the idle limit covers its screens and keeps what was typed", async ({
    page,
}) => {
    await signedInOnClock(page);
    await openSettingsForm(page);
    await markDocument(page);
    await page.route("**/api/**", abort);
    await page.clock.runFor(idleLimit);
    await page.clock.runFor(tick);
    await expect(cover(page)).toBeVisible();
    await expect(page.getByLabel(capacity)).toBeHidden();
    await page.unroute("**/api/**", abort);
    await page.getByRole("button", { name: "Try again" }).click();
    await expect(cover(page)).toHaveCount(0);
    await expect(page.getByLabel(capacity)).toHaveValue("6.25");
    expect(await isMarked(page)).toBe(true);
});

test("a cover lifts at the next answer of the session read", async ({
    page,
}) => {
    const server = await signedInOnClock(page);
    await openSettingsForm(page);
    await page.route("**/api/**", abort);
    await page.clock.runFor(idleLimit + tick);
    await expect(cover(page)).toBeVisible();
    await page.unroute("**/api/**", abort);
    const reads = server.reads;
    await page.clock.runFor(tick);
    await expect(cover(page)).toHaveCount(0);
    expect(server.reads).toBeGreaterThan(reads);
    await expect(page.getByLabel(capacity)).toHaveValue("6.25");
});

test("a window whose workspace list answers reads no session and stays uncovered past the idle limit", async ({
    page,
}) => {
    const server = await signedInOnClock(page);
    await page.goto("/app/w/1/");
    await expect(
        page.getByRole("heading", { name: "Plants", exact: true }),
    ).toBeVisible();
    const reads = server.reads;
    server.failing = true;
    for (let passed = 0; passed < idleLimit + 4 * tick; passed += listPoll) {
        const listed = page.waitForResponse("**/api/tenants/");
        await page.clock.runFor(listPoll);
        expect((await listed).ok()).toBe(true);
    }
    expect(server.reads).toBe(reads);
    await expect(cover(page)).toHaveCount(0);
    await expect(
        page.getByRole("heading", { name: "Plants", exact: true }),
    ).toBeVisible();
});

test("a session read that fails before the idle limit covers nothing", async ({
    page,
}) => {
    const server = await signedInOnClock(page);
    await openSettingsForm(page);
    server.failing = true;
    const failed = page.waitForResponse("**/api/session/");
    await focusWindow(page);
    expect((await failed).status()).toBe(503);
    await page.clock.runFor(tick);
    await expect(cover(page)).toHaveCount(0);
    await expect(page.getByLabel(capacity)).toBeVisible();
});

test("a covered window whose session read finds no user becomes a fresh document", async ({
    page,
}) => {
    const server = await signedInOnClock(page);
    await openSettingsForm(page);
    await markDocument(page);
    await page.route("**/api/**", abort);
    await page.clock.runFor(idleLimit + tick);
    await expect(cover(page)).toBeVisible();
    server.account.current = null;
    await page.unroute("**/api/**", abort);
    await page.getByRole("button", { name: "Try again" }).click();
    await expect(page.getByText(expiredNotice)).toBeVisible();
    expect(await isMarked(page)).toBe(false);
});

test("the ending-soon warning appears at the next tick once the clock passed its mark", async ({
    page,
}) => {
    const expiresAt = new Date(start.getTime() + 10 * 60_000);
    await signedInOnClock(page, expiresAt.toISOString());
    await page.goto("/app/w/1/");
    await expect(
        page.getByRole("heading", { name: "Plants", exact: true }),
    ).toBeVisible();
    const warning = page.getByText(/^Your session ends at /);
    await expect(warning).toHaveCount(0);
    await page.clock.setSystemTime(new Date(start.getTime() + 6 * 60_000));
    await page.clock.runFor(tick);
    await expect(warning).toBeVisible();
});

test("a cover lifts at the answer of Retry after the clock was moved back", async ({
    page,
}) => {
    await signedInOnClock(page);
    await openSettingsForm(page);
    await page.route("**/api/**", abort);
    await page.clock.runFor(idleLimit + tick);
    await expect(cover(page)).toBeVisible();
    await page.unroute("**/api/**", abort);
    await page.clock.setSystemTime(new Date(start.getTime() - 60 * 60_000));
    await page.getByRole("button", { name: "Try again" }).click();
    await expect(cover(page)).toHaveCount(0);
    await expect(page.getByLabel(capacity)).toHaveValue("6.25");
});

function showWindow(page: Page) {
    return page.evaluate(() =>
        document.dispatchEvent(
            new Event("visibilitychange", { bubbles: true }),
        ),
    );
}

function sessionReadFailed(page: Page) {
    return page.waitForEvent("requestfailed", (request) =>
        request.url().endsWith("/api/session/"),
    );
}

test("a failed Retry after the clock was moved back keeps the cover until an answer", async ({
    page,
}) => {
    await signedInOnClock(page);
    await openSettingsForm(page);
    await page.route("**/api/**", abort);
    await page.clock.runFor(idleLimit + tick);
    await expect(cover(page)).toBeVisible();
    await page.clock.setSystemTime(new Date(start.getTime() - 60 * 60_000));
    const failed = sessionReadFailed(page);
    await page.getByRole("button", { name: "Try again" }).click();
    await failed;
    await page.clock.runFor(tick);
    await expect(cover(page)).toBeVisible();
    await expect(page.getByLabel(capacity)).toBeHidden();
    await page.unroute("**/api/**", abort);
    await page.getByRole("button", { name: "Try again" }).click();
    await expect(cover(page)).toHaveCount(0);
    await expect(page.getByLabel(capacity)).toHaveValue("6.25");
});

test("a Retry sent before the stored confirmation and answered after it, with the clock one second back, lifts the cover", async ({
    page,
}) => {
    await signedInOnClock(page);
    await openWorkspace(page);
    const settled = await outstandingAnswers(page);
    const confirmedAt = start.getTime() + 60_000;
    await page.clock.setFixedTime(confirmedAt);
    const confirmed = settled(0);
    await focusWindow(page);
    await confirmed;
    await page.clock.setFixedTime(confirmedAt - 1_000);
    await failSessionReads(page);
    await expect(cover(page)).toBeVisible();
    await page.unroute("**/api/**", abort);
    const retry = await holdNext(page, "/api/session/");
    await page.clock.setFixedTime(confirmedAt - 500);
    await page.getByRole("button", { name: "Try again" }).click();
    await retry.reached;
    await page.clock.setFixedTime(confirmedAt + 500);
    const answered = settled(0);
    retry.release();
    await answered;
    await expect(cover(page)).toHaveCount(0);
    await expect(
        page.getByRole("heading", { name: "Plants", exact: true }),
    ).toBeVisible();
});

test("a workspace-list answer after the clock was moved back lifts the cover", async ({
    page,
}) => {
    await signedInOnClock(page);
    await openSettingsForm(page);
    await page.route("**/api/**", abort);
    await page.clock.runFor(idleLimit + tick);
    await expect(cover(page)).toBeVisible();
    let answerReads!: () => void;
    const readsAnswered = new Promise<void>(
        (resolve) => (answerReads = resolve),
    );
    await page.route("**/api/session/", async (route) => {
        await readsAnswered;
        await route.fallback();
    });
    await page.unroute("**/api/**", abort);
    await page.clock.setSystemTime(new Date(start.getTime() - 60 * 60_000));
    const listed = page.waitForResponse("**/api/tenants/");
    await showWindow(page);
    expect((await listed).ok()).toBe(true);
    await page.clock.runFor(tick);
    await expect(cover(page)).toHaveCount(0);
    await expect(page.getByLabel(capacity)).toHaveValue("6.25");
    const read = page.waitForResponse("**/api/session/");
    answerReads();
    await read;
});

async function serveLanguageChoice(page: Page, account: AccountState) {
    await page.route("**/api/session/language/", (route) => {
        const language = route.request().postDataJSON().language as string;
        account.current = accountWith({ language_preference: language });
        return route.fulfill({
            json: { ...sessionAnswer(account.current, null), language },
        });
    });
}

async function chooseLanguage(page: Page, label: string, language: string) {
    const saved = page.waitForResponse("**/api/session/language/");
    await page.getByLabel(label, { exact: true }).selectOption(language);
    expect((await saved).ok()).toBe(true);
}

test("a list cached in another language before the clock was moved back leaves a fresh session usable", async ({
    page,
}) => {
    const server = await signedInOnClock(page);
    await serveLanguageChoice(page, server.account);
    const noon = new Date("2026-11-20T12:00:00Z");
    await page.clock.setSystemTime(noon);
    await openSettingsForm(page);
    await page.clock.setSystemTime(new Date(noon.getTime() - 60 * 60_000));
    const listed = page.waitForResponse("**/api/tenants/");
    await chooseLanguage(page, "Language", "tr");
    expect((await listed).ok()).toBe(true);
    await page.clock.setSystemTime(new Date(noon.getTime() - 59 * 60_000));
    await chooseLanguage(page, "Dil", "en");
    await expect(page.getByLabel(capacity)).toBeVisible();
    server.failing = true;
    const failed = page.waitForResponse("**/api/session/");
    await focusWindow(page);
    expect((await failed).status()).toBe(503);
    await page.clock.runFor(tick);
    await expect(cover(page)).toHaveCount(0);
    await expect(page.getByLabel(capacity)).toHaveValue("6.25");
});

test("a cached workspace list shown again keeps a set cover", async ({
    page,
}) => {
    await signedInOnClock(page);
    await openSettingsForm(page);
    await page.getByRole("button", { name: "Account", exact: true }).click();
    await expect(page).toHaveURL("/app/account/");
    await page.route("**/api/**", abort);
    await page.clock.runFor(idleLimit + tick);
    await expect(cover(page)).toBeVisible();
    await page.goBack();
    await expect(page).toHaveURL(/\/app\/w\/1\//);
    await page.clock.runFor(tick);
    await expect(cover(page)).toBeVisible();
});

test("a clock moved back before a session read fails still covers the screens", async ({
    page,
}) => {
    await signedInOnClock(page);
    await openSettingsForm(page);
    await page.clock.setSystemTime(new Date(start.getTime() - 60 * 60_000));
    await page.route("**/api/**", abort);
    const failed = sessionReadFailed(page);
    await page.clock.runFor(tick);
    await failed;
    await expect(cover(page)).toBeVisible();
    await expect(page.getByLabel(capacity)).toBeHidden();
});

test("a clock moved back does not postpone the idle session read", async ({
    page,
}) => {
    await signedInOnClock(page);
    await openSettingsForm(page);
    await page.clock.setSystemTime(new Date(start.getTime() - 60 * 60_000));
    const read = page.waitForRequest("**/api/session/");
    await page.clock.runFor(tick);
    await read;
});

test("a cover hides what the screens last announced", async ({ page }) => {
    await signedInOnClock(page);
    await page.goto("/app/w/1/");
    await announceResentInvitation(page);
    await page.route("**/api/**", abort);
    await page.clock.runFor(idleLimit + tick);
    await expect(cover(page)).toBeVisible();
    await expect(liveRegion(page)).toBeHidden();
    await expect(page.getByText(resentNotice)).toBeHidden();
});

type Held = { reached: Promise<void>; release: () => void };

async function holdNext(page: Page, path: string): Promise<Held> {
    let reach!: () => void;
    const reached = new Promise<void>((resolve) => (reach = resolve));
    let release!: () => void;
    const released = new Promise<void>((resolve) => (release = resolve));
    let held = false;
    await page.route(`**${path}`, async (route) => {
        if (held) return route.fallback();
        held = true;
        reach();
        await released;
        return route.fallback();
    });
    return { reached, release };
}

async function openWorkspace(page: Page) {
    await page.goto("/app/w/1/");
    await expect(
        page.getByRole("heading", { name: "Plants", exact: true }),
    ).toBeVisible();
}

async function failSessionReads(page: Page) {
    await page.route("**/api/**", abort);
    const failed = sessionReadFailed(page);
    await focusWindow(page);
    await failed;
}

type Settled = (count: number) => Promise<void>;

// Query Core refetches stale queries on a visibilitychange at window, where no session read listens.
function refetchStale(page: Page) {
    return page.evaluate(() =>
        window.dispatchEvent(new Event("visibilitychange")),
    );
}

async function heldListRead(page: Page, settled: Settled) {
    const list = await holdNext(page, "/api/tenants/");
    await page.clock.setSystemTime(start.getTime() + tick);
    const others = settled(1);
    await refetchStale(page);
    await list.reached;
    await others;
    return list;
}

test("a workspace-list answer released after the idle limit confirms nothing before a failed session read", async ({
    page,
}) => {
    await signedInOnClock(page);
    await openWorkspace(page);
    const settled = await outstandingAnswers(page);
    const list = await heldListRead(page, settled);
    await page.clock.setSystemTime(start.getTime() + idleLimit + 2 * tick);
    list.release();
    await settled(0);
    await failSessionReads(page);
    await page.clock.runFor(tick);
    await expect(cover(page)).toBeVisible();
    await page.clock.runFor(tick);
    await expect(cover(page)).toBeVisible();
});

test("a workspace-list answer released after the idle limit keeps the cover a failed session read set", async ({
    page,
}) => {
    await signedInOnClock(page);
    await openWorkspace(page);
    const settled = await outstandingAnswers(page);
    const list = await heldListRead(page, settled);
    await page.clock.setSystemTime(start.getTime() + idleLimit + 2 * tick);
    await failSessionReads(page);
    await expect(cover(page)).toBeVisible();
    list.release();
    await settled(0);
    await page.clock.runFor(tick);
    await expect(cover(page)).toBeVisible();
});

test("a session answer released after the idle limit confirms nothing", async ({
    page,
}) => {
    await signedInOnClock(page);
    await openWorkspace(page);
    const settled = await outstandingAnswers(page);
    const read = await holdNext(page, "/api/session/");
    await focusWindow(page);
    await read.reached;
    await page.clock.setSystemTime(start.getTime() + idleLimit + tick);
    read.release();
    await settled(0);
    await failSessionReads(page);
    await page.clock.runFor(tick);
    await expect(cover(page)).toBeVisible();
    await page.clock.runFor(tick);
    await expect(cover(page)).toBeVisible();
});

test("a workspace-list answer sent after the cover was set lifts it", async ({
    page,
}) => {
    await signedInOnClock(page);
    await openSettingsForm(page);
    await page.route("**/api/**", abort);
    await page.clock.runFor(idleLimit + tick);
    await expect(cover(page)).toBeVisible();
    const reads = await holdNext(page, "/api/session/");
    await page.unroute("**/api/**", abort);
    const listed = page.waitForResponse("**/api/tenants/");
    await refetchStale(page);
    expect((await listed).ok()).toBe(true);
    await expect(cover(page)).toHaveCount(0);
    await expect(page.getByLabel(capacity)).toHaveValue("6.25");
    reads.release();
});

test("an older workspace-list answer after a newer session answer moves the confirmation nowhere", async ({
    page,
}) => {
    await signedInOnClock(page);
    await openWorkspace(page);
    const settled = await outstandingAnswers(page);
    const list = await heldListRead(page, settled);
    await page.clock.setSystemTime(start.getTime() + 20 * 60_000);
    const answered = settled(1);
    await focusWindow(page);
    await answered;
    list.release();
    await settled(0);
    await page.clock.setSystemTime(start.getTime() + idleLimit + 2 * tick);
    await failSessionReads(page);
    await page.clock.runFor(tick);
    await expect(cover(page)).toHaveCount(0);
    await expect(
        page.getByRole("heading", { name: "Plants", exact: true }),
    ).toBeVisible();
});

test("a workspace-list answer after a failed session read covers nothing", async ({
    page,
}) => {
    const server = await signedInOnClock(page);
    await page.goto("/app/w/1/");
    await expect(
        page.getByRole("heading", { name: "Plants", exact: true }),
    ).toBeVisible();
    server.failing = true;
    const failed = page.waitForResponse("**/api/session/");
    await focusWindow(page);
    expect((await failed).status()).toBe(503);
    const listed = page.waitForResponse("**/api/tenants/");
    await page.clock.runFor(4 * tick);
    await listed;
    await page.clock.runFor(tick);
    await expect(cover(page)).toHaveCount(0);
    await expect(
        page.getByRole("heading", { name: "Plants", exact: true }),
    ).toBeVisible();
});
