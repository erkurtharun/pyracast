import { expect, test } from "@playwright/test";
import { mockApi, paged, region } from "./support/workspace";

test("journal distinguishes repeated daylight-saving hours and compacts ordinary days", async ({
    page,
}) => {
    await page.clock.setFixedTime(new Date("2026-11-10T12:00:00Z"));
    await mockApi(page);
    await page.route("**/tenants/1/regions/", async (route) => {
        await route.fulfill({
            json: paged([{ ...region, timezone: "Europe/Berlin" }]),
        });
    });
    await page.route("**/plants/21/production/**", async (route) => {
        const date =
            new URL(route.request().url()).searchParams.get("start_date") ??
            "2026-10-25";
        await route.fulfill({
            json: {
                timezone: "Europe/Berlin",
                available_start_date: "2026-10-24",
                available_end_date: "2026-10-25",
                start_date: date,
                end_date: date,
                days: [],
                intervals: ["00", "01"].map((hour) => ({
                    starts_at: `${date}T${hour}:00:00Z`,
                    ends_at: `${date}T${hour === "00" ? "01" : "02"}:00:00Z`,
                    observation: null,
                    can_enter: true,
                    reference_available: true,
                    unavailable_reason: null,
                })),
                observations: [],
            },
        });
    });

    await page.goto("/");
    await page.getByRole("button", { name: /Antalya solar park/ }).click();
    await page
        .getByRole("button", { name: "Solar plants", exact: true })
        .click();
    await page.getByRole("button", { name: /East array/ }).click();
    await page
        .getByRole("button", { name: "Calibration", exact: true })
        .click();

    const table = page.getByRole("table", {
        name: "Meter entries \u00b7 60 minutes",
    });
    await expect(page.getByText(/Local times in Europe\/Berlin/)).toBeVisible();
    await expect(
        table.getByRole("rowheader", {
            name: "02:00 GMT+2\u201302:00 GMT+1",
            exact: true,
        }),
    ).toBeVisible();
    await expect(
        table.getByRole("rowheader", {
            name: "02:00 GMT+1\u201303:00 GMT+1",
            exact: true,
        }),
    ).toBeVisible();

    const first = page.getByLabel("Production 2026-10-25T00:00:00Z", {
        exact: true,
    });
    const second = page.getByLabel("Production 2026-10-25T01:00:00Z", {
        exact: true,
    });
    await first.fill("0");
    await second.fill("125");
    await expect(first).toHaveValue("0");
    await expect(second).toHaveValue("125");
    await expect(page.getByText("2 readings ready to save")).toBeVisible();

    await page.getByLabel("Day", { exact: true }).fill("2026-10-24");
    await expect(
        table.getByRole("rowheader", { name: "02:00\u201303:00", exact: true }),
    ).toBeVisible();
    await expect(
        table.getByRole("rowheader", { name: "03:00\u201304:00", exact: true }),
    ).toBeVisible();
    await expect(table.getByRole("rowheader", { name: /GMT/ })).toHaveCount(0);
    await expect(
        page.getByLabel("Production 2026-10-24T00:00:00Z"),
    ).toHaveValue("");

    await page.getByLabel("Day", { exact: true }).fill("2026-10-25");
    await expect(first).toHaveValue("0");
    await expect(second).toHaveValue("125");
    await expect(
        table.getByRole("rowheader", {
            name: "02:00 GMT+2\u201302:00 GMT+1",
            exact: true,
        }),
    ).toBeVisible();
});
