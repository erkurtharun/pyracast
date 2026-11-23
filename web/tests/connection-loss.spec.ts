import { expect, test, type Page } from "./support/test";
import { errorBody } from "./support/errors";
import {
    activeOutage,
    focusWindow,
    loseConnection,
    now,
    openPlant,
    serveOutages,
    serveProduction,
    signIn,
} from "./support/plant-screens";

test.use({ timezoneId: "Europe/Istanbul" });

const refreshFailed = (page: Page) =>
    page.getByRole("alert").filter({ hasText: "Could not refresh; showing" });

function outageSection(page: Page) {
    return page.locator("section", {
        has: page.getByRole("heading", { name: "Plant outages", exact: true }),
    });
}

const outage = (page: Page) =>
    page.getByRole("heading", { name: "Inverter disconnected" });

async function failFirstRead(page: Page, url: string) {
    let failed = false;
    await page.route(url, (route) => {
        if (route.request().method() !== "GET" || failed)
            return route.fallback();
        failed = true;
        return route.fulfill({
            status: 503,
            json: errorBody("busy", "Service is busy."),
        });
    });
}

async function retryFailedRead(page: Page) {
    const failure = page
        .getByRole("alert")
        .filter({ hasText: "Service is busy." });
    await expect(failure).toBeVisible();
    await failure
        .getByRole("button", { name: "Try again", exact: true })
        .click();
    await expect(failure).toHaveCount(0);
}

const savedProductionFailed = (page: Page) =>
    page
        .getByRole("alert")
        .filter({ hasText: "Could not refresh saved production." });

test("after the connection drops, the production screen and the shell say so at their own polls, and the online event reads again", async ({
    page,
}) => {
    await page.clock.install({ time: now });
    await signIn(page);
    await serveProduction(page, [{ can_edit: true }]);
    await openPlant(page, "Production");
    await expect(
        page.getByRole("button", { name: /^Edit 08:00/ }),
    ).toBeVisible();
    const reconnect = await loseConnection(page);
    const production = savedProductionFailed(page);
    await page.clock.runFor(30_000);
    await expect(production).toContainText("Could not reach the server.");
    await expect(refreshFailed(page)).toHaveCount(0);
    await page.clock.runFor(90_000);
    await expect(refreshFailed(page).first()).toContainText(
        "Could not reach the server.",
    );
    await reconnect();
    await expect(refreshFailed(page)).toHaveCount(0);
    await expect(production).toHaveCount(0);
});

test("an open production editor keeps the value typed before the connection dropped through the shell's failed poll and the reconnection", async ({
    page,
}) => {
    await page.clock.install({ time: now });
    await signIn(page);
    await serveProduction(page, [{ can_edit: true }]);
    await openPlant(page, "Production");
    await page.getByRole("button", { name: /^Edit 08:00/ }).click();
    const value = page
        .locator(".production-row-editor")
        .getByRole("textbox")
        .first();
    await value.fill("0,5");
    const reconnect = await loseConnection(page);
    await page.clock.runFor(120_000);
    await expect(refreshFailed(page).first()).toContainText(
        "Could not reach the server.",
    );
    await expect(value).toHaveValue("0,5");
    await reconnect();
    await expect(refreshFailed(page)).toHaveCount(0);
    await expect(savedProductionFailed(page)).toHaveCount(0);
    await expect(value).toHaveValue("0,5");
});

