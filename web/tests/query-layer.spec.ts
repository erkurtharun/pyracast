import { expect, test, type Page } from "./support/test";
import { mockApi, paged, region, tenantRow } from "./support/workspace";
import { errorBody } from "./support/errors";

const FIVE_MINUTES = 5 * 60_000;

function forecast(energy: number) {
    return {
        date: "2026-11-20",
        timezone: region.timezone,
        freshness: "fresh",
        source_kind: "forecast",
        configuration_id: 31,
        result: {
            complete: true,
            energy_mwh: energy,
            coverage_fraction: 1,
            quality_flags: [],
            provenance: {},
            intervals: [
                {
                    start: "2026-11-20T09:00:00Z",
                    end: "2026-11-20T10:00:00Z",
                    mean_power_mw: energy,
                    energy_mwh: energy,
                },
            ],
        },
    };
}

function countApiRequests(page: Page) {
    const counter = { value: 0 };
    page.on("request", (request) => {
        if (new URL(request.url()).pathname.startsWith("/api/"))
            counter.value++;
    });
    return counter;
}

async function openPlant(page: Page, section?: string) {
    await page.goto("/app/");
    await page.getByRole("button", { name: /East array/ }).click();
    if (section)
        await page.getByRole("button", { name: section, exact: true }).click();
}

const generation = (page: Page) =>
    page.locator(".stat", { hasText: "Estimated energy" });

test("a failing second forecast poll keeps the chart on screen", async ({
    page,
}) => {
    await page.clock.install({ time: new Date("2026-11-19T12:00:00Z") });
    await mockApi(page);
    let fail = false;
    await page.route("**/plants/21/forecast/**", (route) =>
        fail
            ? route.fulfill({
                  status: 503,
                  json: errorBody("busy", "Forecast service is busy."),
              })
            : route.fulfill({ json: forecast(237.773) }),
    );
    await openPlant(page);
    await expect(generation(page)).toContainText("237.773");
    fail = true;
    await page.clock.runFor(30_000);
    await expect(
        page.locator("main").getByRole("alert").filter({
            hasText: "Could not refresh",
        }),
    ).toContainText("Forecast service is busy.");
    await expect(generation(page)).toContainText("237.773");
    fail = false;
    await page.getByRole("button", { name: "Try again", exact: true }).click();
    await expect(
        page.locator("main").getByRole("alert").filter({
            hasText: "Could not refresh",
        }),
    ).toHaveCount(0);
    await expect(generation(page)).toContainText("237.773");
});

test("a hidden tab makes no request", async ({ page }) => {
    await page.clock.install({ time: new Date("2026-11-19T12:00:00Z") });
    await mockApi(page);
    await page.route("**/plants/21/forecast/**", (route) =>
        route.fulfill({ json: forecast(12) }),
    );
    await openPlant(page);
    await expect(generation(page)).toContainText("12");
    await page.evaluate(() => {
        Object.defineProperty(document, "visibilityState", {
            configurable: true,
            value: "hidden",
        });
        document.dispatchEvent(new Event("visibilitychange"));
    });
    const requests = countApiRequests(page);
    await page.clock.runFor(FIVE_MINUTES);
    expect(requests.value).toBe(0);
});

for (const screen of [
    { name: "portfolio" },
    { name: "plant forecast", section: "Forecast" },
    { name: "plant calibration", section: "Calibration" },
    { name: "plant production as a member", section: "Production" },
    { name: "plant settings", section: "Settings" },
    { name: "inbox", inbox: true },
] as const) {
    test(`the ${screen.name} screen makes at most 25 API requests in five visible minutes`, async ({
        page,
    }) => {
        await page.clock.install({ time: new Date("2026-11-19T12:00:00Z") });
        await mockApi(page);
        if (screen.name === "plant production as a member")
            await page.route("**/api/tenants/", (route) =>
                route.fulfill({
                    json: paged([
                        tenantRow({
                            id: 1,
                            name: "Sunfield Energy",
                            role: "viewer",
                        }),
                    ]),
                }),
            );
        if (screen.name === "plant settings")
            await page.route("**/collection-schedule/", (route) =>
                route.fulfill({
                    json: {
                        enabled: true,
                        times: ["09:00"],
                        timezone: region.timezone,
                        last_success_at: null,
                        next_collection_at: "2026-11-20T06:00:00Z",
                        history_requests_reserved_per_day: 1,
                        history_local_time: "03:00",
                        history_retry_until_local: "09:00",
                        history_retry_minutes: 30,
                        max_forecast_times: 9,
                        effective_from: "2026-11-01T00:00:00Z",
                        recent_jobs: [],
                    },
                }),
            );
        const requests = countApiRequests(page);
        if ("inbox" in screen) {
            await page.goto("/app/");
            await page.getByRole("button", { name: "Inbox" }).click();
        } else if ("section" in screen) {
            await openPlant(page, screen.section);
            if (screen.name === "plant settings")
                await page
                    .getByRole("button", {
                        name: "Data collection",
                        exact: true,
                    })
                    .click();
        } else await page.goto("/app/");
        await expect(page.locator("main h1").first()).toBeVisible();
        await page.clock.runFor(FIVE_MINUTES);
        expect(requests.value).toBeLessThanOrEqual(25);
    });
}
