import { expect, test } from "./support/test";
import { mockApi, region } from "./support/workspace";

const liveAt = "2026-11-11T05:00:00Z";
const archivedAt = "2026-11-11T02:30:00Z";
const plantTime = (value: string) =>
    new Intl.DateTimeFormat("en", {
        dateStyle: "medium",
        timeStyle: "short",
        timeZone: region.timezone,
    }).format(new Date(value));

test("the last forecast before sunrise is requested and shown without tags", async ({
    page,
}) => {
    await page.clock.setFixedTime(new Date("2026-11-11T12:00:00Z"));
    await mockApi(page);
    const sources: string[] = [];
    await page.route("**/plants/21/forecast/**", (route) => {
        const source =
            new URL(route.request().url()).searchParams.get("data_source") ??
            "";
        sources.push(source);
        const archived = source === "archived_forecast";
        return route.fulfill({
            json: {
                date: "2026-11-11",
                timezone: region.timezone,
                source_kind: "forecast",
                freshness: "archived",
                configuration_id: 3,
                result: {
                    complete: true,
                    energy_mwh: archived ? 11.25 : 12.5,
                    coverage_fraction: 1,
                    intervals: [],
                    quality_flags: archived ? ["archived_forecast"] : [],
                    provenance: {
                        collected_at: archived ? archivedAt : liveAt,
                    },
                },
            },
        });
    });
    await page.goto("/app/");
    await page
        .getByRole("button", { name: /East array|Antalya solar park/ })
        .click();
    const choice = page.getByLabel("Data", { exact: true });
    await expect(choice.locator("option")).toHaveText([
        "Forecast",
        "Last forecast before sunrise",
        "Production calculated from satellite data",
    ]);
    await expect(
        choice.getByRole("option", { name: "Last forecast before sunrise" }),
    ).toHaveAttribute("value", "archived_forecast");
    const panel = page.locator("section.panel");
    const total = panel.locator(".stat strong");
    await page.getByLabel("Forecast date").fill("2026-11-11");
    await expect(total).toHaveText("12.5 MWh");
    await expect(panel.getByText(/^Forecast data: /)).toHaveText(
        `Forecast data: ${plantTime(liveAt)}`,
    );
    await choice.selectOption("archived_forecast");
    await expect.poll(() => sources.at(-1)).toBe("archived_forecast");
    await expect(choice).toHaveValue("archived_forecast");
    await expect(page.getByLabel("Forecast date")).toHaveValue("2026-11-11");
    await expect(total).toHaveText("11.25 MWh");
    await expect(panel.getByText(/^Forecast data: /)).toHaveText(
        `Forecast data: ${plantTime(archivedAt)}`,
    );
    await expect(page.locator("section.panel .tag")).toHaveCount(0);
    await expect(page.getByText(/original calibration/)).toHaveCount(0);
    await expect(page.getByText(/Configuration 3/)).toHaveCount(0);
});
