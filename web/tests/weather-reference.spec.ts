import { expect, test } from "./support/test";
import { readFile } from "node:fs/promises";
import { mockApi, region } from "./support/workspace";

const attribution = {
    text: "Weather data by Open-Meteo.com",
    url: "https://open-meteo.com/",
    license: "CC BY 4.0",
    license_url: "https://creativecommons.org/licenses/by/4.0/",
    changes: "Hourly values aggregated to daily energy.",
};

for (const [status, reference] of Object.entries({
    ready: {
        status: "ready",
        energy_kwh_m2: 4.2,
        coverage_fraction: 1,
        attribution,
    },
    partial: {
        status: "partial",
        energy_kwh_m2: null,
        partial_energy_kwh_m2: 1.2,
        coverage_fraction: 0.5,
        attribution,
    },
    unavailable: {
        status: "unavailable",
        quality_flags: ["provider_error"],
        attribution,
    },
    legacy: undefined,
})) {
    test(`regional weather reference: ${status}`, async ({ page }) => {
        await page.setViewportSize({ width: 320, height: 800 });
        await mockApi(page);
        await page.route("**/regions/11/forecast/**", (route) =>
            route.fulfill({
                json: {
                    region_id: 11,
                    date: "2026-11-11",
                    timezone: region.timezone,
                    source_kind: "forecast",
                    freshness: "fresh",
                    result: {
                        complete: true,
                        energy_kwh_m2: 3,
                        coverage_fraction: 1,
                        intervals: [],
                        weather_reference: reference,
                    },
                },
            }),
        );
        await page.goto("/app/");
        await page
            .getByRole("button", { name: /East array|Antalya solar park/ })
            .click();
        await page
            .getByRole("button", { name: "Irradiance", exact: true })
            .click();
        const card = page.getByRole("region", {
            name: "Independent weather model",
        });
        if (status === "legacy") {
            await expect(card).toHaveCount(0);
            return;
        }
        await expect(card).toBeVisible();
        await expect(
            card.getByRole("link", { name: "Open-Meteo.com" }),
        ).toHaveAttribute("href", "https://open-meteo.com/");
        await expect(
            card.getByRole("link", { name: "CC BY 4.0" }),
        ).toHaveAttribute(
            "href",
            "https://creativecommons.org/licenses/by/4.0/",
        );
        if (status === "ready") {
            await expect(card).toContainText("4.2");
            const downloadPromise = page.waitForEvent("download");
            await page
                .getByRole("button", { name: "Download CSV", exact: true })
                .click();
            const csv = await readFile(
                await (await downloadPromise).path(),
                "utf8",
            );
            expect(csv).not.toContain("weather_reference");
            expect(csv).not.toContain("Open-Meteo");
        } else if (status === "partial") {
            await expect(card).toContainText("The daily total is unavailable");
            await expect(card).toContainText("1.2");
            await expect(card).toContainText("50");
            await expect(card.locator(".stat").first()).toContainText("—");
        } else {
            await expect(card).toContainText("currently unavailable");
            await expect(card).not.toContainText("provider_error");
        }
    });
}
