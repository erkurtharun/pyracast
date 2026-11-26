import { expect, test, type Page } from "./support/test";
import { mockApi } from "./support/workspace";
import { serveSession } from "./support/session";
import {
    daysAnswer,
    readCopy,
    refuseClears,
    seedCopy,
    supplyRegistration,
} from "./support/saved-copy";
import { savedFiguresOff } from "./support/worker-state";

test.skip(
    !savedFiguresOff,
    "Runs against a build whose saved figures are withdrawn.",
);

const setting = "Open figures without a connection";
const minute = 60_000;
const daysPath = /\/api\/tenants\/\d+\/regions\/\d+\/plants\/\d+\/days\/$/;

async function watchRefreshCode(page: Page) {
    let requested = false;
    await page.route("**/_next/static/chunks/**", async (route) => {
        const response = await route.fetch();
        const body = await response.text();
        if (body.includes("pyracast-saved-refresh")) requested = true;
        await route.fulfill({ response, body });
    });
    return () => requested;
}

test("a withdrawn build whose kept figures could not be removed shows neither the setting nor the figures and reads nothing for them", async ({
    page,
}) => {
    await seedCopy(page);
    await refuseClears(page);
    await page.addInitScript(() =>
        Object.defineProperty(navigator, "standalone", { value: true }),
    );
    await supplyRegistration(page);
    const account = await mockApi(page);
    await serveSession(page, account);
    const refreshCode = await watchRefreshCode(page);
    const days: string[] = [];
    await page.route(daysPath, (route) => {
        days.push(new URL(route.request().url()).pathname);
        return route.fulfill({ json: daysAnswer() });
    });
    await page.goto("/app/w/1/");
    await expect(
        page.getByRole("heading", { name: "Plants", exact: true }),
    ).toBeVisible();
    expect(await readCopy(page)).toMatchObject({ plants: 1, flag: "1" });
    for (let step = 0; step < 12; step += 1)
        await page.clock.fastForward(5 * minute);
    await page.getByRole("button", { name: "Account", exact: true }).click();
    await expect(
        page.getByRole("heading", { name: "Account", exact: true, level: 1 }),
    ).toBeVisible();
    await expect(page.getByText(setting, { exact: true })).toHaveCount(0);
    expect(days).toEqual([]);
    expect(refreshCode()).toBe(false);

    await page.route("**/api/**", (route) => route.abort());
    const now = await page.evaluate(() => Date.now());
    await page.clock.pauseAt(now + 31 * minute);
    await expect(
        page.getByRole("heading", { name: "Pyracast cannot be reached" }),
    ).toBeVisible();
    await expect(page.getByRole("button", { name: "Try again" })).toBeVisible();
    await expect(
        page.getByRole("button", { name: "Show last loaded figures" }),
    ).toHaveCount(0);
});
