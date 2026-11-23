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
                name: options.language === "tr" ? "Işınım" : "Irradiance",
                exact: true,
            })
            .click();
}

test("Turkish mixed sources stay readable on mobile without empty quality columns", async ({
    page,
}, testInfo) => {
    await page.setViewportSize({ width: 390, height: 844 });
    await openForecast(page, { mixed: true, language: "tr" });
    await expect(
        page.locator(".tag", { hasText: /^Gerçekleşen tahmini \+ öngörü$/ }),
    ).toBeVisible();
    await page.locator(".interval-details > summary").click();
    const table = page.getByRole("table");
    await expect(table.getByRole("columnheader")).toHaveCount(3);
    await expect(
        table.getByRole("cell", { name: "Gerçekleşen tahmini", exact: true }),
    ).toBeVisible();
    await expect(
        table.getByRole("cell", { name: "Öngörü", exact: true }),
    ).toBeVisible();
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

const sourceFlags = [
    "forecast_fallback",
    "mixed_estimated_actuals_and_forecast",
    "mixed_estimated_actual_vintages",
    "assumed_tracker_geometry",
    "geometry_not_modeled",
];
const sourceFlagLabels = {
    en: {
        panel: "Source & quality",
        labels: [
            "saved forecast, historical estimates pending",
            "estimates for completed intervals where available, forecast elsewhere",
            "combined estimate updates",
            "assumed tracking geometry",
            "plant geometry not modeled",
        ],
    },
    tr: {
        panel: "Kaynak ve kalite",
        labels: [
            "Kayıtlı tahmin, geçmiş veri bekleniyor",
            "Tamamlanan aralıklarda varsa uydu/model tahmini, diğer aralıklarda öngörü",
            "Birleştirilmiş gerçekleşen tahmini güncellemeleri",
            "Varsayılan takip sistemi geometrisi",
            "Santral geometrisi modellenmedi",
        ],
    },
} as const;

for (const language of ["en", "tr"] as const) {
    test(`the source and quality panel names the source and geometry flags in ${language === "en" ? "English" : "Turkish"}`, async ({
        page,
    }) => {
        const { panel, labels } = sourceFlagLabels[language];
        await openForecast(page, { language, qualityFlags: sourceFlags });
        await page.getByText(panel, { exact: true }).click();
        await expect(page.locator("details.provenance .tag")).toHaveText([
            ...labels,
        ]);
    });
}

for (const electrical of [true, false]) {
    test(`${electrical ? "generation" : "irradiance"} discloses estimated and forecast interval sources in mixed views and CSV`, async ({
        page,
    }, testInfo) => {
        await openForecast(page, { electrical, mixed: true });
        await expect(
            page.locator(".tag", { hasText: /^Estimated \+ forecast$/ }),
        ).toBeVisible();
        await expect(
            page.getByText(
                /Satellite\/model estimates for completed intervals/,
            ),
        ).toBeVisible();
        await page.getByText("View interval data (2 intervals)").click();
        const table = page.getByRole("table");
        await expect(
            table.getByRole("columnheader", { name: "Source", exact: true }),
        ).toBeVisible();
        await expect(table.getByRole("row").nth(1)).toContainText(
            "Estimated actuals",
        );
        await expect(table.getByRole("row").nth(2)).toContainText("Forecast");
        await expect(page.locator(".interval-notes")).toHaveCount(0);
        if (electrical) {
            await page.getByLabel("Resolution").selectOption("30");
            await page.getByText("View interval data (4 intervals)").click();
            await expect(table.getByRole("row").nth(3)).toContainText(
                "Estimated + forecast",
            );
        }
        const download = page.waitForEvent("download");
        await page
            .getByRole("button", { name: "Download CSV", exact: true })
            .click();
        const records = Papa.parse<Record<string, string>>(
            await readFile(await (await download).path(), "utf8"),
            { header: true, skipEmptyLines: true },
        );
        expect(records.data[0]?.interval_source).toContain("estimated_actuals");
        expect(records.data[1]?.interval_source).toContain("forecast");
        if (electrical) {
            expect(records.data[2]?.interval_source).toContain(
                "estimated_actuals",
            );
            expect(records.data[2]?.interval_source).toContain("forecast");
        }
        await page.getByText("Source & quality", { exact: true }).click();
        await expect(
            page.getByText(/Estimate update 1: collected/),
        ).toBeVisible();
        await expect(
            page.getByText(/Forecast update 2: collected/),
        ).toBeVisible();
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
    await expect(page.getByLabel("Resolution")).toHaveValue("60");
    await expect(page.getByText(/CSV includes all/)).toHaveCount(0);
    await expect(
        page.getByRole("checkbox", { name: "Show full day", exact: true }),
    ).toBeVisible();
    await page.getByText("View interval data (2 intervals)").click();
    const table = page.getByRole("table");
    await expect(table.getByRole("columnheader")).toHaveText([
        "Time interval",
        "Energy (MWh)",
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
        page.getByText("— Data unavailable; not zero.", { exact: true }),
    ).toHaveCount(0);
    await expect(
        page.getByRole("img", { name: /^Power by interval/ }),
    ).toBeVisible();
    await page.getByLabel("Resolution").selectOption("30");
    await expect(
        page.getByText("View interval data (4 intervals)"),
    ).toBeVisible();
    await page.getByText("View interval data (4 intervals)").click();
    await expect(table.locator(".interval-value")).toHaveText([
        "1",
        "1",
        "1",
        "1",
    ]);
    await expect(
        table.getByRole("cell").filter({ hasText: "12:00–12:30" }),
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
        starts_at: "2026-11-14T12:00:00+03:00",
        ends_at: "2026-11-14T12:30:00+03:00",
        mean_power_mw: "2",
        energy_mwh: "1",
    });
});

for (const scenario of ["outage"] as const) {
    test(`${scenario} keeps both energies and moves only flagged intervals into collapsed notes`, async ({
        page,
    }, testInfo) => {
        await page.setViewportSize({ width: 390, height: 844 });
        await openForecast(page, { scenario, flagged: true });
        await expect(page.getByText(/Green: normal capacity/)).toHaveCount(0);
        await expect(
            page.getByRole("img", {
                name: new RegExp(
                    `Normal capacity and with outages power by interval`,
                ),
            }),
        ).toBeVisible();
        await page.getByText("View interval data (2 intervals)").click();
        const affected = "With outages";
        const table = page.getByRole("table");
        await expect(table.getByRole("columnheader")).toHaveText([
            "Time interval",
            "Normal capacity (MWh)",
            `${affected} (MWh)`,
        ]);
        await expect(table.locator(".interval-value")).toHaveText([
            "2",
            "1",
            "2",
            "1",
        ]);
        await expect(table).not.toContainText("test flag");
        const notes = page.locator(".interval-notes");
        await expect(notes).not.toHaveAttribute("open", "");
        await expect(notes.locator("li")).toHaveCount(2);
        await expect(
            notes.getByText("Normal capacity: normal test flag", {
                exact: true,
            }),
        ).not.toBeVisible();
        await notes.locator("summary").click();
        await expect(notes.locator("li").nth(0)).toHaveText(
            "12:00–13:00Normal capacity: normal test flag",
        );
        await expect(notes.locator("li").nth(1)).toHaveText(
            `13:00–14:00${affected}: affected test flag`,
        );
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
            .getByRole("button", { name: "Download both scenarios CSV" })
            .click();
        const records = Papa.parse<Record<string, string>>(
            await readFile(await (await download).path(), "utf8"),
            { header: true, skipEmptyLines: true },
        );
        expect(records.data[0]).toMatchObject({
            normal_mean_power_mw: "2",
            normal_energy_mwh: "2",
            affected_mean_power_mw: "1",
            affected_energy_mwh: "1",
            normal_quality_flags: "normal_test_flag",
        });
        expect(records.data[1]?.affected_quality_flags).toBe(
            "affected_test_flag",
        );
    });
}

for (const electrical of [true, false]) {
    test(`${electrical ? "generation" : "irradiance"} distinguishes zero from unavailable interval values`, async ({
        page,
    }) => {
        await openForecast(page, { electrical, missing: true });
        await page.getByText("View interval data (3 intervals)").click();
        const table = page.getByRole("table");
        await expect(table.getByRole("columnheader")).toHaveText([
            "Time interval",
            electrical ? "Energy (MWh)" : "Irradiance (W/m²)",
        ]);
        await expect(table.locator(".interval-value")).toHaveText([
            "0",
            "—",
            "0",
        ]);
        await expect(
            table.getByRole("cell", { name: /Data unavailable/ }),
        ).toHaveCount(1);
        await expect(
            page.getByText("— Data unavailable; not zero.", { exact: true }),
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
        await expect(page.locator(".interval-notes summary")).toHaveText(
            "Interval notes (2)",
        );
        await page.locator(".interval-notes summary").click();
        await expect(page.locator(".interval-notes li")).toHaveCount(2);
        await expect(page.locator(".interval-notes")).toContainText(
            "missing source interval",
        );
    });
}
