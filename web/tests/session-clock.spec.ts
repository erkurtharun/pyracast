import type { Route } from "@playwright/test";
import { expect, test, type Page } from "./support/test";
import { errorBody } from "./support/errors";
import { mockApi } from "./support/workspace";
import {
    announceResentInvitation,
    expiredNotice,
    firstAccount,
    focusWindow,
    isMarked,
    liveRegion,
    markDocument,
    resentNotice,
    sessionAnswer,
    type Account,
} from "./support/session";

const start = new Date("2026-11-20T09:00:00Z");
const idleLimit = 30 * 60_000;
const tick = 30_000;
const capacity = "DC nameplate capacity (MW)";

function cover(page: Page) {
    return page.getByRole("heading", {
        name: "Could not confirm your session",
    });
}

type SessionServer = { account: Account; failing: boolean; reads: number };

async function signedInOnClock(page: Page, expiresAt: string | null = null) {
    const server: SessionServer = {
        account: firstAccount,
        failing: false,
        reads: 0,
    };
    await page.clock.install({ time: start });
    await mockApi(page);
    await page.route("**/api/session/", (route) => {
        server.reads++;
        return server.failing
            ? route.fulfill({
                  status: 503,
                  json: errorBody("busy", "The session could not be read."),
              })
            : route.fulfill({
                  json: sessionAnswer(server.account, expiresAt),
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
    await page.clock.runFor(idleLimit + 4 * tick);
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
    server.account = null;
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
        document.dispatchEvent(new Event("visibilitychange")),
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

async function serveLanguageChoice(page: Page) {
    await page.route("**/api/session/language/", (route) => {
        const language = route.request().postDataJSON().language as string;
        const session = sessionAnswer(firstAccount, null);
        return route.fulfill({
            json: {
                ...session,
                user: { ...session.user, language_preference: language },
                language,
            },
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
    await serveLanguageChoice(page);
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
