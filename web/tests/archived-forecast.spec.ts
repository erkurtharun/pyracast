import { expect, test } from "./support/test";
import { mockApi, region } from "./support/workspace";

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
    await page.goto("/");
    await page
        .getByRole("button", { name: /East array|Antalya solar park/ })
        .click();
    await expect(
        page.getByRole("option", { name: "Forecast outlook" }),
    ).toHaveAttribute("value", "forecast");
    await expect(
        page.getByRole("option", { name: "Archived day-ahead forecast" }),
    ).toHaveAttribute("value", "archived_forecast");
    await expect(
        page.getByText(/Saved day-ahead forecast using configuration/),
    ).toHaveCount(0);
    await page.getByLabel("Data source").selectOption("archived_forecast");
    await expect.poll(() => sources.at(-1)).toBe("archived_forecast");
    await expect(
        page.getByText(
            "Saved day-ahead forecast using configuration 3. This archive is separate from the current outlook and metered production.",
        ),
    ).toBeVisible();
    await expect(
        page.getByText(
            "Saved prior-day forecast under its original configuration.",
        ),
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
