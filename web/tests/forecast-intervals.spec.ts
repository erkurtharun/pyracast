import { expect, test, type Page } from "./support/test";
import { accessibilityViolations } from "./support/accessibility";
import { readFile } from "node:fs/promises";
import Papa from "papaparse";
import {
    accountWith,
    mockApi,
    region,
    sessionIdentity,
} from "./support/workspace";

const start = Date.parse("2026-11-14T09:00:00Z");
function intervals(resolution = 60) {
    return Array.from({ length: 120 / resolution }, (_, index) => ({
        start: new Date(start + index * resolution * 60_000).toISOString(),
        end: new Date(start + (index + 1) * resolution * 60_000).toISOString(),
        mean_power_mw: 2,
        energy_mwh: (2 * resolution) / 60,
        ghi_wm2: 500,
        quality_flags: [] as string[],
    }));
}
async function openForecast(
    page: Page,
    options: {
        electrical?: boolean;
        scenario?: "outage";
        missing?: boolean;
        flagged?: boolean;
        mixed?: boolean;
        language?: "en" | "tr";
        qualityFlags?: string[];
    } = {},
) {
    const { electrical = true, scenario, missing, flagged, mixed } = options;
    const account = await mockApi(page);
    if (options.language === "tr") {
        account.current = accountWith({ language_preference: "tr" });
        await page.route("**/api/session/", (route) =>
            route.fulfill({
                json: { ...sessionIdentity(account.current), language: "tr" },
            }),
        );
    }
    await page.route(
        electrical ? "**/plants/21/forecast/**" : "**/regions/11/forecast/**",
        (route) => {
            const resolution = Number(
                new URL(route.request().url()).searchParams.get(
                    "resolution_minutes",
                ) ?? 60,
            );
            const rows = intervals(resolution).map((row, index) => ({
                ...row,
                mean_power_mw: missing
                    ? index === 0
                        ? 0
                        : null
                    : row.mean_power_mw,
                energy_mwh: missing ? (index === 0 ? 0 : null) : row.energy_mwh,
                ghi_wm2: missing ? (index === 0 ? 0 : null) : row.ghi_wm2,
                quality_flags:
                    flagged && index === 0 ? ["normal_test_flag"] : [],
                ...(mixed
                    ? {
                          source_kinds:
                              index === 0
                                  ? ["estimated_actuals"]
                                  : index === 2
                                    ? ["estimated_actuals", "forecast"]
                                    : ["forecast"],
                          source_snapshot_ids: [index + 1],
                      }
                    : {}),
            }));
            if (missing) {
                const [template] = rows;
                if (!template)
                    throw new Error("The forecast fixture has no interval.");
                rows.push({
                    ...template,
                    start: "2026-11-14T11:00:00Z",
                    end: "2026-11-14T12:00:00Z",
                });
            }
            const result = {
                start: "2026-11-13T21:00:00Z",
                end: "2026-11-14T21:00:00Z",
                complete: !missing,
                energy_mwh: missing ? null : 4,
                partial_energy_mwh: missing ? 0 : 4,
                quality_flags: options.qualityFlags ?? [],
                intervals: rows,
                ...(mixed
                    ? {
                          provenance: {
                              source_snapshots: [
                                  {
                                      snapshot_id: 1,
                                      collected_at: "2026-11-14T10:00:00Z",
                                      source_kind: "estimated_actuals",
                                  },
                                  {
                                      snapshot_id: 2,
                                      collected_at: "2026-11-13T18:00:00Z",
                                      source_kind: "forecast",
                                  },
                              ],
                          },
                      }
                    : {}),
            };
            return route.fulfill({
                json: {
                    date: "2026-11-14",
                    timezone: region.timezone,
                    freshness: "fresh",
                    source_kind: mixed ? "mixed" : "forecast",
                    ...(mixed
                        ? {
                              selection: {
                                  mode: "auto",
                                  rule: "latest",
                                  forecast_id: 2,
                                  options: [
                                      {
                                          forecast_id: 2,
                                          fetched_at: "2026-11-13T18:00:00Z",
                                      },
                                  ],
                              },
                          }
                        : {}),
                    result,
                    ...(scenario
                        ? {
                              [`${scenario}_scenario`]: {
                                  status: "estimated",
                                  performance_ratio: 0.5,
                                  window_start: "2026-11-01",
                                  window_end: "2026-11-13",
                                  days: [],
                                  result: {
                                      ...result,
                                      energy_mwh: 2,
                                      intervals: rows.map((row, index) => ({
                                          ...row,
                                          mean_power_mw: 1,
                                          energy_mwh: resolution / 60,
                                          quality_flags:
                                              flagged && index === 1
                                                  ? ["affected_test_flag"]
                                                  : [],
                                      })),
                                  },
                              },
                          }
                        : {}),
                },
            });
        },
    );
    await page.goto("/app/");
    await page.getByRole("button", { name: /East array/ }).click();
    if (!electrical)
        await page
            .getByRole("button", {
                name:
                    options.language === "tr"
                        ? "Işınım tahmini"
                        : "Irradiance forecast",
                exact: true,
            })
            .click();
}