test("without a poll the outage list reports a dropped connection at focus and on Retry while an open outage form keeps what was typed", async ({
    page,
}) => {
    await page.clock.install({ time: now });
    await signIn(page);
    await serveOutages(page, [activeOutage]);
    let outageReads = 0;
    page.on("request", (request) => {
        if (request.url().endsWith("/plants/21/outages/")) outageReads++;
    });
    await openPlant(page, "Outages");
    await expect(outage(page)).toBeVisible();
    await page
        .getByRole("button", { name: "Report outage", exact: true })
        .click();
    await page.getByLabel("Incident title").fill("Grid fault");
    const reconnect = await loseConnection(page);
    const notice = outageSection(page)
        .getByRole("alert")
        .filter({ hasText: "Could not refresh" });
    await page.clock.runFor(5 * 60_000);
    await expect(notice).toHaveCount(0);
    await focusWindow(page);
    await expect(notice).toContainText("Could not reach the server.");
    const reads = outageReads;
    const failed = page.waitForEvent("requestfailed", (request) =>
        request.url().endsWith("/plants/21/outages/"),
    );
    await notice
        .getByRole("button", { name: "Try again", exact: true })
        .click();
    await failed;
    expect(outageReads).toBe(reads + 1);
    await expect(notice).toBeVisible();
    await expect(page.getByLabel("Incident title")).toHaveValue("Grid fault");
    await reconnect();
    await expect(notice).toHaveCount(0);
    await expect(page.getByLabel("Incident title")).toHaveValue("Grid fault");
});

test("plants hidden by a refusal stay hidden through a later lost read until a read succeeds", async ({
    page,
}) => {
    await page.clock.install({ time: now });
    await signIn(page);
    let outcome: "ok" | "denied" | "lost" = "ok";
    await page.route("**/api/tenants/1/sites/", (route) =>
        outcome === "denied"
            ? route.fulfill({
                  status: 403,
                  json: errorBody(
                      "membership_required",
                      "You are not a member of this workspace.",
                  ),
              })
            : outcome === "lost"
              ? route.abort("internetdisconnected")
              : route.fallback(),
    );
    await page.goto("/app/");
    const card = page.getByRole("button", { name: /East array/ });
    await expect(card).toBeVisible();
    outcome = "denied";
    await page.clock.runFor(30_000);
    const denial = page
        .getByRole("alert")
        .filter({ hasText: "You are not a member of this workspace." });
    await expect(denial).toBeVisible();
    await expect(card).toHaveCount(0);
    const retry = page
        .getByRole("region", { name: "Plants" })
        .getByRole("button", { name: "Try again", exact: true });
    outcome = "lost";
    await retry.click();
    const lost = page
        .getByRole("alert")
        .filter({ hasText: "Could not reach the server." });
    await expect(lost).toBeVisible();
    await expect(card).toHaveCount(0);
    outcome = "ok";
    await retry.click();
    await expect(card).toBeVisible();
    await expect(lost).toHaveCount(0);
});

test("a Retry while a focus read runs sends no second request, and that read's refusal removes the outages", async ({
    page,
}) => {
    await page.clock.install({ time: now });
    await signIn(page);
    await serveOutages(page, [activeOutage]);
    let answer: "rows" | "busy" | "held" = "rows";
    let reads = 0;
    const reached = Promise.withResolvers<void>();
    const release = Promise.withResolvers<void>();
    await page.route("**/plants/21/outages/", async (route) => {
        if (route.request().method() !== "GET") return route.fallback();
        reads++;
        if (answer === "busy")
            return route.fulfill({
                status: 503,
                json: errorBody("busy", "Service is busy."),
            });
        if (answer === "rows") return route.fallback();
        reached.resolve();
        await release.promise;
        return route.fulfill({
            status: 403,
            json: errorBody(
                "permission_denied",
                "You cannot read this plant's outages.",
            ),
        });
    });
    await openPlant(page, "Outages");
    await expect(outage(page)).toBeVisible();
    answer = "busy";
    await page.clock.runFor(5_000);
    await focusWindow(page);
    const notice = outageSection(page)
        .getByRole("alert")
        .filter({ hasText: "Could not refresh" });
    await expect(notice).toContainText("Service is busy.");
    answer = "held";
    await focusWindow(page);
    await reached.promise;
    const sent = reads;
    await notice
        .getByRole("button", { name: "Try again", exact: true })
        .click();
    release.resolve();
    await expect(
        outageSection(page)
            .getByRole("alert")
            .filter({ hasText: "You cannot read this plant's outages." }),
    ).toBeVisible();
    await expect(outage(page)).toHaveCount(0);
    await expect(notice).toHaveCount(0);
    expect(reads).toBe(sent);
});

