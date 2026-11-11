import { expect, test } from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";
import { readFile } from "node:fs/promises";
import Papa from "papaparse";

test("a customer manages an approved region through the real API", async ({
    page,
}, testInfo) => {
    test.setTimeout(60_000);
    await page.goto("/");
    await expect(page.getByLabel("Username")).toBeVisible();
    await page.screenshot({
        path: testInfo.outputPath("login-desktop.png"),
        fullPage: true,
    });
    await page.getByLabel("Username").fill(process.env.BROWSER_USERNAME!);
    await page.getByLabel("Password").fill(process.env.BROWSER_PASSWORD!);
    await page.getByRole("button", { name: /sign in/i }).click();
    await expect(page.getByRole("heading", { name: "Overview" })).toBeVisible();

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
    await page.getByLabel("Forecast date").fill(process.env.BROWSER_DATE!);
    await expect(
        page.getByRole("heading", { name: "Regional irradiance" }),
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
    await page.getByRole("button", { name: "Solar plants" }).click();
    await page.getByRole("button", { name: "+ Add a plant" }).click();
    await page.getByLabel("Plant name").fill("Browser plant");
    await page.getByLabel("DC nameplate capacity (MW)").fill("130");
    await page.getByLabel("Inverter AC capacity (MW, optional)").fill("100");
    await page.getByRole("button", { name: "Create plant" }).click();
    await expect(
        page.getByRole("heading", { name: "Browser plant" }),
    ).toBeVisible();
    await expect(page.getByLabel("Forecast date")).toHaveValue(
        process.env.BROWSER_DATE!,
    );
    await page.getByLabel("Resolution").selectOption("60");
    await expect(
        page.getByText("View interval data (12 intervals)"),
    ).toBeVisible();
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
        // FixtureProvider supplies positive power from 06:00 through 18:00 local.
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
    await page
        .getByRole("button", { name: "Generation forecast", exact: true })
        .click();
    await expect(
        page.getByText("Normal capacity · daily energy", { exact: true }),
    ).toBeVisible();
    await expect(
        page.getByText("With outages · daily energy", { exact: true }),
    ).toBeVisible();
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
    await page
        .getByRole("button", { name: "Generation forecast", exact: true })
        .click();
    await expect(page.getByText("Daily energy", { exact: true })).toBeVisible();
    await expect(
        page.getByRole("button", { name: "Download both scenarios CSV" }),
    ).toHaveCount(0);
    await expectFullForecastCsv(48);
    await page.getByLabel("Forecast date").fill(process.env.BROWSER_TODAY!);
    await page.getByText("Source & quality", { exact: true }).click();
    await expect(
        page.getByText(
            /Model: reference-power-transfer-v1\. Configuration 1\./,
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

    await page.getByRole("button", { name: "Automatic production" }).click();
    await expect(page.getByLabel("EPİAŞ plant ID")).toBeVisible();
    await expect(page.getByLabel(/Measurement point/)).toHaveValue("unknown");
    await page
        .getByLabel("Archive date")
        .fill(process.env.BROWSER_HISTORY_DATE!);
    await expect(
        page.getByText("No EPİAŞ data has been archived for this day."),
    ).toBeVisible();
    await page
        .getByText("Sources and calibration eligibility", { exact: true })
        .click();
    await expect(
        page.getByText(
            "Reported production can support provisional calibration; UEVM matching net export is preferred.",
            {
                exact: false,
            },
        ),
    ).toBeVisible();

    await page
        .getByRole("button", { name: "Calibration", exact: true })
        .click();
    await expect(
        page.getByRole("heading", { name: "Production journal" }),
    ).toBeVisible();
    await expect(
        page.getByRole("button", { name: "Request calibration" }),
    ).toHaveCount(0);
    await page
        .getByLabel("Day", { exact: true })
        .fill(process.env.BROWSER_HISTORY_DATE!);
    const meterEntries = page.getByRole("table", { name: "Meter entries" });
    await expect(meterEntries.getByRole("spinbutton").first()).toBeEnabled();
    await meterEntries.getByRole("spinbutton").first().fill("1250");
    await page.getByRole("button", { name: "Save meter readings" }).click();
    await expect(page.getByText("1 meter reading saved.")).toBeVisible();
    await expect(
        meterEntries.getByText("1.25 MWh", { exact: true }),
    ).toBeVisible();
    const historyDate = new Date(
        `${process.env.BROWSER_HISTORY_DATE!}T12:00:00Z`,
    );
    historyDate.setUTCDate(historyDate.getUTCDate() - 10);
    const missingDate = historyDate.toISOString().slice(0, 10);
    await page.getByLabel("Day", { exact: true }).fill(missingDate);
    await expect(
        page.getByText(
            "No comparison data or saved meter intervals are available for this day.",
        ),
    ).toBeVisible();
    await expect(meterEntries.getByRole("spinbutton")).toHaveCount(0);
    await expect(
        page.getByRole("button", { name: "Save meter readings" }),
    ).toBeDisabled();
    await page
        .getByLabel("Day", { exact: true })
        .fill(process.env.BROWSER_HISTORY_DATE!);
    await expect(
        meterEntries.getByText("1.25 MWh", { exact: true }),
    ).toBeVisible();
    await page.screenshot({
        path: testInfo.outputPath("production-journal-desktop.png"),
        fullPage: true,
    });
    await page.setViewportSize({ width: 390, height: 844 });
    expect(
        await page
            .locator(".journal")
            .evaluate((element) => element.scrollWidth <= element.clientWidth),
    ).toBe(true);
    await page.locator(".journal").screenshot({
        path: testInfo.outputPath("production-journal-mobile.png"),
    });
    await page.setViewportSize({ width: 1440, height: 1100 });
    const journalA11y = await new AxeBuilder({ page })
        .withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"])
        .analyze();
    expect(
        journalA11y.violations.filter(
            (item) => item.impact === "critical" || item.impact === "serious",
        ),
    ).toEqual([]);
    await page.reload();
    await page.getByRole("button", { name: /Site/ }).click();
    await page.getByRole("button", { name: "Solar plants" }).click();
    await page.getByRole("button", { name: /Browser plant/ }).click();
    await page
        .getByRole("button", { name: "Calibration", exact: true })
        .click();
    await expect(
        page
            .getByRole("table", { name: "Meter entries" })
            .getByText("1.25 MWh", { exact: true }),
    ).toBeVisible();

    await page.getByRole("button", { name: /all plants/i }).click();
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
    await page.getByLabel("Enable forecast emails for this region").check();
    await page.getByLabel(/Delivery times/).fill("10:00");
    await page.getByLabel("Email address (optional override)").fill("");
    await page.getByRole("button", { name: "Save reminders" }).click();
    await expect(page.getByText(/member@example.com/)).toBeVisible();
    await expect(page.getByLabel(/Delivery times/)).toHaveValue("10:00");
    await expect(page.getByText("Enabled", { exact: true })).toBeVisible();
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
    await page
        .getByRole("button", { name: "Solar plants", exact: true })
        .click();
    await page.getByRole("button", { name: /Browser plant/ }).click();
    await page.getByText("Plant actions", { exact: true }).click();
    await page
        .getByRole("button", { name: "Delete plant", exact: true })
        .click();
    const plantDeleted = page.waitForResponse(
        (response) =>
            response.request().method() === "DELETE" &&
            /\/plants\/\d+\/$/.test(new URL(response.url()).pathname),
    );
    await page
        .getByRole("button", { name: "Move plant to recycle bin" })
        .click();
    expect((await plantDeleted).ok()).toBeTruthy();
    await expect(
        page.getByRole("button", { name: /Browser plant/ }),
    ).toHaveCount(0);
    await page
        .getByRole("button", { name: "Recycle bin", exact: true })
        .click();
    await page.getByRole("button", { name: "Restore Browser plant" }).click();
    await expect(
        page.getByText("Browser plant restored. Find it in Overview."),
    ).toBeVisible();
    await page.getByRole("button", { name: "Overview", exact: true }).click();
    await page.getByRole("button", { name: /Site/ }).click();
    await page
        .getByRole("button", { name: "Delete region", exact: true })
        .click();
    await page
        .getByRole("button", { name: "Move region to recycle bin" })
        .click();
    await expect(page.getByRole("button", { name: /Site/ })).toHaveCount(0);
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
        page.getByText("Site restored. Find it in Overview."),
    ).toBeVisible();
    await page.getByRole("button", { name: "Overview", exact: true }).click();
    await page.getByRole("button", { name: /Site/ }).click();
    await page
        .getByRole("button", { name: "Solar plants", exact: true })
        .click();
    await page.getByRole("button", { name: /Browser plant/ }).click();
    await page
        .getByRole("button", { name: "Calibration", exact: true })
        .click();
    await expect(
        page
            .getByRole("table", { name: "Meter entries" })
            .getByText("1.25 MWh", { exact: true }),
    ).toBeVisible();
    await page.getByRole("button", { name: "Edit configuration" }).click();
    await page.getByLabel("Mounting type").selectOption("fixed");
    await page.getByLabel("Surface tilt (degrees, optional)").fill("33");
    await page.getByLabel("Surface azimuth (degrees, optional)").fill("180");
    await page.getByRole("button", { name: "Save configuration" }).click();
    await expect(
        page.getByRole("heading", { name: "Edit plant configuration" }),
    ).toHaveCount(0);
    await page.getByRole("button", { name: "Edit configuration" }).click();
    await expect(page.getByLabel("Mounting type")).toHaveValue("fixed");
    await expect(
        page.getByLabel("Surface tilt (degrees, optional)"),
    ).toHaveValue("33");
    await expect(
        page.getByLabel("Surface azimuth (degrees, optional)"),
    ).toHaveValue("180");
    await page.getByRole("button", { name: "Close configuration" }).click();
    await page
        .getByRole("button", { name: "Generation forecast", exact: true })
        .click();
    await expect(
        page.getByRole("heading", { name: "Forecast performance" }),
    ).toBeVisible();
    await expect(page.getByText("Loading performance…")).toHaveCount(0);
    await page.screenshot({
        path: testInfo.outputPath("forecast-performance-desktop.png"),
        fullPage: true,
    });
});
