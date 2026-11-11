import { expect, test } from "@playwright/test";

test("journal date controls wait for the initial range before selecting a day", async ({
    page,
}, testInfo) => {
    const paged = (results: unknown[]) => ({
        results,
        count: results.length,
        next: null,
        previous: null,
    });
    const region = {
        id: 11,
        name: "Date test region",
        latitude: 36.9,
        longitude: 30.7,
        timezone: "Europe/Istanbul",
        surface: "horizontal",
        revision: 1,
        status: "approved",
    };
    const configuration = {
        id: 31,
        revision: 1,
        binding_id: 1,
        created_at: "2026-01-01T00:00:00Z",
        dc_capacity_mw: 5,
        inverter_capacity_mw: 4,
        export_limit_mw: null,
        measurement_basis: "net_export",
    };
    const plant = {
        id: 21,
        name: "Date test plant",
        region_id: 11,
        created_at: "2026-01-01T00:00:00Z",
        current_configuration: configuration,
    };
    let release!: () => void;
    const initialResponse = new Promise<void>((resolve) => {
        release = resolve;
    });
    await page.route("**/api/**", async (route) => {
        const url = new URL(route.request().url()),
            path = url.pathname;
        let data: unknown;
        if (path === "/api/session/")
            data = {
                user: { id: 1, username: "date.user", email: "date@test.test" },
                csrf_token: "date-token",
            };
        else if (path === "/api/tenants/")
            data = paged([{ id: 1, name: "Date test tenant" }]);
        else if (path.endsWith("/regions/")) data = paged([region]);
        else if (path.endsWith("/plants/")) data = paged([plant]);
        else if (path.endsWith("/calibrations/")) data = paged([]);
        else if (path.endsWith("/performance/"))
            data = {
                start_date: "2026-01-01",
                end_date: "2026-01-01",
                matched_days: 0,
                requested_days: 1,
                metrics: null,
                baseline_metrics: null,
                days: [],
            };
        else if (path.endsWith("/forecast/"))
            data = {
                timezone: region.timezone,
                freshness: "missing",
                result: null,
            };
        else if (path.endsWith("/production/")) {
            expect(url.searchParams.get("daylight_only")).toBe("true");
            if (url.searchParams.has("start_date"))
                expect(url.searchParams.get("end_date")).toBe(
                    url.searchParams.get("start_date"),
                );
            const start = url.searchParams.get("start_date") ?? "2026-01-01",
                end = url.searchParams.get("end_date") ?? "2026-01-07";
            if (!url.searchParams.has("start_date")) await initialResponse;
            data = {
                timezone: region.timezone,
                start_date: start,
                end_date: end,
                available_start_date: "2026-01-01",
                available_end_date: "2026-01-07",
                days: [
                    {
                        date: start,
                        available_count: 1,
                        recorded_count: 0,
                        missing_count: 1,
                        sunrise:
                            start === "2026-01-06"
                                ? `${start}T03:00:00+03:00`
                                : `${start}T06:15:00Z`,
                        sunset:
                            start === "2026-01-06"
                                ? `${start}T01:00:00+03:00`
                                : `${start}T16:45:00Z`,
                        daylight_status: "normal",
                        daylight_windows:
                            start === "2026-01-06"
                                ? [
                                      {
                                          starts_at: `${start}T00:00:00+03:00`,
                                          ends_at: `${start}T01:00:00+03:00`,
                                      },
                                      {
                                          starts_at: `${start}T03:00:00+03:00`,
                                          ends_at: "2026-01-07T00:00:00+03:00",
                                      },
                                  ]
                                : [
                                      {
                                          starts_at: `${start}T06:15:00Z`,
                                          ends_at: `${start}T16:45:00Z`,
                                      },
                                  ],
                    },
                ],
                intervals: [
                    {
                        starts_at: `${start}T09:00:00Z`,
                        ends_at: `${start}T10:00:00Z`,
                        can_enter: true,
                        reference_available: true,
                        observation: null,
                        unavailable_reason: null,
                    },
                ],
                observations: [],
            };
        } else throw new Error(`Unmocked API: ${path}`);
        await route.fulfill({ json: data });
    });
    await page.goto("/");
    await page.getByRole("button", { name: /Date test region/ }).click();
    await page
        .getByRole("button", { name: "Solar plants", exact: true })
        .click();
    await page.getByRole("button", { name: /Date test plant/ }).click();
    await page
        .getByRole("button", { name: "Calibration", exact: true })
        .click();
    await expect(page.getByLabel("Day", { exact: true })).toBeDisabled();
    release();
    await page.getByLabel("Day", { exact: true }).fill("2026-01-07");
    await expect(
        page.getByLabel("Production 2026-01-07T09:00:00Z"),
    ).toBeEnabled();
    await expect(
        page.getByLabel("Production 2026-01-01T09:00:00Z"),
    ).toHaveCount(0);
    await expect(page.getByText(/09:15/)).toBeVisible();
    const entry = page.getByLabel("Production 2026-01-07T09:00:00Z");
    await entry.fill("0");
    await expect(
        page.getByRole("button", { name: "Save meter readings" }),
    ).toBeEnabled();
    await page.getByLabel("Day", { exact: true }).fill("2026-01-06");
    await expect(
        page.getByLabel("Production 2026-01-06T09:00:00Z"),
    ).toHaveValue("");
    await expect(page.getByText("00:00–01:00, 03:00–00:00")).toBeVisible();
    await expect(
        page.getByRole("button", { name: "Save meter readings" }),
    ).toBeDisabled();
    await page.getByLabel("Day", { exact: true }).fill("2026-01-07");
    await expect(entry).toHaveValue("0");
    await page.setViewportSize({ width: 390, height: 844 });
    await page.screenshot({
        path: testInfo.outputPath("production-journal-mobile.png"),
        fullPage: true,
    });
});