async function watchPlantCards(page: Page) {
    await page.evaluate(() => {
        const shown = () =>
            [...document.querySelectorAll("button")].some((button) =>
                button.textContent?.includes("East array"),
            );
        const state = window as unknown as { plantCardShown: boolean };
        state.plantCardShown = shown();
        new MutationObserver(() => {
            if (shown()) state.plantCardShown = true;
        }).observe(document.body, {
            childList: true,
            subtree: true,
            characterData: true,
        });
    });
    return () =>
        page.evaluate(
            () =>
                (window as unknown as { plantCardShown: boolean })
                    .plantCardShown,
        );
}

test("plants refused in one language are not shown again from another language's earlier read", async ({
    page,
}) => {
    const session = await signIn(page, { language: "tr" });
    const reached = Promise.withResolvers<void>();
    const release = Promise.withResolvers<void>();
    let refused = false;
    await page.route("**/api/tenants/1/sites/", async (route) => {
        if (session.language() === "en") refused = true;
        else if (refused) {
            reached.resolve();
            await release.promise;
            return route.fulfill({
                status: 403,
                json: errorBody(
                    "membership_required",
                    "Bu çalışma alanının üyesi değilsiniz.",
                ),
            });
        } else return route.fallback();
        return route.fulfill({
            status: 403,
            json: errorBody(
                "membership_required",
                "You are not a member of this workspace.",
            ),
        });
    });
    await page.goto("/app/");
    await expect(
        page.getByRole("button", { name: /East array/ }),
    ).toBeVisible();
    await page.getByLabel("Dil", { exact: true }).selectOption("en");
    const denial = page
        .getByRole("alert")
        .filter({ hasText: "You are not a member of this workspace." });
    await expect(denial).toBeVisible();
    const cardShown = await watchPlantCards(page);
    expect(await cardShown()).toBe(false);
    await page.getByLabel("Language", { exact: true }).selectOption("tr");
    await expect(page.locator("html")).toHaveAttribute("lang", "tr");
    const retry = page
        .getByRole("region", { name: "Santraller" })
        .getByRole("button", { name: "Tekrar dene", exact: true });
    await expect(retry).toBeVisible();
    await expect(denial).toBeVisible();
    expect(await cardShown()).toBe(false);
    await reached.promise;
    release.resolve();
    await expect(
        page
            .getByRole("alert")
            .filter({ hasText: "Bu çalışma alanının üyesi değilsiniz." }),
    ).toBeVisible();
    await expect(denial).toHaveCount(0);
    await expect(retry).toBeVisible();
    expect(await cardShown()).toBe(false);
});

test("a language change while reads fail keeps the earlier outages with the time of their answer and an open outage form", async ({
    page,
}) => {
    await page.clock.setFixedTime(now);
    await signIn(page);
    await serveOutages(page, [activeOutage]);
    await openPlant(page, "Outages");
    await expect(outage(page)).toBeVisible();
    await page
        .getByRole("button", { name: "Report outage", exact: true })
        .click();
    await page.getByLabel("Incident title").fill("Grid fault");
    await page.route("**/plants/21/outages/", (route) =>
        route.request().method() === "GET"
            ? route.abort("internetdisconnected")
            : route.fallback(),
    );
    await page.clock.setFixedTime(new Date(now.getTime() + 7 * 60_000));
    await page.getByLabel("Language", { exact: true }).selectOption("tr");
    await expect(
        page.getByRole("heading", { name: "Santral kesintileri", exact: true }),
    ).toBeVisible();
    await expect(
        page
            .getByRole("alert")
            .filter({ hasText: "Yenilenemedi; 15:00 itibarıyla" }),
    ).toContainText("Sunucuya ulaşılamadı.");
    await expect(outage(page)).toBeVisible();
    await expect(page.locator('input[name="title"]')).toHaveValue("Grid fault");
});