const panel = (page: Page) => page.locator("section.panel");

test("a Turkish mixed day stays readable on mobile with only the time and forecast columns", async ({
    page,
}, testInfo) => {
    await page.setViewportSize({ width: 390, height: 844 });
    await openForecast(page, { mixed: true, language: "tr" });
    await expect(
        panel(page).getByRole("heading", { name: "Üretim tahmini" }),
    ).toBeVisible();
    await expect(panel(page).getByText(/^Tahmin verisi: /)).toBeVisible();
    await expect(panel(page).locator(".tag")).toHaveCount(0);
    await page.locator(".interval-details > summary").click();
    const table = page.getByRole("table", { name: "Saatlik değerler" });
    await expect(table.getByRole("columnheader")).toHaveText([
        "Saat",
        "Tahmin (MWh)",
    ]);
    expect(
        await page.evaluate(
            () => document.documentElement.scrollWidth <= innerWidth,
        ),
    ).toBe(true);
    await page.screenshot({
        path: testInfo.outputPath("mixed-sources-tr-mobile.png"),
        fullPage: true,
    });
});

const internalFlags = [
    "mixed_estimated_actual_vintages",
    "assumed_tracker_geometry",
    "provisional_production_calibration",
    "unvalidated_reference_transfer",
];

for (const language of ["en", "tr"] as const) {
    test(`the forecast tab shows no internal tags or source details in ${language === "en" ? "English" : "Turkish"}`, async ({
        page,
    }) => {
        await openForecast(page, { language, qualityFlags: internalFlags });
        await expect(
            panel(page).getByRole("heading", {
                name:
                    language === "en"
                        ? "Generation forecast"
                        : "Üretim tahmini",
            }),
        ).toBeVisible();
        await expect(panel(page).locator(".tag")).toHaveCount(0);
        await expect(panel(page).locator("details.provenance")).toHaveCount(0);
        await expect(
            panel(page).getByText(
                language === "en" ? "Source & quality" : "Kaynak ve kalite",
            ),
        ).toHaveCount(0);
        await expect(panel(page).locator(".stat")).toHaveCount(1);
    });
}

