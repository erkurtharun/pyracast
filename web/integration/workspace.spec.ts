import { expect, test } from "@playwright/test";
import { readFile } from "node:fs/promises";
import Papa from "papaparse";
import { accessibilityViolations } from "../tests/support/accessibility";

test("a customer manages one plant site through the real API", async ({
    page,
}, testInfo) => {
    test.setTimeout(60_000);
    await page.clock.setFixedTime(new Date(process.env.BROWSER_NOW!));
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
    await expect(
        page.getByRole("heading", { name: "Plants", exact: true }),
    ).toBeVisible();

    const forbidden = await page.evaluate(
        async ({ tenant, region }) => {
            const response = await fetch(
                `/api/tenants/${tenant}/regions/${region}/forecast/`,
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
    expect(forbidden).toBe(403);

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
        page.getByRole("heading", { name: "Irradiance forecast" }),
    ).toBeVisible();
    await expect(
        page.getByRole("img", { name: "Irradiance (W/m²)", exact: true }),
    ).toBeVisible();
    await expect(page.getByText("kWh/m²").first()).toBeVisible();
    await page.screenshot({
        path: testInfo.outputPath("region-desktop.png"),
        fullPage: true,
    });
    const regionA11y = await accessibilityViolations(page);
    expect(
        regionA11y.filter(
            (item) => item.impact === "critical" || item.impact === "serious",
        ),
    ).toEqual([]);

    await page
        .getByLabel("Forecast date")
        .fill(process.env.BROWSER_HISTORY_DATE!);
    await expect(
        page.getByRole("heading", {
            name: "Irradiance calculated from satellite data",
        }),
    ).toBeVisible();
    await page.getByRole("button", { name: "Generation", exact: true }).click();
    await page.getByLabel("Forecast date").fill(process.env.BROWSER_DATE!);
    await page.getByLabel("Resolution").selectOption("60");
    async function expectTableRows(date: string, name: string, rows: number) {
        const panel = page
            .locator("section.panel")
            .filter({ has: page.getByText(date, { exact: true }) });
        const toggle = panel.getByText("Show table", { exact: true });
        await toggle.click();
        await expect(
            panel.getByRole("table", { name }).getByRole("row"),
        ).toHaveCount(rows + 1);
        await toggle.click();
    }
    await expectTableRows(process.env.BROWSER_DATE!, "Hourly values", 12);
    await page.getByText("Show table", { exact: true }).click();
    await expect(
        page
            .getByRole("table", { name: "Hourly values" })
            .getByRole("columnheader"),
    ).toHaveText(["Time", "Forecast (MWh)"]);
    await page.getByText("Show table", { exact: true }).click();
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
        maximumFractionDigits: 2,
    }).format(hourlyDaylight.energy_mwh);
    await expect(
        page.locator(".stat").filter({ hasText: /^Daily total/ }),
    ).toContainText(`${daylightEnergy} MWh`);
    await expect(page.locator(".stat")).toHaveCount(1);
    async function expectFullForecastCsv(fullCount: 24 | 48) {
        const download = page.waitForEvent("download");
        await page
            .getByRole("button", { name: "Download CSV", exact: true })
            .first()
            .click();
        const bytes = await readFile(await (await download).path());
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
        expect(parsed.data[0]?.mean_power_mw).toBe("0");
        expect(parsed.data.at(-1)!.mean_power_mw).toBe("0");
        const localTime = (stamp: string) =>
            new Intl.DateTimeFormat("en-GB", {
                timeZone: "Europe/Istanbul",
                hour: "2-digit",
                minute: "2-digit",
                hourCycle: "h23",
            }).format(new Date(stamp));
        expect(parsed.data[0]?.timezone).toBe("Europe/Istanbul");
        expect(localTime(parsed.data[0]?.starts_at ?? "")).toBe("00:00");
        expect(localTime(parsed.data.at(-1)?.ends_at ?? "")).toBe("00:00");
        expect(localTime(positive[0]?.starts_at ?? "")).toBe("06:00");
        expect(localTime(positive.at(-1)?.ends_at ?? "")).toBe("18:00");
    }
    await expectFullForecastCsv(24);
    await page.getByLabel("Resolution").selectOption("30");
    await expectTableRows(process.env.BROWSER_DATE!, "Half-hourly values", 24);
    expect((await daylightForecast(30)).energy_mwh).toBe(
        hourlyDaylight.energy_mwh,
    );
    await expect(
        page.locator(".stat").filter({ hasText: /^Daily total/ }),
    ).toContainText(`${daylightEnergy} MWh`);
    await expectFullForecastCsv(48);
    // Outage times are entered on the plant's wall clock, whatever zone the browser runs in.
    const outageTimes = await page.evaluate(() => {
        const plantInput = (minutesAgo: number) =>
            new Intl.DateTimeFormat("sv-SE", {
                timeZone: "Europe/Istanbul",
                year: "numeric",
                month: "2-digit",
                day: "2-digit",
                hour: "2-digit",
                minute: "2-digit",
                hourCycle: "h23",
            })
                .format(new Date(Date.now() - minutesAgo * 60_000))
                .replace(" ", "T");
        return {
            starts: plantInput(10),
            edited: plantInput(9),
            ends: plantInput(5),
        };
    });
    await page.getByRole("button", { name: "Outages", exact: true }).click();
    await page
        .getByRole("button", { name: "Report outage", exact: true })
        .click();
    await page.getByLabel("Incident title").fill("Browser inverter outage");
    await page
        .getByLabel("Started at", { exact: true })
        .fill(outageTimes.starts);
    await page
        .getByRole("button", { name: "Save outage", exact: true })
        .click();
    await expect(
        page.getByRole("heading", { name: "Browser inverter outage" }),
    ).toBeVisible();
    await page.getByRole("button", { name: "Forecast", exact: true }).click();
    await expect(
        page.locator(".stat").getByText("Without outage", { exact: true }),
    ).toBeVisible();
    await expect(
        page.locator(".stat").getByText("With outage", { exact: true }),
    ).toBeVisible();
    await expect(
        page.locator(".stat").filter({ hasText: /^Without outage/ }),
    ).toContainText(`${daylightEnergy} MWh`);
    await expect(
        page
            .locator(".stat")
            .filter({ hasText: /^With outage/ })
            .locator("strong"),
    ).toHaveText("0 MWh");
    const outageDownload = page.waitForEvent("download");
    await page
        .getByRole("button", { name: "Download CSV", exact: true })
        .first()
        .click();
    const outageCsv = Papa.parse<Record<string, string>>(
        await readFile(await (await outageDownload).path(), "utf8"),
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
    expect(outageCsv.data[0]).toMatchObject({
        plant_name: "Browser plant",
        timezone: "Europe/Istanbul",
    });
    await page.screenshot({
        path: testInfo.outputPath("plant-outage-comparison-desktop.png"),
        fullPage: true,
    });
    await page.getByRole("button", { name: "Outages", exact: true }).click();
    await page
        .getByRole("button", { name: "Edit report", exact: true })
        .click();
    await page
        .getByLabel("Started at", { exact: true })
        .fill(outageTimes.edited);
    const outageList = (await page.evaluate(async (url) => {
        const response = await fetch(`${url}outages/`);
        return response.json();
    }, plantUrl)) as {
        results: { id: number; revision: number; starts_at: string }[];
    };
    const reported = outageList.results[0]!;
    const changedMeanwhile = await page.evaluate(
        async ({ url, outage }) => {
            const session = await (await fetch("/api/session/")).json();
            const response = await fetch(`${url}outages/${outage.id}/`, {
                method: "PUT",
                headers: {
                    "Content-Type": "application/json",
                    "X-CSRFToken": session.csrf_token,
                },
                body: JSON.stringify({
                    kind: "inverter",
                    title: "Inverter outage changed elsewhere",
                    starts_at: outage.starts_at,
                    expected_revision: outage.revision,
                }),
            });
            return response.status;
        },
        { url: plantUrl, outage: reported },
    );
    expect(changedMeanwhile).toBe(200);
    const stale = page.waitForResponse(
        (response) =>
            response.request().method() === "PUT" &&
            response.url().endsWith(`/outages/${reported.id}/`),
    );
    await page.getByRole("button", { name: "Save outage changes" }).click();
    expect((await stale).status()).toBe(409);
    await expect(
        page.getByText(
            "Title: Browser inverter outage → Inverter outage changed elsewhere",
        ),
    ).toBeVisible();
    await expect(page.getByLabel("Started at", { exact: true })).toHaveValue(
        outageTimes.edited,
    );
    const applied = page.waitForResponse(
        (response) =>
            response.request().method() === "PUT" &&
            response.url().endsWith(`/outages/${reported.id}/`),
    );
    await page.getByRole("button", { name: "Save outage changes" }).click();
    expect((await applied).status()).toBe(200);
    await expect(
        page.getByRole("heading", { name: "Browser inverter outage" }),
    ).toBeVisible();
    await page
        .getByRole("button", { name: "Resolve outage", exact: true })
        .click();
    await page
        .getByLabel("Resolved at", { exact: true })
        .fill(outageTimes.ends);
    await page.getByRole("button", { name: "Save resolution" }).click();
    await expect(page.getByText("Resolved", { exact: true })).toBeVisible();
    await page.getByRole("button", { name: "Forecast", exact: true }).click();
    await expect(
        page.locator(".stat").getByText("Daily total", { exact: true }),
    ).toBeVisible();
    await expect(
        page.locator(".stat").getByText("With outage", { exact: true }),
    ).toHaveCount(0);
    await expectFullForecastCsv(48);
    await page.getByLabel("Forecast date").fill(process.env.BROWSER_TODAY!);
    await expect(page.getByText(/^Forecast data: /)).toBeVisible();
    await expect(page.getByText(/^Model: /)).toHaveCount(0);
    await expectTableRows(process.env.BROWSER_TODAY!, "Half-hourly values", 48);
    await expect(
        page.getByRole("heading", {
            name: "No technical details for this day.",
        }),
    ).toHaveCount(0);
    await expect(page.getByText("configuration_unavailable")).toHaveCount(0);
    await page
        .getByLabel("Forecast date")
        .fill(process.env.BROWSER_HISTORY_DATE!);
    await expect(
        page.getByRole("heading", {
            name: "Production calculated from satellite data",
        }),
    ).toBeVisible();
    await expectTableRows(
        process.env.BROWSER_HISTORY_DATE!,
        "Half-hourly values",
        24,
    );
    await page.screenshot({
        path: testInfo.outputPath("plant-history-desktop.png"),
        fullPage: true,
    });
    await page.getByLabel("Forecast date").fill(process.env.BROWSER_DATE!);
    await expectTableRows(process.env.BROWSER_DATE!, "Half-hourly values", 24);

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
    await page.getByRole("checkbox", { name: "Show full day" }).check();
    const productionTable = page.getByRole("table", {
        name: "Production intervals",
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
            name: "Remove correction for 00:00–01:00",
            exact: true,
        })
        .click();
    expect((await reverted).status()).toBe(200);
    await expect(firstHour).not.toContainText("2.5");
    await saveProduction("1.25");
    const productionDownload = page.waitForEvent("download");
    await page
        .getByRole("button", { name: "Download CSV", exact: true })
        .click();
    const exportedProduction = await readFile(
        await (await productionDownload).path(),
    );
    await page.getByRole("button", { name: "Import CSV" }).click();
    await page.getByLabel("Production CSV").setInputFiles({
        name: "production-export.csv",
        mimeType: "text/csv",
        buffer: exportedProduction,
    });
    await expect(page.getByText(/1 valid observations ready/)).toBeVisible();
    await page
        .getByRole("button", { name: "Preview import", exact: true })
        .click();
    await page.getByRole("button", { name: "Confirm import (1)" }).click();
    await expect(
        page.getByText(
            "Import complete: 0 created, 1 already present, 1 total.",
        ),
    ).toBeVisible();
    await page.getByRole("button", { name: "Import CSV" }).click();
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
    const productionA11y = await accessibilityViolations(page);
    expect(productionA11y).toEqual([]);
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
    await page.getByLabel("Collection time 1", { exact: true }).fill("09:00");
    await page.getByRole("button", { name: "+ Add time" }).click();
    await page.getByLabel("Collection time 2", { exact: true }).fill("16:00");
    await page
        .getByRole("button", { name: "Save collection schedule" })
        .click();
    await expect(
        page.getByLabel("Collection time 1", { exact: true }),
    ).toHaveValue("09:00");
    await expect(
        page.getByLabel("Collection time 2", { exact: true }),
    ).toHaveValue("16:00");
    await page.getByRole("button", { name: "Email reminders" }).click();
    const reminders = page.getByRole("region", {
        name: "Email reminders",
        exact: true,
    });
    const savedReminders = page
        .getByRole("heading", { name: "Who sees what", exact: true })
        .locator("xpath=..");
    const reviewDialog = page.getByRole("dialog", {
        name: "Show these addresses to every recipient?",
        exact: true,
    });
    async function saveShownReminders(hidden: string) {
        const previewed = page.waitForResponse(
            (response) =>
                response.request().method() === "POST" &&
                response.url().endsWith("/reminders/preview/"),
        );
        await page
            .getByRole("button", { name: "Save reminders", exact: true })
            .click();
        const preview = await previewed;
        expect(preview.status()).toBe(200);
        const { disclosure } = (await preview.json()) as { disclosure: string };
        await expect(reviewDialog).toBeVisible();
        await expect(
            reviewDialog.getByText(hidden, { exact: true }),
        ).toBeVisible();
        await expect(
            reviewDialog.getByText("No address will appear in To or Copy.", {
                exact: true,
            }),
        ).toBeVisible();
        const saved = page.waitForResponse(
            (response) =>
                response.request().method() === "PUT" &&
                response.url().endsWith("/reminders/"),
        );
        const reread = page.waitForResponse(
            (response) =>
                response.request().method() === "GET" &&
                response.url().endsWith("/reminders/"),
        );
        await reviewDialog
            .getByRole("button", { name: "Save and show addresses" })
            .click();
        const write = await saved;
        expect(write.status()).toBe(200);
        expect(write.request().postDataJSON()).toMatchObject({ disclosure });
        expect((await reread).status()).toBe(200);
        await expect(reviewDialog).toHaveCount(0);
        await expect(
            reminders.getByText("Saved successfully.", { exact: true }),
        ).toBeVisible();
    }
    await page.getByLabel("Enable forecast emails for this plant").check();
    await page.getByLabel("Delivery time 1", { exact: true }).fill("10:00");
    await page
        .getByLabel("Email address 1", { exact: true })
        .fill("member@example.com");
    await saveShownReminders("Hidden (Bcc): 1 recipient");
    await expect(
        page.getByLabel("Email address 1", { exact: true }),
    ).toHaveValue("member@example.com");
    await expect(
        page.getByLabel("Delivery time 1", { exact: true }),
    ).toHaveValue("10:00");
    await expect(reminders.getByText("Sending", { exact: true })).toBeVisible();
    await expect(savedReminders).toContainText("1 / 5");
    await expect(savedReminders).toContainText("Hidden (Bcc): 1 recipient");
    await page
        .getByLabel("Email address 1", { exact: true })
        .fill("forecast-one@example.test");
    await page.getByRole("button", { name: "Add address" }).click();
    await page
        .getByLabel("Email address 2", { exact: true })
        .fill("forecast-two@example.test");
    await saveShownReminders("Hidden (Bcc): 2 recipients");
    await expect(
        page.getByLabel("Email address 1", { exact: true }),
    ).toHaveValue("forecast-one@example.test");
    await expect(
        page.getByLabel("Email address 2", { exact: true }),
    ).toHaveValue("forecast-two@example.test");
    await expect(savedReminders).toContainText("2 / 5");
    await expect(savedReminders).toContainText("Hidden (Bcc): 2 recipients");
    await expect(
        savedReminders.getByText(
            "All recipients are hidden: the email shows no recipient address.",
            { exact: true },
        ),
    ).toBeVisible();
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
    const mobileA11y = await accessibilityViolations(page);
    expect(
        mobileA11y.filter(
            (item) => item.impact === "critical" || item.impact === "serious",
        ),
    ).toEqual([]);
    await page.setViewportSize({ width: 1280, height: 900 });
    const childDeleteStatus = await page.evaluate(async (url) => {
        const session = await (await fetch("/api/session/")).json();
        const response = await fetch(url, {
            method: "DELETE",
            headers: { "X-CSRFToken": session.csrf_token },
        });
        return response.status;
    }, plantUrl);
    expect(childDeleteStatus).toBe(405);
    await page
        .getByRole("button", { name: "← All plants", exact: true })
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
    await expect(
        page.getByRole("heading", { name: "Plants", exact: true }),
    ).toBeVisible();
    await expect(
        page.getByRole("button", { name: /Browser plant/ }),
    ).toHaveCount(0);
    await page
        .getByRole("button", { name: "Recycle bin", exact: true })
        .click();
    await expect(
        page.getByRole("button", { name: "Restore Browser plant" }),
    ).toBeVisible();
    await page.screenshot({
        path: testInfo.outputPath("recycle-bin-desktop.png"),
        fullPage: true,
    });
    await page.getByRole("button", { name: "Restore Browser plant" }).click();
    await expect(page.getByText("Browser plant restored.")).toBeVisible();
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