test("a first outage list read that failed offers Retry, which reads it again", async ({
    page,
}) => {
    await page.clock.setFixedTime(now);
    await signIn(page);
    await serveOutages(page, [activeOutage]);
    await failFirstRead(page, "**/plants/21/outages/");
    await openPlant(page, "Outages");
    await retryFailedRead(page);
    await expect(outage(page)).toBeVisible();
});

test("a first plant list read that failed offers Retry, which opens the plant", async ({
    page,
}) => {
    await page.clock.setFixedTime(now);
    await signIn(page);
    await failFirstRead(page, "**/api/tenants/1/sites/");
    await page.goto("/app/w/1/plants/11/forecast/");
    await retryFailedRead(page);
    await expect(
        page.getByRole("heading", { name: "East array", level: 1 }),
    ).toBeVisible();
});

test("a first inbox read that failed offers Retry, which reads it again", async ({
    page,
}) => {
    await page.clock.setFixedTime(now);
    await signIn(page);
    await failFirstRead(page, "**/change-requests/?*");
    await page.goto("/app/");
    await page.getByRole("button", { name: "Inbox", exact: true }).click();
    await retryFailedRead(page);
    await expect(page.getByText("No submissions here.")).toBeVisible();
});

const pendingInboxRead = (url: string) =>
    url.includes("/change-requests/?") && url.includes("scope=pending");

test("Refresh list while the first inbox read runs sends no second request, and that read's refusal is shown", async ({
    page,
}) => {
    await page.clock.setFixedTime(now);
    await signIn(page);
    await page.goto("/app/");
    await expect(
        page.getByRole("button", { name: /East array/ }),
    ).toBeVisible();
    let reads = 0;
    const reached = Promise.withResolvers<void>();
    const release = Promise.withResolvers<void>();
    await page.route("**/change-requests/?*", async (route) => {
        if (!pendingInboxRead(route.request().url())) return route.fallback();
        reads++;
        if (reads > 1) return route.abort("internetdisconnected");
        reached.resolve();
        await release.promise;
        return route.fulfill({
            status: 403,
            json: errorBody(
                "permission_denied",
                "You cannot read this workspace's submissions.",
            ),
        });
    });
    await page.getByRole("button", { name: "Inbox", exact: true }).click();
    await reached.promise;
    await page
        .getByRole("button", { name: "Refresh list", exact: true })
        .click();
    release.resolve();
    await expect(
        page.getByRole("alert").filter({
            hasText: "You cannot read this workspace's submissions.",
        }),
    ).toBeVisible();
    await expect(page.getByText("Could not reach the server.")).toHaveCount(0);
    expect(reads).toBe(1);
});

test("Refresh list reads an idle inbox list again", async ({ page }) => {
    await page.clock.setFixedTime(now);
    await signIn(page);
    await page.goto("/app/");
    await page.getByRole("button", { name: "Inbox", exact: true }).click();
    await expect(page.getByText("No submissions here.")).toBeVisible();
    const read = page.waitForRequest((request) =>
        pendingInboxRead(request.url()),
    );
    await page
        .getByRole("button", { name: "Refresh list", exact: true })
        .click();
    await read;
});

test("a first automatic calibration read that failed offers Retry, which reads it again", async ({
    page,
}) => {
    await page.clock.setFixedTime(now);
    await signIn(page);
    await failFirstRead(page, "**/calibration/automation/");
    await openPlant(page, "Calibration");
    await retryFailedRead(page);
    await expect(
        page.getByRole("checkbox", { name: "Automatic calibration enabled" }),
    ).toBeVisible();
});

test("a first production read that failed offers Retry, which reads the day again", async ({
    page,
}) => {
    await page.clock.setFixedTime(now);
    await signIn(page);
    await serveProduction(page, [{ can_edit: true }]);
    await failFirstRead(page, "**/api/**/production/?*");
    await openPlant(page, "Production");
    await retryFailedRead(page);
    await expect(
        page.getByRole("button", { name: /^Edit 08:00/ }),
    ).toBeVisible();
});