for (const electrical of [true, false]) {
    test(`${electrical ? "generation" : "irradiance"} shows a mixed day as forecast and names the row kinds in the CSV`, async ({
        page,
    }, testInfo) => {
        await openForecast(page, { electrical, mixed: true });
        await expect(
            panel(page).getByRole("heading", {
                name: electrical
                    ? "Generation forecast"
                    : "Irradiance forecast",
            }),
        ).toBeVisible();
        await expect(panel(page).getByText(/^Forecast data: /)).toHaveText(
            `Forecast data: ${new Intl.DateTimeFormat("en", {
                dateStyle: "medium",
                timeStyle: "short",
                timeZone: region.timezone,
            }).format(new Date("2026-11-13T18:00:00Z"))}`,
        );
        await expect(panel(page).getByText(/^Satellite data: /)).toHaveCount(0);
        await expect(panel(page).locator(".tag")).toHaveCount(0);
        await page.getByText("Show table", { exact: true }).click();
        const table = page.getByRole("table");
        await expect(table.getByRole("columnheader")).toHaveText([
            "Time",
            electrical ? "Forecast (MWh)" : "Irradiance (W/m²)",
        ]);
        if (electrical) {
            await page
                .getByLabel("Interval", { exact: true })
                .selectOption("30");
            await page.getByText("Show table", { exact: true }).click();
            await expect(
                page.getByRole("table", { name: "Half-hourly values" }),
            ).toBeVisible();
            await expect(table.getByRole("row")).toHaveCount(5);
        }
        const download = page.waitForEvent("download");
        await page
            .getByRole("button", { name: "Download CSV", exact: true })
            .click();
        const file = await download;
        expect(file.suggestedFilename()).toBe(
            `pyracast-east-array-2026-11-14-${electrical ? "generation" : "irradiance"}.csv`,
        );
        const records = Papa.parse<Record<string, string>>(
            await readFile(await file.path(), "utf8"),
            { header: true, skipEmptyLines: true },
        );
        expect(records.data[0]?.Data).toBe("Satellite data");
        expect(records.data[1]?.Data).toBe("Forecast");
        if (electrical) expect(records.data[2]?.Data).toBe("Forecast");
        await page.screenshot({
            path: testInfo.outputPath("mixed-sources.png"),
            fullPage: true,
        });
    });
}

test("generation intervals show energy without redundant visible power columns", async ({
    page,
}) => {
    await openForecast(page);
    await expect(page.getByLabel("Interval", { exact: true })).toHaveValue(
        "60",
    );
    await expect(page.getByText(/CSV includes all/)).toHaveCount(0);
    await expect(
        page.getByRole("checkbox", { name: "Show full day", exact: true }),
    ).toBeVisible();
    await page.getByText("Show table", { exact: true }).click();
    const table = page.getByRole("table", { name: "Hourly values" });
    await expect(table.getByRole("columnheader")).toHaveText([
        "Time",
        "Forecast (MWh)",
    ]);
    await expect(
        table.getByRole("cell").filter({ hasText: "12:00–13:00" }),
    ).toBeVisible();
    await expect(table.locator(".interval-value")).toHaveText(["2", "2"]);
    await expect(table.getByRole("row").nth(1)).toContainText(
        "Average power: 2 MW.",
    );
    await expect(page.locator(".interval-notes")).toHaveCount(0);
    await expect(
        page.getByText("— means no value.", { exact: true }),
    ).toHaveCount(0);
    await expect(
        page.getByRole("img", { name: "Average power (MW)", exact: true }),
    ).toBeVisible();
    await page.getByLabel("Interval", { exact: true }).selectOption("30");
    await page.getByText("Show table", { exact: true }).click();
    const halfHourly = page.getByRole("table", { name: "Half-hourly values" });
    await expect(halfHourly.locator(".interval-value")).toHaveText([
        "1",
        "1",
        "1",
        "1",
    ]);
    await expect(
        halfHourly.getByRole("cell").filter({ hasText: "12:00–12:30" }),
    ).toBeVisible();
    const download = page.waitForEvent("download");
    await page
        .getByRole("button", { name: "Download CSV", exact: true })
        .click();
    const records = Papa.parse<Record<string, string>>(
        await readFile(await (await download).path(), "utf8"),
        { header: true, skipEmptyLines: true },
    );
    expect(records.data).toHaveLength(4);
    expect(records.data[0]).toMatchObject({
        Start: "2026-11-14T12:00:00+03:00",
        End: "2026-11-14T12:30:00+03:00",
        "Average power (MW)": "2",
        "Forecast (MWh)": "1",
    });
});

