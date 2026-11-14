import { expect, test, type Page } from "./support/test";
import { mockApi, region } from "./support/workspace";

async function openForecast(page: Page, width: number) {
    await page.setViewportSize({ width, height: 900 });
    await mockApi(page);
    await page.route("**/regions/11/forecast/**", (route) =>
        route.fulfill({
            json: {
                region_id: region.id,
                date: "2026-11-11",
                timezone: region.timezone,
                source_kind: "forecast",
                freshness: "fresh",
                collected_at: "2026-11-11T05:00:00Z",
                result: {
                    complete: true,
                    coverage_fraction: 1,
                    intervals: [
                        {
                            start: "2026-11-11T07:00:00Z",
                            end: "2026-11-11T07:30:00Z",
                            ghi_wm2: 500,
                        },
                    ],
                },
            },
        }),
    );
    await page.goto("/");
    await page
        .getByRole("button", { name: /East array|Antalya solar park/ })
        .click();
    await page.getByRole("button", { name: "Irradiance", exact: true }).click();
    return page.locator("details.interval-details");
}

test("wide content supports keyboard scrolling and adapts to viewport width", async ({
    page,
}) => {
    const details = await openForecast(page, 390);
    const summary = details.locator("summary");
    const scroll = details.locator(".table-scroll");
    await summary.click();
    await scroll.locator("table").evaluate((table) => {
        table.style.minWidth = "700px";
    });
    await expect(scroll).toHaveAttribute("role", "region");
    await expect(scroll).toHaveAccessibleName("Forecast intervals");
    await expect(scroll).toHaveAttribute("tabindex", "0");
    await summary.focus();
    await page.keyboard.press("Tab");
    await expect(scroll).toBeFocused();
    await page.keyboard.press("ArrowRight");
    await expect
        .poll(() => scroll.evaluate((element) => element.scrollLeft))
        .toBeGreaterThan(0);

    await page.setViewportSize({ width: 1440, height: 900 });
    await expect(scroll).not.toHaveAttribute("tabindex");
    await expect(scroll).not.toHaveAttribute("role");
    await expect(scroll).not.toHaveAttribute("aria-label");
    await expect(details.getByRole("table")).toHaveAccessibleName(
        `Forecast intervals, ${region.timezone}`,
    );

    await page.setViewportSize({ width: 390, height: 900 });
    await expect(scroll).toHaveAttribute("tabindex", "0");
    await expect(scroll).toHaveAccessibleName("Forecast intervals");
});

test("scroll regions follow table content width and closed details reopening", async ({
    page,
}) => {
    const details = await openForecast(page, 1440);
    const summary = details.locator("summary");
    const scroll = details.locator(".table-scroll");
    await expect(scroll).toBeHidden();
    await expect(
        page.getByRole("region", { name: "Forecast intervals" }),
    ).toHaveCount(0);
    await summary.click();
    await expect(scroll).not.toHaveAttribute("tabindex");
    const width = await scroll.evaluate((element) => element.clientWidth);
    await scroll.locator("table").evaluate((table, minimumWidth) => {
        table.style.minWidth = `${minimumWidth}px`;
    }, width + 200);
    await expect(scroll).toHaveAttribute("tabindex", "0");
    await expect(scroll).toHaveAccessibleName("Forecast intervals");
    expect(await scroll.evaluate((element) => element.clientWidth)).toBe(width);

    await summary.click();
    await expect(scroll).toBeHidden();
    await expect(
        page.getByRole("region", { name: "Forecast intervals" }),
    ).toHaveCount(0);
    await summary.click();
    await expect(scroll).toHaveAttribute("tabindex", "0");
    await expect(scroll).toHaveAccessibleName("Forecast intervals");
    await scroll.locator("table").evaluate((table) => {
        table.style.removeProperty("min-width");
    });
    await expect(scroll).not.toHaveAttribute("tabindex");
    await expect(scroll).not.toHaveAttribute("role");
    await expect(scroll).not.toHaveAttribute("aria-label");
});

test("simplified forecast intervals fit mobile without an extra scroll tab stop", async ({
    page,
}) => {
    const details = await openForecast(page, 390);
    const summary = details.locator("summary");
    const scroll = details.locator(".table-scroll");
    await summary.click();
    await expect(details.getByRole("columnheader")).toHaveText([
        "Time interval",
        "Irradiance (W/m²)",
    ]);
    expect(
        await scroll.evaluate(
            (element) => element.scrollWidth <= element.clientWidth,
        ),
    ).toBe(true);
    await expect(scroll).not.toHaveAttribute("tabindex");
    await expect(scroll).not.toHaveAttribute("role");
    await expect(scroll).not.toHaveAttribute("aria-label");
    await summary.focus();
    await page.keyboard.press("Tab");
    await expect(scroll).not.toBeFocused();
});
