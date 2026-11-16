import { expect, test } from "./support/test";
import { mockApi, region, user } from "./support/workspace";

test("plant archive requests and displays the original forecast", async ({
    page,
}) => {
    await mockApi(page);
    const sources: string[] = [];
    await page.route("**/plants/21/forecast/**", (route) => {
        const source =
            new URL(route.request().url()).searchParams.get("data_source") ??
            "";
        sources.push(source);
        return route.fulfill({
            json: {
                date: "2026-11-11",
                timezone: region.timezone,
                source_kind: "forecast",
                freshness: "stale",
                configuration_id: 3,
                result: {
                    complete: true,
                    energy_mwh: 12.5,
                    coverage_fraction: 1,
                    intervals: [],
                    quality_flags:
                        source === "archived_forecast"
                            ? ["archived_forecast"]
                            : [],
                },
            },
        });
    });
    await page.goto("/app/");
    await page
        .getByRole("button", { name: /East array|Antalya solar park/ })
        .click();
    await expect(
        page.getByRole("option", { name: "Forecast outlook" }),
    ).toHaveAttribute("value", "forecast");
    await expect(
        page.getByRole("option", { name: "Archived forecast" }),
    ).toHaveAttribute("value", "archived_forecast");
    await expect(
        page.locator(".tag").filter({ hasText: /^Archived forecast$/ }),
    ).toHaveCount(0);
    await page.getByLabel("Data source").selectOption("archived_forecast");
    await expect.poll(() => sources.at(-1)).toBe("archived_forecast");
    await expect(
        page.locator(".tag").filter({ hasText: /^Archived forecast$/ }),
    ).toBeVisible();
    await expect(
        page.getByText("Forecasts collected before daylight."),
    ).toBeVisible();
    await expect(
        page.getByText("12.5", { exact: false }).first(),
    ).toBeVisible();
    await page.getByText("Source & quality", { exact: true }).click();
    await expect(page.getByText(/Configuration 3/)).toBeVisible();
    await expect(
        page.getByText("Satellite/model estimates, not meter readings."),
    ).toHaveCount(0);
});

for (const language of ["en", "tr"] as const) {
    test(`reconstructed archive discloses recalculation and its local time (${language})`, async ({
        page,
    }) => {
        await mockApi(page);
        if (language === "tr") {
            await page.setViewportSize({ width: 390, height: 844 });
            await page.route("**/api/session/", (route) =>
                route.fulfill({
                    json: { user, language, csrf_token: "signed-token" },
                }),
            );
        }
        await page.route("**/plants/21/forecast/**", (route) =>
            route.fulfill({
                json: {
                    date: "2026-11-11",
                    timezone: region.timezone,
                    source_kind: "forecast",
                    freshness: "stale",
                    result: {
                        complete: true,
                        energy_mwh: 17.25,
                        coverage_fraction: 1,
                        intervals: [],
                        quality_flags: ["reconstructed_forecast"],
                        provenance: {
                            reconstructed_at: "2026-11-16T10:30:00Z",
                            calibration_id: 12,
                            factor: 0.9,
                        },
                    },
                },
            }),
        );
        await page.goto("/app/");
        await page
            .getByRole("button", { name: /East array|Antalya solar park/ })
            .click();
        await page
            .getByLabel(language === "tr" ? "Veri kaynağı" : "Data source")
            .selectOption("archived_forecast");
        await expect(
            page
                .getByText(
                    language === "tr"
                        ? "Güncel kalibrasyonla yeniden hesaplandı"
                        : "Recalculated with the latest calibration",
                    {
                        exact: true,
                    },
                )
                .first(),
        ).toBeVisible();
        await expect(
            page.getByText(language === "tr" ? "17,25 MWh" : "17.25 MWh", {
                exact: true,
            }),
        ).toBeVisible();
        await expect(
            page.locator(".tag").filter({
                hasText:
                    language === "tr"
                        ? /^Arşivlenmiş tahmin$/
                        : /^Archived forecast$/,
            }),
        ).toHaveCount(0);
        await expect(
            page.getByText(
                language === "tr"
                    ? "Uydu ve model tahminleridir; sayaç ölçümü değildir."
                    : "Satellite/model estimates, not meter readings.",
            ),
        ).toHaveCount(0);
        await page
            .getByText(
                language === "tr" ? "Kaynak ve kalite" : "Source & quality",
                { exact: true },
            )
            .click();
        await expect(
            page.getByText(
                language === "tr"
                    ? /Hesaplama zamanı:.*16\.11\.2026.*13:30/
                    : /Recalculated at:.*11\/16\/2026.*13:30/,
            ),
        ).toBeVisible();
        if (language === "tr")
            expect(
                await page.evaluate(() => document.documentElement.scrollWidth),
            ).toBeLessThanOrEqual(390);
    });
}