for (const scenario of ["outage"] as const) {
    test(`${scenario} keeps both energies in the table without interval notes`, async ({
        page,
    }, testInfo) => {
        await page.setViewportSize({ width: 390, height: 844 });
        await openForecast(page, { scenario, flagged: true });
        await expect(page.getByText(/Green: normal capacity/)).toHaveCount(0);
        await expect(
            page.getByRole("img", { name: "Average power (MW)", exact: true }),
        ).toBeVisible();
        await expect(page.locator(".chart-legend")).toHaveText(
            "Without outageWith outage",
        );
        await page.getByText("Show table", { exact: true }).click();
        const table = page.getByRole("table");
        await expect(table.getByRole("columnheader")).toHaveText([
            "Time",
            "Without outage (MWh)",
            "With outage (MWh)",
        ]);
        await expect(table.locator(".interval-value")).toHaveText([
            "2",
            "1",
            "2",
            "1",
        ]);
        await expect(table).not.toContainText("test flag");
        await expect(page.locator(".interval-notes")).toHaveCount(0);
        await expect(page.getByText(/test flag/)).toHaveCount(0);
        expect(
            await table.evaluate(
                (element) => element.scrollWidth <= element.clientWidth,
            ),
        ).toBe(true);
        expect(
            await page.evaluate(() => document.documentElement.scrollWidth),
        ).toBeLessThanOrEqual(390);
        expect(
            await table
                .locator("tbody td")
                .nth(1)
                .evaluate((element) => getComputedStyle(element).textAlign),
        ).toBe("right");
        const scan = await accessibilityViolations(page);
        expect(scan).toEqual([]);
        await page.screenshot({
            path: testInfo.outputPath("intervals-mobile-390.png"),
            fullPage: true,
        });
        await page.setViewportSize({ width: 1280, height: 900 });
        await page.screenshot({
            path: testInfo.outputPath("intervals-desktop.png"),
            fullPage: true,
        });
        await page.setViewportSize({ width: 320, height: 844 });
        expect(
            await table.evaluate(
                (element) => element.scrollWidth <= element.clientWidth,
            ),
        ).toBe(true);
        expect(
            await page.evaluate(() => document.documentElement.scrollWidth),
        ).toBeLessThanOrEqual(320);
        const download = page.waitForEvent("download");
        await page
            .getByRole("button", { name: "Download CSV", exact: true })
            .click();
        const records = Papa.parse<Record<string, string>>(
            await readFile(await (await download).path(), "utf8"),
            { header: true, skipEmptyLines: true },
        );
        expect(records.meta.fields).toEqual([
            "Plant",
            "Data",
            "Start",
            "End",
            "Without outage (MWh)",
            "With outage (MWh)",
            "Average power without outage (MW)",
            "Average power with outage (MW)",
        ]);
        expect(records.data[0]).toMatchObject({
            "Without outage (MWh)": "2",
            "With outage (MWh)": "1",
            "Average power without outage (MW)": "2",
            "Average power with outage (MW)": "1",
        });
    });
}

for (const electrical of [true, false]) {
    test(`${electrical ? "generation" : "irradiance"} distinguishes zero from unavailable interval values and gives no total for missing hours`, async ({
        page,
    }) => {
        await openForecast(page, { electrical, missing: true });
        await expect(
            page.getByText("Data is missing for some hours.", { exact: true }),
        ).toBeVisible();
        await expect(
            page.locator(".stat", { hasText: "Daily total" }).locator("strong"),
        ).toHaveText(electrical ? "— MWh" : "— kWh/m²");
        await page.getByText("Show table", { exact: true }).click();
        const table = page.getByRole("table");
        await expect(table.getByRole("columnheader")).toHaveText([
            "Time",
            electrical ? "Forecast (MWh)" : "Irradiance (W/m²)",
        ]);
        await expect(table.locator(".interval-value")).toHaveText([
            "0",
            "—",
            "0",
        ]);
        await expect(
            table.getByRole("cell", { name: /No value\./ }),
        ).toHaveCount(1);
        await expect(
            page.getByText("— means no value.", { exact: true }),
        ).toBeVisible();
        await page
            .getByRole("checkbox", {
                name: "Show full day",
            })
            .check();
        await expect(table.getByRole("row")).toHaveCount(6);
        await expect(table.locator(".interval-value")).toHaveText([
            "—",
            "0",
            "—",
            "0",
            "—",
        ]);
        await expect(page.locator(".interval-notes")).toHaveCount(0);
    });
}
