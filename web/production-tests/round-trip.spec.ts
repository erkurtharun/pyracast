import { expect, test, type Page } from "../tests/support/test";
import {
    firstAccount,
    mockApi,
    type AccountState,
} from "../tests/support/workspace";
import { serveSession } from "../tests/support/session";
import { daysAnswer, readHeld } from "../tests/support/saved-copy";
import { workerWithdrawn } from "../tests/support/worker-state";

const NOTICE_POLICY =
    "default-src 'none'; style-src 'unsafe-inline'; form-action 'none'; base-uri 'none'; frame-ancestors 'none'";
const FORECAST = "/app/w/1/plants/11/forecast/";
const DAYS = /\/api\/tenants\/1\/regions\/11\/plants\/21\/days\/$/;
const MINUTE = 60_000;
const TITLE = "Keep figures on this device";

type Served = { answer: object };

async function application(page: Page, account: AccountState, served: Served) {
    await mockApi(page, { account });
    await serveSession(page, account);
    await page.route(DAYS, (route) => route.fulfill({ json: served.answer }));
}

function totals(page: Page) {
    return readHeld(page).then(({ days }) =>
        days.map((day) => `${day.kind} ${day.date} ${day.total_mwh}`),
    );
}

async function expectSaved(page: Page, address: string, heading: string) {
    const answer = await page.goto(address);
    expect(answer?.status()).toBe(503);
    expect(answer?.fromServiceWorker()).toBe(true);
    expect(answer?.headers()["content-security-policy"]).toBe(NOTICE_POLICY);
    await expect(page.getByRole("heading", { name: heading })).toBeVisible();
    expect(await page.evaluate(() => document.scripts.length)).toBe(0);
    await expect(page.getByText(/^Read from the server on /)).toBeVisible();
}

async function expectNothingSaved(page: Page) {
    const answer = await page.goto(`${FORECAST}?date=2026-11-11`);
    expect(answer?.status()).toBe(503);
    await expect(
        page.getByRole("heading", { name: "Pyracast cannot be reached" }),
    ).toBeVisible();
    await expect(page.getByText("East array")).toHaveCount(0);
}

if (!workerWithdrawn)
    test("figures kept on the device are read without a connection, renewed once it returns, and gone after a removal and after a sign-out made without a connection", async ({
        page,
        context,
    }) => {
        const account: AccountState = { current: null };
        const served: Served = { answer: daysAnswer(1.5) };
        await application(page, account, served);
        await page.goto("/app/");
        await page.evaluate(() => navigator.serviceWorker.ready);
        account.current = firstAccount;
        await page.goto("/app/w/1/");
        await expect(
            page.getByRole("heading", { name: "Plants", exact: true }),
        ).toBeVisible();
        await page.getByText(TITLE, { exact: true }).click();
        await page.getByRole("button", { name: "Turn on" }).click();
        await expect
            .poll(() => totals(page))
            .toEqual(["forecast 2026-11-11 1.5", "production 2026-11-09 1.2"]);

        await context.setOffline(true);
        const offline = await context.newPage();
        await application(offline, account, served);
        await expectSaved(
            offline,
            `${FORECAST}?date=2026-11-11`,
            "East array, Sunfield Energy: Forecast",
        );
        await expect(offline.getByText("Day total: 1.5 MWh")).toBeVisible();
        await offline.reload();
        await expect(offline.getByText("Day total: 1.5 MWh")).toBeVisible();
        await offline.getByRole("link", { name: "Production" }).click();
        await expect(
            offline.getByRole("heading", {
                name: "East array, Sunfield Energy: Production",
            }),
        ).toBeVisible();

        const renewed = daysAnswer(2.5);
        served.answer = {
            ...renewed,
            forecast: [
                ...renewed.forecast,
                ...daysAnswer(3, "2026-11-12").forecast,
            ],
        };
        await context.setOffline(false);
        await offline.getByRole("link", { name: "Try again" }).first().click();
        await expect(
            offline.getByRole("heading", { name: "East array", level: 1 }),
        ).toBeVisible();
        await offline.clock.fastForward(16 * MINUTE);
        await expect
            .poll(() => totals(offline))
            .toEqual([
                "forecast 2026-11-11 2.5",
                "forecast 2026-11-12 3",
                "production 2026-11-09 1.2",
            ]);

        await context.setOffline(true);
        await expectSaved(
            offline,
            `${FORECAST}?date=2026-11-12`,
            "East array, Sunfield Energy: Forecast",
        );
        await expect(offline.getByText("Day total: 3 MWh")).toBeVisible();
        await offline
            .getByRole("link", {
                name: "Remove saved figures from this device",
            })
            .click();
        await offline
            .getByRole("link", { name: "Remove saved figures", exact: true })
            .click();
        await expect(
            offline.getByText(
                "The saved figures were removed from this device.",
            ),
        ).toBeVisible();
        await expectNothingSaved(offline);

        await context.setOffline(false);
        account.current = { ...firstAccount, tag: "tag-c" };
        await page.reload();
        await expect
            .poll(() => totals(page))
            .toEqual([
                "forecast 2026-11-11 2.5",
                "forecast 2026-11-12 3",
                "production 2026-11-09 1.2",
            ]);
        await context.setOffline(true);
        await page.route("**/api/auth/browser/v1/auth/session", (route) =>
            route.request().method() === "DELETE"
                ? route.abort("internetdisconnected")
                : route.fallback(),
        );
        await page
            .getByRole("button", { name: "Sign out", exact: true })
            .click();
        await expect(
            page.getByRole("heading", { name: "Signing out…" }),
        ).toBeVisible();
        await expectNothingSaved(offline);
    });
