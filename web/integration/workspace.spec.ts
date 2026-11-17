import { expect, test } from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";
import { readFile } from "node:fs/promises";
import Papa from "papaparse";

test("a customer manages one plant site through the real API", async ({
    page,
}, testInfo) => {
    test.setTimeout(60_000);
    await page.clock.setFixedTime(
        new Date(`${process.env.BROWSER_TODAY!}T12:00:00Z`),
    );
    await page.goto("/app/");
    await expect(page.getByLabel("Email", { exact: true })).toBeVisible();
    await page.screenshot({
        path: testInfo.outputPath("login-desktop.png"),
        fullPage: true,
    });
    await page
        .getByLabel("Email", { exact: true })
        .fill(process.env.BROWSER_EMAIL!);
    await page.getByLabel("Password").fill(process.env.BROWSER_PASSWORD!);
    await page
        .locator("form")
        .getByRole("button", { name: "Sign in", exact: true })
        .click();
    await expect(page.getByRole("heading", { name: "Plants" })).toBeVisible();

    const forbidden = await page.evaluate(
        async ({ tenant, region }) => {
            const response = await fetch(
                `/api/tenants/${tenant}/regions/${region}/`,
                {
                    credentials: "same-origin",
                    cache: "no-store",
                },
            );
            return response.status;
        },
        {
            tenant: process.env.BROWSER_OTHER_TENANT,
            region: process.env.BROWSER_OTHER_REGION,
        },
    );
    expect([403, 404]).toContain(forbidden);

    await page.getByRole("button", { name: /Site/ }).click();
    await expect(
        page.getByRole("heading", { name: "Complete plant setup" }),
    ).toBeVisible();
    await page.getByLabel("Plant name").fill("Browser plant");
    await page.getByLabel("DC nameplate capacity (MW)").fill("130");
    await page.getByLabel("Inverter AC capacity (MW, optional)").fill("100");
    await page.getByRole("button", { name: "Continue", exact: true }).click();
    const created = page.waitForResponse(
        (response) =>
            response.request().method() === "POST" &&
            /\/plants\/$/.test(new URL(response.url()).pathname),
    );
    await page
        .getByRole("button", { name: "Submit plant for approval" })
        .click();
    const createdPlant = (await (await created).json()) as {
        id: number;
        region_id: number;
        current_configuration: { id: number; revision: number };
    };
    expect(createdPlant.current_configuration.revision).toBe(1);
    const tenantId = await page.evaluate(async () => {
        const response = await fetch("/api/tenants/");
        const tenants = await response.json();
        return tenants.results[0].id as number;
    });
    const plantUrl = `/api/tenants/${tenantId}/regions/${createdPlant.region_id}/plants/${createdPlant.id}/`;
    await expect(
        page.getByRole("heading", { name: "Browser plant" }),
    ).toBeVisible();
    await expect(page.getByLabel("Forecast date")).toHaveValue(
        process.env.BROWSER_DATE!,
    );
    await page.getByRole("button", { name: "Irradiance", exact: true }).click();

    await page.getByLabel("Forecast date").fill(process.env.BROWSER_DATE!);
    await expect(
        page.getByRole("heading", { name: "Irradiance" }),
    ).toBeVisible();
    await expect(
        page.getByRole("img", { name: /Irradiance by interval/ }),
    ).toBeVisible();
    await expect(page.getByText("kWh/m²").first()).toBeVisible();
    await page.screenshot({
        path: testInfo.outputPath("region-desktop.png"),
        fullPage: true,
    });
    const regionA11y = await new AxeBuilder({ page })
        .withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"])
        .analyze();
    expect(
        regionA11y.violations.filter(
            (item) => item.impact === "critical" || item.impact === "serious",
        ),
    ).toEqual([]);

    await page
        .getByLabel("Forecast date")
        .fill(process.env.BROWSER_HISTORY_DATE!);
    await expect(
        page.getByText("Satellite/model estimates, not meter readings."),
    ).toBeVisible();
    await page.getByRole("button", { name: "Generation", exact: true }).click();
    await page.getByLabel("Forecast date").fill(process.env.BROWSER_DATE!);
    await page.getByLabel("Resolution").selectOption("60");
    await expect(
        page.getByText("View interval data (12 intervals)"),
    ).toBeVisible();
    await page.getByText("View interval data (12 intervals)").click();
    await expect(
        page
            .getByRole("table", { name: "Forecast intervals, Europe/Istanbul" })
            .getByRole("columnheader"),
    ).toHaveText(["Time interval", "Energy (MWh)"]);
    await page.getByText("View interval data (12 intervals)").click();
    async function daylightForecast(resolution: 30 | 60) {
        const response = await page.request.get(`${plantUrl}forecast/`, {
            params: {
                date: process.env.BROWSER_DATE!,
                resolution_minutes: resolution,
                data_source: "auto",
            },
        });
        expect(response.status()).toBe(200);
        const forecast = (await response.json()) as {
            result: {
                daylight: {
                    complete: boolean;
                    coverage_fraction: number;
                    energy_mwh: number;
                };
            };
        };
        expect(forecast.result.daylight.complete).toBe(true);
        expect(forecast.result.daylight.coverage_fraction).toBe(1);
        expect(forecast.result.daylight.energy_mwh).toBeGreaterThan(0);
        return forecast.result.daylight;
    }
    const hourlyDaylight = await daylightForecast(60);
    const daylightEnergy = new Intl.NumberFormat("en", {
        maximumFractionDigits: 3,
    }).format(hourlyDaylight.energy_mwh);
    await expect(
        page.locator(".stat").filter({ hasText: /^Estimated energy/ }),
    ).toContainText(`${daylightEnergy} MWh`);
    await expect(
        page.locator(".stat").filter({ hasText: /^Coverage/ }),
    ).toContainText("100 %");
    async function expectFullForecastCsv(fullCount: 24 | 48) {
        const download = page.waitForEvent("download");
        await page
            .getByRole("button", { name: "Download CSV", exact: true })
            .first()
            .click();
        const bytes = await readFile((await (await download).path())!);
        expect([...bytes.subarray(0, 3)]).toEqual([239, 187, 191]);
        const parsed = Papa.parse<Record<string, string>>(
            bytes.toString("utf8"),
            { header: true, skipEmptyLines: true },
        );
        expect(parsed.errors).toEqual([]);
        expect(parsed.data).toHaveLength(fullCount);
        const positive = parsed.data.filter(
            (row) => Number(row.mean_power_mw) > 0,
        );
        expect(positive).toHaveLength(fullCount / 2);
        expect(parsed.data[0].mean_power_mw).toBe("0");
        expect(parsed.data.at(-1)!.mean_power_mw).toBe("0");
        const localTime = (stamp: string) =>
            new Intl.DateTimeFormat("en-GB", {
                timeZone: "Europe/Istanbul",
                hour: "2-digit",
                minute: "2-digit",
                hourCycle: "h23",
            }).format(new Date(stamp));
        expect(localTime(parsed.data[0].start)).toBe("00:00");
        expect(localTime(parsed.data.at(-1)!.end)).toBe("00:00");
        expect(localTime(positive[0].start)).toBe("06:00");
        expect(localTime(positive.at(-1)!.end)).toBe("18:00");
    }
    await expectFullForecastCsv(24);
    await page.getByLabel("Resolution").selectOption("30");
    await expect(
        page.getByText("View interval data (24 intervals)"),
    ).toBeVisible();
    expect((await daylightForecast(30)).energy_mwh).toBe(
        hourlyDaylight.energy_mwh,
    );
    await expect(
        page.locator(".stat").filter({ hasText: /^Estimated energy/ }),
    ).toContainText(`${daylightEnergy} MWh`);
    await expectFullForecastCsv(48);
    const outageTimes = await page.evaluate(() => {
        const localInput = (minutesAgo: number) => {
            const date = new Date(Date.now() - minutesAgo * 60_000);
            return new Date(date.getTime() - date.getTimezoneOffset() * 60_000)
                .toISOString()
                .slice(0, 16);
        };
        return { starts: localInput(10), ends: localInput(5) };
    });
    await page.getByRole("button", { name: "Outages", exact: true }).click();
    await page
        .getByRole("button", { name: "Report outage", exact: true })
        .click();
    await page.getByLabel("Incident title").fill("Browser inverter outage");
    await page
        .getByLabel("Started at (device time)", { exact: true })
        .fill(outageTimes.starts);
    await page
        .getByRole("button", { name: "Save outage", exact: true })
        .click();
    await expect(
        page.getByRole("heading", { name: "Browser inverter outage" }),
    ).toBeVisible();
    await page.getByRole("button", { name: "Forecast", exact: true }).click();
    await expect(
        page.locator(".stat").getByText("Normal capacity", { exact: true }),
    ).toBeVisible();
    await expect(
        page.locator(".stat").getByText("With outages", { exact: true }),
    ).toBeVisible();
    await expect(
        page.locator(".stat").filter({ hasText: /^Normal capacity/ }),
    ).toContainText(`${daylightEnergy} MWh`);
    await expect(
        page
            .locator(".stat")
            .filter({ hasText: /^With outages/ })
            .locator("strong"),
    ).toHaveText("0 MWh");
    const outageDownload = page.waitForEvent("download");
    await page
        .getByRole("button", { name: "Download both scenarios CSV" })
        .click();
    const outageCsv = Papa.parse<Record<string, string>>(
        await readFile((await (await outageDownload).path())!, "utf8"),
        { header: true, skipEmptyLines: true },
    );
    expect(outageCsv.errors).toEqual([]);
    expect(outageCsv.data).toHaveLength(48);
    expect(
        outageCsv.data.some((row) => Number(row.normal_mean_power_mw) > 0),
    ).toBe(true);
    expect(
        outageCsv.data.every(
            (row) =>
                row.affected_mean_power_mw === "0" &&
                row.affected_energy_mwh === "0",
        ),
    ).toBe(true);
    expect(outageCsv.data[0]["outage_scenario.incidents"]).toContain(
        "Browser inverter outage",
    );
    await page.screenshot({
        path: testInfo.outputPath("plant-outage-comparison-desktop.png"),
        fullPage: true,
    });
    await page.getByRole("button", { name: "Outages", exact: true }).click();
    await page
        .getByRole("button", { name: "Resolve outage", exact: true })
        .click();
    await page
        .getByLabel("Resolved at (device time)", { exact: true })
        .fill(outageTimes.ends);
    await page.getByRole("button", { name: "Save resolution" }).click();
    await expect(page.getByText("resolved", { exact: true })).toBeVisible();
    await page.getByRole("button", { name: "Forecast", exact: true }).click();
    await expect(
        page.getByText("Estimated energy", { exact: true }),
    ).toBeVisible();
    await expect(
        page.getByRole("button", { name: "Download both scenarios CSV" }),
    ).toHaveCount(0);
    await expectFullForecastCsv(48);
    await page.getByLabel("Forecast date").fill(process.env.BROWSER_TODAY!);
    await page.getByText("Source & quality", { exact: true }).click();
    await expect(
        page.getByText(
            `Model: reference-power-transfer-v1. Configuration ${createdPlant.current_configuration.id}.`,
            { exact: false },
        ),
    ).toBeVisible();
    await expect(
        page.getByText("View interval data (48 intervals)"),
    ).toBeVisible();
    await expect(
        page.getByRole("heading", { name: "No configuration for this day" }),
    ).toHaveCount(0);
    await expect(page.getByText("configuration_unavailable")).toHaveCount(0);
    await page
        .getByLabel("Forecast date")
        .fill(process.env.BROWSER_HISTORY_DATE!);
    await expect(
        page.getByText("Satellite/model estimates, not meter readings."),
    ).toBeVisible();
    await expect(
        page.getByText("View interval data (24 intervals)"),
    ).toBeVisible();
    await page.screenshot({
        path: testInfo.outputPath("plant-history-desktop.png"),
        fullPage: true,
    });
    await page.getByLabel("Forecast date").fill(process.env.BROWSER_DATE!);
    await expect(
        page.getByText("View interval data (24 intervals)"),
    ).toBeVisible();

    await page.getByRole("button", { name: "Production", exact: true }).click();
    await expect(page.getByLabel("EPİAŞ plant ID")).toHaveCount(0);
    await expect(
        page.getByRole("button", {
            name: /Connect EPİAŞ|Edit connection|Disconnect collection/,
        }),
    ).toHaveCount(0);
    await page
        .getByLabel("Day", { exact: true })
        .fill(process.env.BROWSER_HISTORY_DATE!);
    const productionTable = page.getByRole("table", {
        name: "Production intervals · Europe/Istanbul",
    });
    const firstHour = productionTable.getByRole("row").filter({
        has: page.getByRole("rowheader", {
            name: "00:00–01:00",
            exact: true,
        }),
    });
    async function saveProduction(value: string) {
        await firstHour
            .getByRole("button", { name: "Edit 00:00–01:00", exact: true })
            .click();
        await page
            .getByLabel("Production for 00:00–01:00 (MWh)", { exact: true })
            .fill(value);
        const saved = page.waitForResponse(
            (response) =>
                response.request().method() === "POST" &&
                response.url().endsWith("/production/changes/"),
        );
        await page.getByRole("button", { name: "Save", exact: true }).click();
        expect((await saved).status()).toBe(200);
        await expect(
            page.getByLabel("Production for 00:00–01:00 (MWh)", {
                exact: true,
            }),
        ).toHaveCount(0);
    }
    await saveProduction("1.25");
    await expect(firstHour).toContainText("1.25");
    await saveProduction("2.5");
    await expect(firstHour).toContainText("2.5");
    await firstHour
        .getByRole("button", { name: "Edit 00:00–01:00", exact: true })
        .click();
    const reverted = page.waitForResponse(
        (response) =>
            response.request().method() === "POST" &&
            response.url().endsWith("/production/changes/"),
    );
    await page
        .getByRole("button", {
            name: "Use provider value for 00:00–01:00",
            exact: true,
        })
        .click();
    expect((await reverted).status()).toBe(200);
    await expect(firstHour).not.toContainText("2.5");
    await saveProduction("1.25");
    await page.screenshot({
        path: testInfo.outputPath("production-desktop.png"),
        fullPage: true,
    });
    await page.setViewportSize({ width: 390, height: 844 });
    await expect(firstHour).toContainText("1.25");
    expect(
        await page.evaluate(
            () => document.documentElement.scrollWidth <= window.innerWidth,
        ),
    ).toBe(true);
    await page
        .locator(".production-workspace")
        .screenshot({ path: testInfo.outputPath("production-mobile.png") });
    const productionA11y = await new AxeBuilder({ page })
        .withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"])
        .analyze();
    expect(productionA11y.violations).toEqual([]);
    await page.setViewportSize({ width: 1440, height: 1100 });
    await page.reload();
    await page.getByRole("button", { name: "Production", exact: true }).click();
    await page
        .getByLabel("Day", { exact: true })
        .fill(process.env.BROWSER_HISTORY_DATE!);
    await expect(firstHour).toContainText("1.25");

    await page.getByRole("button", { name: "Settings", exact: true }).click();
    await page
        .getByRole("button", { name: "Data collection", exact: true })
        .click();
    await page.getByLabel("Collection time 1 (Europe/Istanbul)").fill("09:00");
    await page.getByRole("button", { name: "+ Add time" }).click();
    await page.getByLabel("Collection time 2 (Europe/Istanbul)").fill("16:00");
    await page
        .getByRole("button", { name: "Save collection schedule" })
        .click();
    await expect(
        page.getByLabel("Collection time 1 (Europe/Istanbul)"),
    ).toHaveValue("09:00");
    await expect(
        page.getByLabel("Collection time 2 (Europe/Istanbul)"),
    ).toHaveValue("16:00");
    await page.getByRole("button", { name: "Email reminders" }).click();
    await page.getByLabel("Enable forecast emails for this plant").check();
    await page.getByLabel("Delivery time 1", { exact: true }).fill("10:00");
    await page.getByLabel("Email address 1", { exact: true }).fill("");
    await page.getByRole("button", { name: "Save reminders" }).click();
    await expect(
        page.getByLabel("Email address 1", { exact: true }),
    ).toHaveValue("member@example.com");
    await expect(
        page.getByLabel("Delivery time 1", { exact: true }),
    ).toHaveValue("10:00");
    await expect(page.getByText("Enabled", { exact: true })).toBeVisible();
    await page
        .getByLabel("Email address 1", { exact: true })
        .fill("forecast-one@example.test");
    await page.getByRole("button", { name: "Add address" }).click();
    await page
        .getByLabel("Email address 2", { exact: true })
        .fill("forecast-two@example.test");
    const remindersSaved = page.waitForResponse(
        (response) =>
            response.request().method() === "PUT" &&
            response.url().endsWith("/reminders/"),
    );
    await page.getByRole("button", { name: "Save reminders" }).click();
    expect((await remindersSaved).ok()).toBeTruthy();
    await page.reload();
    await page.getByRole("button", { name: "Settings", exact: true }).click();
    await page
        .getByRole("button", { name: "Email reminders", exact: true })
        .click();
    await expect(
        page.getByLabel("Email address 1", { exact: true }),
    ).toHaveValue("forecast-one@example.test");
    await expect(
        page.getByLabel("Email address 2", { exact: true }),
    ).toHaveValue("forecast-two@example.test");
    await expect(
        page.getByLabel("Delivery time 1", { exact: true }),
    ).toHaveValue("10:00");
    await page.setViewportSize({ width: 390, height: 844 });
    await page.screenshot({
        path: testInfo.outputPath("reminders-mobile.png"),
        fullPage: true,
    });
    expect(
        await page.evaluate(
            () => document.documentElement.scrollWidth <= window.innerWidth,
        ),
    ).toBe(true);
    const mobileA11y = await new AxeBuilder({ page })
        .withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"])
        .analyze();
    expect(
        mobileA11y.violations.filter(
            (item) => item.impact === "critical" || item.impact === "serious",
        ),
    ).toEqual([]);
    await page.setViewportSize({ width: 1280, height: 900 });
    const plantDeleted = page.waitForResponse(
        (response) =>
            response.request().method() === "DELETE" &&
            /\/plants\/\d+\/$/.test(new URL(response.url()).pathname),
    );
    const childDeleteStatus = await page.evaluate(async (url) => {
        const session = await (await fetch("/api/session/")).json();
        const response = await fetch(url, {
            method: "DELETE",
            headers: { "X-CSRFToken": session.csrf_token },
        });
        return response.status;
    }, plantUrl);
    expect(childDeleteStatus).toBe(204);
    await page.getByRole("button", { name: "All plants" }).click();
    expect((await plantDeleted).ok()).toBeTruthy();
    await expect(
        page.getByRole("button", { name: /Browser plant/ }),
    ).toHaveCount(0);
    await page
        .getByRole("button", { name: "Recycle bin", exact: true })
        .click();
    await page.getByRole("button", { name: "Restore Browser plant" }).click();
    await expect(
        page.getByText("Browser plant restored. Find it in Plants."),
    ).toBeVisible();
    await page
        .getByRole("navigation", { name: "Main navigation" })
        .getByRole("button", { name: "Plants", exact: true })
        .click();
    await page.getByRole("button", { name: /Browser plant/ }).click();
    await page.getByRole("button", { name: "Settings", exact: true }).click();
    await page
        .getByRole("button", { name: "Delete plant", exact: true })
        .click();
    const siteDeleted = page.waitForResponse(
        (response) =>
            response.request().method() === "DELETE" &&
            new URL(response.url()).pathname ===
                `/api/tenants/${tenantId}/regions/${createdPlant.region_id}/`,
    );
    await page
        .getByRole("button", { name: "Move plant to recycle bin" })
        .click();
    expect((await siteDeleted).status()).toBe(204);
    await expect(page.getByRole("heading", { name: "Plants" })).toBeVisible();
    await expect(
        page.getByRole("button", { name: /Browser plant/ }),
    ).toHaveCount(0);
    await page
        .getByRole("button", { name: "Recycle bin", exact: true })
        .click();
    await expect(
        page.getByRole("button", { name: "Restore Site" }),
    ).toBeVisible();
    await page.screenshot({
        path: testInfo.outputPath("recycle-bin-desktop.png"),
        fullPage: true,
    });
    await page.getByRole("button", { name: "Restore Site" }).click();
    await expect(
        page.getByText("Site restored. Find it in Plants."),
    ).toBeVisible();
    await page
        .getByRole("navigation", { name: "Main navigation" })
        .getByRole("button", { name: "Plants", exact: true })
        .click();
    await page.getByRole("button", { name: /Browser plant/ }).click();
    await page.getByRole("button", { name: "Production", exact: true }).click();
    await page
        .getByLabel("Day", { exact: true })
        .fill(process.env.BROWSER_HISTORY_DATE!);
    await expect(firstHour).toContainText("1.25");
    await page.getByRole("button", { name: "Settings", exact: true }).click();
    await page.getByLabel("Mounting type").selectOption("fixed");
    await page.getByLabel("Surface tilt (degrees, optional)").fill("33");
    await page.getByLabel("Surface azimuth (degrees, optional)").fill("180");
    const configurationSaved = page.waitForResponse(
        (response) =>
            response.request().method() === "PUT" &&
            new URL(response.url()).pathname === plantUrl,
    );
    await page.getByRole("button", { name: "Save configuration" }).click();
    expect((await configurationSaved).ok()).toBeTruthy();
    await expect(
        page.getByText("Saved successfully.", { exact: true }),
    ).toBeVisible();
    await page.reload();
    await expect(page.getByLabel("Mounting type")).toHaveValue("fixed");
    await expect(
        page.getByLabel("Surface tilt (degrees, optional)"),
    ).toHaveValue("33");
    await expect(
        page.getByLabel("Surface azimuth (degrees, optional)"),
    ).toHaveValue("180");
    await page.getByRole("button", { name: "Production", exact: true }).click();
    await page.getByRole("button", { name: "Comparison", exact: true }).click();
    await expect(
        page.getByRole("heading", { name: "Production comparison" }),
    ).toBeVisible();
    await expect(page.getByText("Loading performance…")).toHaveCount(0);
    await page.screenshot({
        path: testInfo.outputPath("forecast-performance-desktop.png"),
        fullPage: true,
    });
});
