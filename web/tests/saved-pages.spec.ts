import { expect, test } from "@playwright/test";
import { routes } from "../src/lib/routes";
import type { components } from "../src/lib/schema";
import { instant } from "./support/test";
import {
    savedDatabase,
    type SavedDatabase,
    type SavedRecords,
} from "./support/worker-database";
import { refusalCases } from "./support/saved-copy";
import { loadScript, SCOPE, type ScriptOptions } from "./support/worker-script";
import { workerState } from "./support/worker-state";

const NOW = instant.getTime();
const HOUR = 3_600_000;
const DAY = 24 * HOUR;
const ORIGIN = new URL(SCOPE).origin;
const TOKEN = "0123456789abcdef0123456789abcdef";
const KEY = { tenant: 3, site: 5, plant: 8 };
const NOTICE_POLICY =
    "default-src 'none'; style-src 'unsafe-inline'; form-action 'none'; base-uri 'none'; frame-ancestors 'none'";
const WORKER = "../public/app/sw.js";
const EXPIRED =
    "The saved figures were removed from this device because no one signed in here for 90 days or the device clock was changed.";
const REMOVED = "The saved figures were removed from this device.";

type Language = "en" | "tr";
type Row = Record<string, unknown>;
type DaysAnswer = components["schemas"]["PlantDaysOutput"];

const holder = (overrides: Row = {}) => ({
    user: 7,
    language: "en",
    session: "tag-1",
    active: true,
    confirmedAt: NOW - DAY,
    removal: TOKEN,
    retryAt: null,
    ...overrides,
});

const plant = (overrides: Row = {}) => ({
    ...KEY,
    name: "North field",
    workspace: "Solar Co",
    openedAt: NOW - 2 * HOUR,
    attemptedAt: NOW - 3 * HOUR,
    refreshedAt: NOW - 3 * HOUR,
    ...overrides,
});

const emitted = (time: number) =>
    new Date(time).toISOString().replace(".000Z", "Z");

function intervals(first: string, count: number, length = HOUR) {
    const start = Date.parse(first);
    return Array.from({ length: count }, (_, index) => ({
        start: emitted(start + index * length),
        end: emitted(start + (index + 1) * length),
    }));
}

const hours = (date: string, count: number) =>
    intervals(`${date}T09:00:00Z`, count);

const energyRows = (
    first: string,
    values: (number | null)[],
    length = HOUR,
) => {
    const times = intervals(first, values.length, length);
    return values.map((mwh, index) => ({
        ...times[index]!,
        mwh,
        affected_mwh: null,
        sources: null,
    }));
};

function forecastDay(date: string, overrides: Row = {}) {
    const rows = hours(date, 3).map((interval, index) => ({
        ...interval,
        mwh: [0.5, 1.23456, null][index],
        affected_mwh: null,
        sources: null,
    }));
    return {
        ...KEY,
        kind: "forecast",
        date,
        timezone: "UTC",
        source_kind: "forecast",
        freshness: "fresh",
        stale_reason: null,
        unavailable_reason: null,
        collected_at: "2026-11-10T06:00:00+00:00",
        total_mwh: 1.73456,
        partial: false,
        affected: null,
        rows,
        ...overrides,
    };
}

function productionDay(date: string, overrides: Row = {}) {
    const rows = hours(date, 3).map((interval, index) => ({
        ...interval,
        mwh: [0.25, 0.5, null][index],
        quality: ["valid", "curtailed", null][index],
        source: ["epias", "manual <entry>", null][index],
        provisional: [false, true, null][index],
    }));
    return {
        ...KEY,
        kind: "production",
        date,
        timezone: "UTC",
        total_mwh: 0.75,
        complete: false,
        rows,
        ...overrides,
    };
}

function copy(records: SavedRecords = {}) {
    return savedDatabase({
        records: {
            holder: holder(),
            plants: [plant()],
            days: [],
            ...records,
        },
    });
}

const savedWorker = (database: SavedDatabase, options: ScriptOptions = {}) =>
    loadScript(WORKER, {
        database,
        now: NOW,
        zone: "Europe/Istanbul",
        ...options,
    });

async function visit(worker: ReturnType<typeof loadScript>, address: string) {
    const { answer } = await worker.navigate({ url: `${ORIGIN}${address}` });
    expect(answer).toBeDefined();
    return answer!;
}

async function savedAnswer(
    address: string,
    database = copy(),
    options: ScriptOptions = {},
) {
    const answer = await visit(savedWorker(database, options), address);
    return { response: answer, body: await answer.text(), database };
}

function manualTimers() {
    const pending = new Set<() => void>();
    return {
        setTimeout: (run: () => void) => {
            const timer = () => run();
            pending.add(timer);
            return timer;
        },
        clearTimeout: (timer: unknown) => {
            pending.delete(timer as () => void);
        },
        fire: () => {
            for (const timer of [...pending]) {
                pending.delete(timer);
                timer();
            }
        },
    };
}

function clock(start: number) {
    let time = start;
    return {
        now: () => time,
        advance: (by: number) => {
            time += by;
        },
    };
}

const savedPage = async (
    address: string,
    database?: ReturnType<typeof copy>,
    options?: ScriptOptions,
) => (await savedAnswer(address, database, options)).body;

const plantAddress = (
    kind: "forecast" | "production" = "forecast",
    date?: string,
) => routes.plant(KEY.tenant, KEY.site, kind, { date });

const links = (body: string) =>
    [...body.matchAll(/<a href="([^"]*)"[^>]*>([^<]*)<\/a>/g)].map(
        ([, href = "", text = ""]) => [href.replaceAll("&amp;", "&"), text],
    );

const tableRows = (body: string) =>
    [...body.matchAll(/<tr><td>(.*?)<\/tr>/g)].map(([row]) =>
        [...row.matchAll(/<td[^>]*>([^<]*)<\/td>/g)].map(([, cell]) => cell),
    );

function bars(body: string, kind: string, full = false) {
    const charts = body.split('<div class="focused-chart">');
    const chart = full ? charts[0] : charts.at(-1);
    return (
        chart?.match(
            new RegExp(
                '<rect class="' +
                    kind +
                    '"|<line class="zero zero-' +
                    kind +
                    '"',
                "g",
            ),
        )?.length ?? 0
    );
}

function chartEdges(body: string) {
    const chart = body.split('<div class="focused-chart">').at(-1);
    const axis =
        chart?.match(/<div class="chart-time">(.*?)<\/div>/)?.[1] ?? "";
    const ticks = [...axis.matchAll(/>([^<>]+)<\/span>/g)];
    return [ticks[0]?.[1], ticks.at(-1)?.[1]];
}

function totalText(label: string, value: string, detail?: string) {
    return `<p class="total"><span>${label}</span>
<strong>${value} <span>MWh</span></strong>
${detail ? `<small>${detail}</small>` : ""}`;
}

const shownDay = (body: string) => body.match(/<time datetime="([^"]+)">/)?.[1];

const bothLanguages = (body: string) =>
    body.includes('<section lang="en">') &&
    body.includes('<section lang="tr">');

const expectNotice = (body: string) => {
    expect(bothLanguages(body)).toBe(true);
    expect(links(body)).toEqual([
        ["", "Try again"],
        ["", "Tekrar dene"],
    ]);
};

const heldDates = (body: string) =>
    links(body)
        .map(([href]) => href?.match(/\?date=(.+)$/)?.[1])
        .filter(Boolean);

const LABELS: Record<Language, Record<string, string>> = {
    en: {
        title: "Pyracast cannot be reached · Saved on this device",
        read: "Read from the server on Nov 10, 2026, 12:00 GMT+3, 3 hours ago.",
        day: "Plant local time · UTC",
        source: "Source: Estimated actuals.",
        collected: "Source data collected on Nov 10, 2026, 09:00 GMT+3.",
        stale: "The newest provider collection is more than a day old.",
        total: totalText("Day total", "1.735", "partial estimate"),
        affected: totalText("With outages", "1.2"),
        heading: "<h1>North field</h1>",
        caption: "<caption>Energy per interval</caption>",
    },
    tr: {
        title: "Bu cihazda kayıtlı",
        read: "Sunucudan okunma: 10 Kas 2026 12:00 GMT+3, 3 saat önce.",
        day: "Santral saati · UTC",
        source: "Kaynak: Gerçekleşen tahmini.",
        collected: "Kaynak verinin alınma zamanı: 10 Kas 2026 09:00 GMT+3.",
        stale: "Sağlayıcıdan alınan en yeni veri bir günden eski.",
        total: totalText("Gün toplamı", "1,735", "kısmi tahmin"),
        affected: totalText("Kesintiler dahil", "1,2"),
        heading: "<h1>North field</h1>",
        caption: "<caption>Aralık başına enerji</caption>",
    },
};

if (workerState === "figures")
    test.describe("the worker without a connection", () => {
        test.describe("a saved forecast page", () => {
            for (const language of ["en", "tr"] as const)
                test(`shows a held day with the labels of its read, the curve and the table in ${language}`, async () => {
                    const { response, body } = await savedAnswer(
                        plantAddress("forecast", "2026-11-11"),
                        copy({
                            holder: holder({ language }),
                            days: [
                                forecastDay("2026-11-11", {
                                    source_kind: "estimated_actuals",
                                    freshness: "stale",
                                    stale_reason: "collection_overdue",
                                    partial: true,
                                    affected: {
                                        status: "estimated",
                                        total_mwh: 1.2,
                                        partial: false,
                                    },
                                }),
                            ],
                        }),
                    );
                    expect(response.status).toBe(503);
                    expect(response.headers.get("cache-control")).toBe(
                        "no-store",
                    );
                    expect(
                        response.headers.get("content-security-policy"),
                    ).toBe(NOTICE_POLICY);
                    expect(body).toContain(`<html lang="${language}">`);
                    const labels = LABELS[language];
                    for (const label of Object.values(labels))
                        expect(body).toContain(label);
                    expect(links(body)).toContainEqual([
                        "",
                        language === "en" ? "Try again" : "Tekrar dene",
                    ]);
                    expect(body).toMatch(/<svg role="img" aria-label="[^"]+"/);
                    expect(bars(body, "energy")).toBe(2);
                    expect(bars(body, "affected")).toBe(0);
                    expect(tableRows(body)).toEqual([
                        ["09:00–10:00", language === "en" ? "0.5" : "0,5", "—"],
                        [
                            "10:00–11:00",
                            language === "en" ? "1.235" : "1,235",
                            "—",
                        ],
                        ["11:00–12:00", "—", "—"],
                    ]);
                    expect(body).not.toMatch(
                        /<script|<meta[^>]+http-equiv|<form|<iframe/i,
                    );
                    expect(body).not.toMatch(/\son[a-z]+=/i);
                });

            test("draws one bar per row for each scenario and leaves a gap for a missing value", async () => {
                const rows = hours("2026-11-11", 4).map((interval, index) => ({
                    ...interval,
                    mwh: [1, null, 2, 3][index],
                    affected_mwh: [0.5, 1, null, 3][index],
                    sources: null,
                }));
                const body = await savedPage(
                    plantAddress("forecast", "2026-11-11"),
                    copy({
                        days: [
                            forecastDay("2026-11-11", {
                                rows,
                                affected: {
                                    status: "estimated",
                                    total_mwh: 4.5,
                                    partial: false,
                                },
                            }),
                        ],
                    }),
                );
                expect(bars(body, "energy")).toBe(3);
                expect(bars(body, "affected")).toBe(3);
                expect(tableRows(body).map((row) => row.slice(1))).toEqual([
                    ["1", "0.5"],
                    ["—", "1"],
                    ["2", "—"],
                    ["3", "3"],
                ]);
            });

            test("focus follows local daylight and keeps a recorded value outside it", async () => {
                const date = "2026-11-11";
                const rows = energyRows("2026-11-11T06:00:00Z", [
                    0,
                    0,
                    0,
                    1,
                    null,
                    0,
                    2,
                    0,
                ]);
                const render = (overrides: Row = {}) =>
                    savedPage(
                        plantAddress("forecast", date),
                        copy({
                            days: [forecastDay(date, { rows, ...overrides })],
                        }),
                    );
                const window = {
                    starts_at: "2026-11-11T08:30:00.125Z",
                    ends_at: "2026-11-11T11:00:00Z",
                };
                const body = await render({ daylight_windows: [window] });
                expect(chartEdges(body)).toEqual(["08:00", "13:00"]);
                expect(bars(body, "energy")).toBe(4);
                expect(tableRows(body)).toHaveLength(rows.length);

                const boundary = await render({
                    rows: rows.slice(0, 6),
                    daylight_windows: [
                        { ...window, ends_at: "2026-11-11T11:00:00.000001Z" },
                    ],
                });
                expect(chartEdges(boundary)).toEqual(["08:00", "12:00"]);

                const legacy = await render();
                expect(chartEdges(legacy)).toEqual(["09:00", "13:00"]);
                const malformed = await render({
                    daylight_windows: [
                        { ...window, ends_at: "2026-11-12T11:00:00Z" },
                    ],
                });
                expect(malformed).toBe(legacy);
            });

            test("polar night has no focused chart and keeps the known zeroes in the full day", async () => {
                const date = "2026-12-21";
                const rows = energyRows(
                    "2026-12-20T23:00:00Z",
                    Array.from({ length: 24 }, () => 0),
                );
                const render = (overrides: Row) =>
                    savedPage(
                        plantAddress("forecast", date),
                        copy({
                            days: [
                                forecastDay(date, {
                                    timezone: "Europe/Oslo",
                                    total_mwh: null,
                                    rows,
                                    ...overrides,
                                }),
                            ],
                        }),
                    );
                const polar = await render({ daylight_windows: [] });
                expect(polar).toContain("No daylight on this plant day.");
                expect(polar).toContain(
                    "All plotted interval values are zero.",
                );
                expect(polar).not.toContain('<div class="focused-chart">');
                expect(polar).toContain(
                    '<details class="full-day">\n<summary>Full-day chart</summary>',
                );
                expect(bars(polar, "energy", true)).toBe(24);
                expect(tableRows(polar)).toHaveLength(24);

                const missing = await render({ rows: [] });
                expect(missing).toContain(
                    "No interval data is saved for this day.",
                );
                expect(missing).not.toContain("No daylight on this plant day.");
                expect(missing).not.toContain("<svg");
            });

            test("marks a day of mixed sources and an outage of unknown impact", async () => {
                const rows = hours("2026-11-11", 2).map((interval, index) => ({
                    ...interval,
                    mwh: 1,
                    affected_mwh: null,
                    sources: [
                        ["estimated_actuals"],
                        ["forecast", "estimated_actuals"],
                    ][index],
                }));
                const body = await savedPage(
                    plantAddress("forecast", "2026-11-11"),
                    copy({
                        days: [
                            forecastDay("2026-11-11", {
                                source_kind: "mixed",
                                rows,
                                affected: {
                                    status: "unknown_impact",
                                    total_mwh: null,
                                    partial: false,
                                },
                            }),
                        ],
                    }),
                );
                expect(body).toContain("Source: Estimated + forecast.");
                expect(body).toContain(
                    "Estimates for completed intervals where available; forecast elsewhere.",
                );
                expect(body).toContain("Outage impact is unknown.");
                expect(body).toContain(totalText("With outages", "—"));
                expect(tableRows(body).map((row) => row.at(-1))).toEqual([
                    "Estimated actuals",
                    "Forecast, Estimated actuals",
                ]);
            });

            const REASONS: [string, string, string][] = [
                [
                    "configuration_unavailable",
                    "No configuration for this day.",
                    "Bu güne ait yapılandırma yok.",
                ],
                [
                    "forecast_unavailable",
                    "No forecast for this day yet.",
                    "Bu güne ait tahmin henüz yok.",
                ],
                [
                    "estimated_actuals_unavailable",
                    "No estimated actuals for this day.",
                    "Bu güne ait gerçekleşen üretim tahmini yok.",
                ],
                [
                    "calculation_out_of_range",
                    "This forecast could not be calculated.",
                    "Bu tahmin hesaplanamadı.",
                ],
            ];
            for (const [reason, english, turkish] of REASONS)
                test(`shows the label of ${reason} instead of totals, curve and table`, async () => {
                    for (const [language, label] of [
                        ["en", english],
                        ["tr", turkish],
                    ]) {
                        const body = await savedPage(
                            plantAddress("forecast", "2026-11-11"),
                            copy({
                                holder: holder({ language }),
                                days: [
                                    forecastDay("2026-11-11", {
                                        source_kind: "estimated_actuals",
                                        freshness: "missing",
                                        unavailable_reason: reason,
                                        collected_at: null,
                                        total_mwh: null,
                                        rows: [],
                                    }),
                                ],
                            }),
                        );
                        expect(body).toContain(`<p class="mark">${label}</p>`);
                        expect(body).not.toMatch(/Day total|Gün toplamı/);
                        expect(body).not.toContain("<svg");
                        expect(body).not.toContain("<table");
                    }
                });

            test("shows an unknown code as it is", async () => {
                const body = await savedPage(
                    plantAddress("forecast", "2026-11-11"),
                    copy({
                        days: [
                            forecastDay("2026-11-11", {
                                source_kind: "satellite",
                                stale_reason: "provider_paused",
                                affected: {
                                    status: "assumed",
                                    total_mwh: 1,
                                    partial: true,
                                },
                            }),
                        ],
                    }),
                );
                expect(body).toContain("Source: satellite.");
                expect(body).toContain('<p class="mark">provider_paused</p>');
                expect(body).toContain('<p class="mark">assumed</p>');
                expect(body).toContain(
                    totalText("With outages", "1", "partial estimate"),
                );
            });

            test("writes an interval of a 23 and of a 25 hour day once each, naming the offset where it changes", async () => {
                const day = (date: string, start: string, count: number) =>
                    forecastDay(date, {
                        timezone: "Europe/Berlin",
                        rows: energyRows(
                            start,
                            Array.from({ length: count }, (_, index) => index),
                        ),
                    });
                const short = await savedPage(
                    plantAddress("forecast", "2026-03-29"),
                    copy({
                        days: [day("2026-03-29", "2026-03-28T23:00:00Z", 23)],
                    }),
                );
                const long = await savedPage(
                    plantAddress("forecast", "2026-10-25"),
                    copy({
                        days: [day("2026-10-25", "2026-10-24T22:00:00Z", 25)],
                    }),
                );
                expect(tableRows(short)).toHaveLength(23);
                expect(bars(short, "energy", true)).toBe(23);
                expect(tableRows(short)[2]?.[0]).toBe(
                    "03:00 GMT+2–04:00 GMT+2",
                );
                const labels = tableRows(long).map(([interval]) => interval);
                expect(labels).toHaveLength(25);
                expect(bars(long, "energy", true)).toBe(25);
                expect(new Set(labels).size).toBe(25);
                expect(labels.slice(2, 4)).toEqual([
                    "02:00 GMT+2–02:00 GMT+1",
                    "02:00 GMT+1–03:00 GMT+1",
                ]);
            });

            test("names the offset when it changes at the end of the last interval", async () => {
                const rows = energyRows(
                    "2026-09-05T04:00:00Z",
                    Array.from({ length: 24 }, () => 1),
                );
                const body = await savedPage(
                    plantAddress("forecast", "2026-09-05"),
                    copy({
                        days: [
                            forecastDay("2026-09-05", {
                                timezone: "America/Santiago",
                                rows,
                            }),
                        ],
                    }),
                );
                const labels = tableRows(body).map(([interval]) => interval);
                expect(labels[0]).toBe("00:00 GMT-4–01:00 GMT-4");
                expect(labels.at(-1)).toBe("23:00 GMT-4–01:00 GMT-3");
            });
        });

        test.describe("a saved production page", () => {
            test("shows the day's marks and total and keeps invalid readings in the table", async () => {
                const date = "2026-11-09";
                const day = productionDay(date);
                const rows = [
                    ...day.rows,
                    {
                        start: "2026-11-09T12:00:00Z",
                        end: "2026-11-09T13:00:00Z",
                        mwh: 9,
                        quality: "invalid",
                        source: "manual",
                        provisional: false,
                    },
                ];
                const body = await savedPage(
                    plantAddress("production", date),
                    copy({ days: [productionDay(date, { rows })] }),
                );
                expect(body).toContain("<h1>North field</h1>");
                expect(body).toContain("Plant local time · UTC");
                expect(body).toContain('<p class="mark">Incomplete day</p>');
                expect(body).toContain(totalText("Day total", "0.75"));
                expect(tableRows(body)).toEqual([
                    ["09:00–10:00", "0.25", "Valid", "epias", "—"],
                    [
                        "10:00–11:00",
                        "0.5",
                        "Curtailed",
                        "manual &lt;entry&gt;",
                        "Provisional",
                    ],
                    ["11:00–12:00", "—", "—", "—", "—"],
                    ["12:00–13:00", "9", "Invalid", "manual", "—"],
                ]);
                expect(body).toContain('aria-current="page">Production');
                expect(body).toContain(
                    "Invalid readings are available in the interval table.",
                );
                expect(bars(body, "energy", true)).toBe(2);
                expect(bars(body, "energy")).toBe(2);
            });

            test("shows a complete day in Turkish", async () => {
                const body = await savedPage(
                    plantAddress("production", "2026-11-09"),
                    copy({
                        holder: holder({ language: "tr" }),
                        days: [productionDay("2026-11-09", { complete: true })],
                    }),
                );
                expect(body).toContain('<p class="mark">Tam gün</p>');
                expect(body).toContain(totalText("Gün toplamı", "0,75"));
                expect(tableRows(body)[1]).toEqual([
                    "10:00–11:00",
                    "0,5",
                    "Kısıtlanmış",
                    "manual &lt;entry&gt;",
                    "Geçici",
                ]);
            });
        });

        test.describe("the day of a saved page", () => {
            const forecastDays = (...dates: string[]) =>
                dates.map((date) => forecastDay(date));
            const productionDays = (...dates: string[]) =>
                dates.map((date) => productionDay(date));

            test("is the named day when it is held", async () => {
                const body = await savedPage(
                    plantAddress("forecast", "2026-11-09"),
                    copy({ days: forecastDays("2026-11-09", "2026-11-11") }),
                );
                expect(shownDay(body)).toBe("2026-11-09");
            });

            test("is tomorrow for a forecast and yesterday for production when held", async () => {
                expect(
                    shownDay(
                        await savedPage(
                            plantAddress("forecast"),
                            copy({
                                days: forecastDays(
                                    "2026-11-10",
                                    "2026-11-11",
                                    "2026-11-12",
                                ),
                            }),
                        ),
                    ),
                ).toBe("2026-11-11");
                expect(
                    shownDay(
                        await savedPage(
                            plantAddress("production"),
                            copy({
                                days: productionDays(
                                    "2026-11-08",
                                    "2026-11-09",
                                ),
                            }),
                        ),
                    ),
                ).toBe("2026-11-09");
            });

            test("is the held day nearest before the default for a copy that ends before today", async () => {
                expect(
                    shownDay(
                        await savedPage(
                            plantAddress("forecast"),
                            copy({
                                days: forecastDays(
                                    "2026-10-01",
                                    "2026-10-20",
                                    "2026-10-21",
                                ),
                            }),
                        ),
                    ),
                ).toBe("2026-10-21");
            });

            test("is the earliest held day when every held day is later", async () => {
                expect(
                    shownDay(
                        await savedPage(
                            plantAddress("production"),
                            copy({
                                days: productionDays(
                                    "2026-11-12",
                                    "2026-11-13",
                                ),
                            }),
                        ),
                    ),
                ).toBe("2026-11-12");
            });

            test("follows the time zone of the plant's days, not the device's", async () => {
                const days = ["2026-11-11", "2026-11-12", "2026-11-13"].map(
                    (date) =>
                        forecastDay(date, {
                            timezone: "Pacific/Kiritimati",
                            rows: energyRows(`${date}T09:00:00+14:00`, [1]),
                        }),
                );
                expect(
                    shownDay(
                        await savedPage(
                            plantAddress("forecast"),
                            copy({ days }),
                            {
                                now: Date.parse("2026-11-10T15:00:00Z"),
                                zone: "America/Los_Angeles",
                            },
                        ),
                    ),
                ).toBe("2026-11-12");
            });

            test("that is named but not held gives the list of held days", async () => {
                const body = await savedPage(
                    plantAddress("forecast", "2026-11-20"),
                    copy({ days: forecastDays("2026-11-09", "2026-11-11") }),
                );
                expect(body).toContain("This day is not saved on this device.");
                expect(shownDay(body)).toBeUndefined();
                expect(links(body)).toEqual(
                    expect.arrayContaining([
                        [
                            plantAddress("forecast", "2026-11-09"),
                            "November 9, 2026",
                        ],
                        [
                            plantAddress("forecast", "2026-11-11"),
                            "November 11, 2026",
                        ],
                    ]),
                );
            });

            test("is missing for a held plant without saved days of the kind, which says so", async () => {
                const body = await savedPage(plantAddress("production"));
                expect(shownDay(body)).toBeUndefined();
                expect(body).toContain(
                    "No figures of this kind are saved for this plant yet.",
                );
            });
        });

        test.describe("the links of a saved page", () => {
            test("equal the application's addresses of the neighbouring days, the other kind, the saved plants and the removal", async () => {
                const body = await savedPage(
                    plantAddress("forecast", "2026-11-11"),
                    copy({
                        days: ["2026-11-09", "2026-11-11", "2026-11-12"].map(
                            (date) => forecastDay(date),
                        ),
                    }),
                );
                expect(links(body)).toEqual([
                    [routes.entry(), "Saved plants"],
                    ["", "Try again"],
                    [plantAddress("production"), "Production"],
                    [
                        plantAddress("forecast", "2026-11-09"),
                        "Previous saved day",
                    ],
                    [plantAddress("forecast", "2026-11-12"), "Next saved day"],
                    [
                        "/app/?saved=remove",
                        "Remove saved figures from this device",
                    ],
                ]);
            });

            test("leave out the neighbour a first or last held day lacks", async () => {
                const body = await savedPage(
                    plantAddress("production", "2026-11-09"),
                    copy({ days: [productionDay("2026-11-09")] }),
                );
                expect(links(body).map(([, text]) => text)).toEqual([
                    "Saved plants",
                    "Try again",
                    "Forecast",
                    "Remove saved figures from this device",
                ]);
                expect(links(body)[2]?.[0]).toBe(plantAddress("forecast"));
            });
        });

        test.describe("stored text on a saved page", () => {
            test("is escaped in content and in attributes", async () => {
                const name = '<img src=x>"North" & field';
                const workspace = '<b>"Solar" & Co</b>';
                const code = '<i>"kind" & co</i>';
                const body = await savedPage(
                    plantAddress("forecast", "2026-11-11"),
                    copy({
                        plants: [plant({ name, workspace })],
                        days: [
                            forecastDay("2026-11-11", { source_kind: code }),
                        ],
                    }),
                );
                const escaped = (value: string) =>
                    value
                        .replaceAll("&", "&amp;")
                        .replaceAll("<", "&lt;")
                        .replaceAll(">", "&gt;")
                        .replaceAll('"', "&quot;");
                for (const value of [name, workspace, code]) {
                    expect(body).not.toContain(value);
                    expect(body).toContain(escaped(value));
                }
                expect(body).toContain(
                    `aria-label="Energy per interval of ${escaped(name)} on`,
                );
                expect(body).toContain(
                    `<title>${escaped(name)}: Forecast | Pyracast</title>`,
                );
                expect(body).not.toMatch(/<(?:img|b|i)[\s>]/);
                const list = await savedPage(
                    routes.entry(),
                    copy({ plants: [plant({ name, workspace })] }),
                );
                expect(list).not.toContain(name);
                expect(list).toContain(`>${escaped(name)}</a>`);
                expect(list).toContain(
                    `<p class="workspace">${escaped(workspace)}</p>`,
                );
            });
        });

        test.describe("the notice of the worker with a copy", () => {
            test("lists the saved plants in the holder's language with their read time", async () => {
                const body = await savedPage(
                    routes.entry(),
                    copy({
                        holder: holder({ language: "tr" }),
                        plants: [
                            plant({ name: "Zeta", refreshedAt: NOW - 3 * DAY }),
                            {
                                ...plant(),
                                site: 6,
                                plant: 9,
                                name: "Alpha",
                                refreshedAt: null,
                            },
                        ],
                    }),
                );
                expect(body).toContain('<html lang="tr">');
                expect(body).not.toContain('<section lang="en">');
                expect(body).toContain("<h1>Kayıtlı santraller</h1>");
                expect(body).toContain(
                    "Pyracast'e ulaşılamıyor · Bu cihazda kayıtlı",
                );
                expect(body).not.toContain("davet bağlantısıyla");
                expect(links(body)).toEqual([
                    ["", "Tekrar dene"],
                    [routes.plant(3, 6), "Alpha"],
                    [routes.plant(3, 5), "Zeta"],
                    ["/app/?saved=remove", "Kayıtlı rakamları bu cihazdan sil"],
                ]);
                expect(body).toContain("Henüz sunucudan okunmadı.");
                expect(body).toContain("3 gün önce.");
            });

            test("answers an address that names no held plant", async () => {
                for (const address of [
                    "/app/w/3/",
                    "/app/w/3/plants/6/forecast/",
                    "/app/w/4/plants/5/forecast/",
                    "/app/w/0/plants/5/forecast/",
                    "/app/w/3/plants/5oops/production/",
                    "/app/w/3/plants/5%2Fproduction/",
                    "/app/w/3/plants/05/forecast/",
                    "/app/w/99999999999999999999/plants/5/forecast/",
                    "/app/account/",
                ]) {
                    const body = await savedPage(address);
                    expect(links(body), address).toContainEqual([
                        routes.plant(3, 5),
                        "North field",
                    ]);
                    expect(shownDay(body), address).toBeUndefined();
                }
            });

            test("gives way to a held plant's forecast after its complete site segment", async () => {
                for (const address of [
                    "/app/w/3/plants/5",
                    "/app/w/3/plants/5/",
                    "/app/w/3/plants/5/settings/",
                ]) {
                    const body = await savedPage(
                        address,
                        copy({ days: [forecastDay("2026-11-11")] }),
                    );
                    expect(shownDay(body), address).toBe("2026-11-11");
                }
            });

            test("is the plain notice in the holder's language for a copy without plants", async () => {
                const body = await savedPage(
                    plantAddress("forecast"),
                    copy({ plants: [] }),
                );
                expect(body).toContain('<html lang="en">');
                expect(bothLanguages(body)).toBe(false);
                expect(links(body)).toEqual([["", "Try again"]]);
            });

            test("leaves out a plant record that does not match the format", async () => {
                const body = await savedPage(
                    routes.entry(),
                    copy({
                        plants: [
                            plant(),
                            { ...plant(), site: 6, name: 4 },
                            { ...plant(), site: 7, refreshedAt: "yesterday" },
                            { ...plant(), site: 8, plant: 0 },
                            { ...plant(), site: 9, workspace: undefined },
                            { ...plant(), site: 10, refreshedAt: 1e20 },
                            { ...plant(), site: 11, openedAt: NOW + 0.5 },
                        ],
                    }),
                );
                expect(links(body).map(([href]) => href)).toEqual([
                    "",
                    routes.plant(3, 5),
                    "/app/?saved=remove",
                ]);
            });

            test("leaves out a plant record whose key disagrees with it", async () => {
                const database = copy();
                database.put("plants", plant({ tenant: 4 }), [3, 6, 9]);
                const body = await savedPage(routes.entry(), database);
                expect(links(body).map(([href]) => href)).toEqual([
                    "",
                    routes.plant(3, 5),
                    "/app/?saved=remove",
                ]);
            });

            test("reads the days of the valid plant when a malformed one shares its site", async () => {
                const body = await savedPage(
                    plantAddress("forecast", "2026-11-11"),
                    copy({
                        plants: [{ ...plant(), plant: 1, name: 4 }, plant()],
                        days: [forecastDay("2026-11-11")],
                    }),
                );
                expect(shownDay(body)).toBe("2026-11-11");
            });

            test("leaves out a day stored under a key that disagrees with it and keeps its neighbours", async () => {
                const database = copy({ days: [forecastDay("2026-11-13")] });
                database.put("days", forecastDay("2026-11-12"), [
                    KEY.tenant,
                    KEY.site,
                    KEY.plant,
                    "forecast",
                    "2026-11-11",
                ]);
                for (const date of ["2026-11-11", "2026-11-12"]) {
                    const body = await savedPage(
                        plantAddress("forecast", date),
                        database,
                    );
                    expect(shownDay(body), date).toBeUndefined();
                    expect(heldDates(body), date).toEqual(["2026-11-13"]);
                }
            });

            const forecastRows = (rows: Row[]) =>
                forecastDay("2026-11-11").rows.map((row, index) => ({
                    ...row,
                    ...rows[index],
                }));
            const productionRows = (rows: Row[]) =>
                productionDay("2026-11-11").rows.map((row, index) => ({
                    ...row,
                    ...rows[index],
                }));
            const ruledOut: [string, Row][] = [
                [
                    "with an impossible date in an interval",
                    forecastDay("2026-11-11", {
                        rows: forecastRows([{ start: "2026-11-31T09:00:00Z" }]),
                    }),
                ],
                [
                    "with an interval ending at hour 24",
                    forecastDay("2026-11-11", {
                        rows: forecastRows([
                            {},
                            {},
                            { end: "2026-11-11T24:00:00Z" },
                        ]),
                    }),
                ],
                [
                    "with an impossible collection date",
                    forecastDay("2026-11-11", {
                        collected_at: "2026-02-30T06:00:00+00:00",
                    }),
                ],
                [
                    "with a reason and figures",
                    forecastDay("2026-11-11", {
                        source_kind: "estimated_actuals",
                        freshness: "missing",
                        unavailable_reason: "estimated_actuals_unavailable",
                        collected_at: null,
                    }),
                ],
                [
                    "with negative energy",
                    forecastDay("2026-11-11", {
                        rows: forecastRows([{ mwh: -0.5 }]),
                    }),
                ],
                [
                    "with negative energy with outages",
                    forecastDay("2026-11-11", {
                        rows: forecastRows([{}, { affected_mwh: -1 }]),
                    }),
                ],
                [
                    "with an interval that ends before it starts",
                    forecastDay("2026-11-11", {
                        rows: forecastRows([{ end: "2026-11-11T08:00:00Z" }]),
                    }),
                ],
                [
                    "with milliseconds in an interval time",
                    forecastDay("2026-11-11", {
                        rows: forecastRows([
                            { start: "2026-11-11T09:00:00.000Z" },
                        ]),
                    }),
                ],
                [
                    "with intervals that overlap by less than a millisecond",
                    forecastDay("2026-11-11", {
                        rows: forecastRows([
                            { end: "2026-11-11T10:00:00.000900Z" },
                            { start: "2026-11-11T10:00:00.000100Z" },
                        ]),
                    }),
                ],
                [
                    "with an interval time without seconds",
                    forecastDay("2026-11-11", {
                        rows: forecastRows([{ start: "2026-11-11T09:00Z" }]),
                    }),
                ],
                [
                    "with a fraction of a second in a production interval",
                    productionDay("2026-11-11", {
                        rows: productionRows([
                            {},
                            {},
                            { end: "2026-11-11T12:00:00.5Z" },
                        ]),
                    }),
                ],
                [
                    "with energy with outages but no outage scenario",
                    forecastDay("2026-11-11", {
                        rows: forecastRows([{ affected_mwh: 1000 }]),
                    }),
                ],
                [
                    "with forecast intervals none of which has a figure",
                    forecastDay("2026-11-11", {
                        total_mwh: null,
                        rows: forecastRows([{ mwh: null }, { mwh: null }]),
                    }),
                ],
                [
                    "with production intervals none of which has a figure",
                    productionDay("2026-11-11", {
                        total_mwh: null,
                        rows: productionRows([]).slice(2),
                    }),
                ],
                [
                    "whose intervals are a hole",
                    forecastDay("2026-11-11", { rows: new Array(1) }),
                ],
                [
                    "with a hole after its forecast intervals",
                    forecastDay("2026-11-11", {
                        rows: Object.assign(forecastRows([]), { length: 4 }),
                    }),
                ],
                [
                    "with a hole in the sources of an interval",
                    forecastDay("2026-11-11", {
                        source_kind: "mixed",
                        rows: forecastRows([
                            {
                                sources: Object.assign(["forecast"], {
                                    length: 2,
                                }),
                            },
                        ]),
                    }),
                ],
                [
                    "with a hole after its production intervals",
                    productionDay("2026-11-11", {
                        rows: Object.assign(productionRows([]), { length: 4 }),
                    }),
                ],
                [
                    "with an interval on the next date",
                    forecastDay("2026-11-11", {
                        rows: forecastRows([
                            {},
                            {},
                            {
                                start: "2026-11-12T09:00:00Z",
                                end: "2026-11-12T10:00:00Z",
                            },
                        ]),
                    }),
                ],
                [
                    "with an interval that ends after the local midnight",
                    forecastDay("2026-11-11", {
                        timezone: "Asia/Tokyo",
                        rows: energyRows("2026-11-11T14:00:00Z", [1, 1]),
                    }),
                ],
                [
                    "with production intervals on another local date",
                    productionDay("2026-11-11", {
                        timezone: "Pacific/Kiritimati",
                    }),
                ],
                [
                    "with intervals in reverse order",
                    forecastDay("2026-11-11", {
                        rows: forecastRows([]).reverse(),
                    }),
                ],
                [
                    "with sources on a day that is not mixed",
                    forecastDay("2026-11-11", {
                        rows: forecastRows([{ sources: ["forecast"] }]),
                    }),
                ],
                [
                    "with production marks on a missing value",
                    productionDay("2026-11-11", {
                        rows: productionRows([
                            {},
                            {},
                            { quality: "valid", provisional: true },
                        ]),
                    }),
                ],
                [
                    "with a production value without its quality",
                    productionDay("2026-11-11", {
                        rows: productionRows([{ quality: null }]),
                    }),
                ],
                [
                    "with negative production",
                    productionDay("2026-11-11", {
                        rows: productionRows([{ mwh: -0.25 }]),
                    }),
                ],
            ];
            for (const [name, day] of ruledOut)
                test(`leaves out a day ${name} and keeps its neighbours`, async () => {
                    const kind = day.kind as "forecast" | "production";
                    const neighbour =
                        kind === "forecast"
                            ? forecastDay("2026-11-12")
                            : productionDay("2026-11-12");
                    const body = await savedPage(
                        plantAddress(kind, "2026-11-11"),
                        copy({ days: [day, neighbour] }),
                    );
                    expect(shownDay(body)).toBeUndefined();
                    expect(heldDates(body)).toEqual(["2026-11-12"]);
                });

            test("does not show a day record that does not match the format", async () => {
                const missing: Row = forecastDay("2026-11-11");
                delete missing.collected_at;
                const broken = [
                    missing,
                    forecastDay("2026-11-11", { total_mwh: "1" }),
                    forecastDay("2026-11-11", { rows: [{ start: "soon" }] }),
                    forecastDay("2026-11-11", { timezone: "Mars/Olympus" }),
                    forecastDay("2026-11-11", { kind: "production" }),
                    forecastDay("2026-11-11", {
                        affected: { status: "estimated" },
                    }),
                    forecastDay("2026-11-11", {
                        rows: energyRows(
                            "2026-11-11T00:00:00Z",
                            Array.from({ length: 53 }, () => 1),
                            HOUR / 4,
                        ),
                    }),
                ];
                for (const day of broken) {
                    const body = await savedPage(
                        plantAddress("forecast", "2026-11-11"),
                        copy({ days: [day] }),
                    );
                    expect(
                        shownDay(body),
                        JSON.stringify(day).slice(0, 80),
                    ).toBeUndefined();
                    expect(body).toContain(
                        "No figures of this kind are saved for this plant yet.",
                    );
                }
            });

            test("shows the days of a days-read answer as the backend emits them", async () => {
                const answer: DaysAnswer = {
                    plant_id: KEY.plant,
                    name: "North field",
                    today: "2026-11-11",
                    forecast: [
                        {
                            date: "2026-11-12",
                            timezone: "Europe/Istanbul",
                            source_kind: "forecast",
                            freshness: "fresh",
                            stale_reason: null,
                            unavailable_reason: null,
                            collected_at: "2026-11-11T06:00:00.123456Z",
                            total_mwh: 1.5,
                            partial: false,
                            affected: null,
                            rows: energyRows(
                                "2026-11-11T21:00:00Z",
                                Array.from({ length: 24 }, (_, hour) =>
                                    hour === 12 ? 1.5 : 0,
                                ),
                            ),
                        },
                    ],
                    production: [
                        {
                            date: "2026-11-10",
                            timezone: "Europe/Istanbul",
                            total_mwh: 0.5,
                            complete: false,
                            rows: intervals("2026-11-09T21:00:00Z", 24).map(
                                (interval, hour) =>
                                    hour === 12
                                        ? {
                                              ...interval,
                                              mwh: 0.5,
                                              quality: "valid",
                                              source: "epias",
                                              provisional: false,
                                          }
                                        : {
                                              ...interval,
                                              mwh: null,
                                              quality: null,
                                              source: null,
                                              provisional: null,
                                          },
                            ),
                        },
                    ],
                };
                const database = copy({
                    days: [
                        ...answer.forecast.map((day) => ({
                            ...KEY,
                            kind: "forecast",
                            ...day,
                        })),
                        ...answer.production.map((day) => ({
                            ...KEY,
                            kind: "production",
                            ...day,
                        })),
                    ],
                });
                const forecast = await savedPage(
                    plantAddress("forecast", "2026-11-12"),
                    database,
                );
                expect(shownDay(forecast)).toBe("2026-11-12");
                expect(tableRows(forecast)).toHaveLength(24);
                const production = await savedPage(
                    plantAddress("production", "2026-11-10"),
                    database,
                );
                expect(shownDay(production)).toBe("2026-11-10");
                expect(tableRows(production)).toHaveLength(24);
            });

            test("shows a day of the most rows a local day has", async () => {
                const rows = energyRows(
                    "2026-10-24T22:00:00Z",
                    Array.from({ length: 52 }, () => 1),
                    HOUR / 2,
                );
                const body = await savedPage(
                    plantAddress("forecast", "2026-10-25"),
                    copy({
                        days: [
                            forecastDay("2026-10-25", {
                                timezone: "Antarctica/Troll",
                                rows,
                            }),
                        ],
                    }),
                );
                expect(tableRows(body)).toHaveLength(52);
            });

            test("shows a day at the edge of each rule of the format", async () => {
                const edges: Row[] = [
                    forecastDay("2026-11-11", {
                        total_mwh: 2,
                        rows: energyRows("2026-11-11T09:00:00Z", [
                            null,
                            2,
                            null,
                        ]),
                    }),
                    forecastDay("2026-11-11", {
                        affected: {
                            status: "estimated",
                            total_mwh: 1000,
                            partial: false,
                        },
                        rows: forecastDay("2026-11-11").rows.map((row) => ({
                            ...row,
                            affected_mwh: 1000,
                        })),
                    }),
                    forecastDay("2026-11-11", {
                        timezone: "Asia/Tokyo",
                        rows: energyRows("2026-11-10T15:00:00Z", [1, 1]).concat(
                            energyRows("2026-11-11T14:00:00Z", [1]),
                        ),
                    }),
                    forecastDay("2026-11-11", {
                        timezone: "Europe/Istanbul",
                        rows: [
                            {
                                start: "2026-11-11T12:00:00+03:00",
                                end: "2026-11-11T13:00:00+03:00",
                                mwh: 1,
                                affected_mwh: null,
                                sources: null,
                            },
                        ],
                    }),
                    productionDay("2026-11-11", {
                        total_mwh: 0.25,
                        rows: productionDay("2026-11-11").rows.map(
                            (row, index) =>
                                index === 0
                                    ? row
                                    : {
                                          ...row,
                                          mwh: null,
                                          quality: null,
                                          source: null,
                                          provisional: null,
                                      },
                        ),
                    }),
                ];
                for (const day of edges) {
                    const body = await savedPage(
                        plantAddress(
                            day.kind as "forecast" | "production",
                            "2026-11-11",
                        ),
                        copy({ days: [day] }),
                    );
                    expect(
                        shownDay(body),
                        JSON.stringify(day).slice(0, 120),
                    ).toBe("2026-11-11");
                }
            });
        });

        test.describe("the notice without a copy that can be shown", () => {
            test("appears without a database and creates none", async () => {
                const database = savedDatabase();
                expectNotice(await savedPage(plantAddress(), database));
                expect(database.opens).toEqual(["pyracast-saved"]);
                expect(database.exists()).toBe(false);
            });

            test("appears for another format number", async () => {
                const database = savedDatabase({
                    version: 2,
                    records: { holder: holder(), plants: [plant()] },
                });
                expectNotice(await savedPage(plantAddress(), database));
                expect(database.transactions).toEqual([]);
                expect(database.openConnections()).toBe(0);
            });

            test("appears for a missing or malformed holder", async () => {
                for (const value of [
                    undefined,
                    holder({ language: "de" }),
                    holder({ removal: "short" }),
                    holder({ confirmedAt: null }),
                    "holder",
                ])
                    expectNotice(
                        await savedPage(
                            plantAddress(),
                            copy({ holder: value }),
                        ),
                    );
            });

            test("appears for a holder time no date can represent, and the copy stays", async () => {
                for (const value of [
                    holder({ confirmedAt: 1e20 }),
                    holder({ confirmedAt: -1e20 }),
                    holder({ confirmedAt: NOW - DAY + 0.5 }),
                    holder({ retryAt: 1e20 }),
                ]) {
                    const database = copy({
                        holder: value,
                        days: [forecastDay("2026-11-11")],
                    });
                    expectNotice(await savedPage(plantAddress(), database));
                    expect(database.plants()).toHaveLength(1);
                    expect(database.days()).toHaveLength(1);
                    expect(database.transactions).toEqual([
                        "readonly holder,plants,days",
                    ]);
                }
            });

            test("appears within the read limit when the database never answers", async () => {
                const delays: number[] = [];
                const body = await savedPage(
                    plantAddress(),
                    savedDatabase({ answers: false }),
                    {
                        setTimeout: (run, delay) => {
                            delays.push(delay);
                            run();
                        },
                    },
                );
                expectNotice(body);
                expect(delays).toEqual([3000]);
            });

            for (const [stage, event] of [
                ["open", "opens"],
                ["readonly", "is read"],
            ] as const)
                test(`appears when the copy ${event} after the read limit before the limit's timer has run`, async () => {
                    const database = copy({
                        days: [forecastDay("2026-11-11")],
                    });
                    const ticks = clock(0);
                    const late = database.pause(stage);
                    const answer = visit(
                        savedWorker(database, {
                            ...manualTimers(),
                            monotonic: ticks.now,
                        }),
                        plantAddress("forecast", "2026-11-11"),
                    );
                    await late.reached;
                    ticks.advance(3000);
                    late.release();
                    expectNotice(await (await answer).text());
                    await database.idle();
                    expect(database.transactions).toEqual(
                        stage === "open" ? [] : ["readonly holder,plants,days"],
                    );
                    expect(database.plants()).toHaveLength(1);
                });

            test("appears when reading the copy fails", async () => {
                const database = copy();
                database.indexedDB.open = () => {
                    throw new DOMException("denied", "SecurityError");
                };
                expectNotice(await savedPage(plantAddress(), database));
            });

            test("is in the holder's language for an inactive copy and changes nothing", async () => {
                const database = copy({
                    holder: holder({ active: false, language: "tr" }),
                    days: [forecastDay("2026-11-11")],
                });
                const body = await savedPage(
                    plantAddress("forecast", "2026-11-11"),
                    database,
                );
                expect(body).toContain('<html lang="tr">');
                expect(links(body)).toEqual([["", "Tekrar dene"]]);
                expect(database.plants()).toHaveLength(1);
                expect(database.transactions).toEqual([
                    "readonly holder,plants,days",
                ]);
            });

            for (const { name, marks, shown } of refusalCases(7, "tag-1"))
                test(`${shown ? "does not appear" : "appears"} for an active copy ${name}, and the copy stays`, async () => {
                    const database = copy({
                        marks,
                        days: [forecastDay("2026-11-11")],
                    });
                    const body = await savedPage(
                        plantAddress("forecast", "2026-11-11"),
                        database,
                    );
                    expect(body.includes("<h1>North field</h1>")).toBe(shown);
                    expect(database.plants()).toHaveLength(1);
                    expect(database.days()).toHaveLength(1);
                });
        });

        test.describe("a copy outside its 90 days", () => {
            for (const [name, confirmedAt] of [
                ["more than 90 days old", NOW - 90 * DAY - 1],
                ["more than a day ahead of the clock", NOW + DAY + 1],
            ] as const)
                test(`is emptied when ${name} and the notice says so once`, async () => {
                    const database = copy({
                        holder: holder({ confirmedAt }),
                        days: [forecastDay("2026-11-11")],
                    });
                    const first = await savedPage(
                        plantAddress("forecast", "2026-11-11"),
                        database,
                    );
                    expect(first).toContain(EXPIRED);
                    expect(first).not.toContain("North field");
                    expect(database.plants()).toEqual([]);
                    expect(database.days()).toEqual([]);
                    expect(database.holder()).toEqual(holder({ confirmedAt }));
                    const second = await savedPage(
                        plantAddress("forecast", "2026-11-11"),
                        database,
                    );
                    expect(second).not.toContain("were removed");
                    expect(links(second)).toEqual([["", "Try again"]]);
                });

            test("is shown up to its last day", async () => {
                const body = await savedPage(
                    plantAddress("forecast", "2026-11-11"),
                    copy({
                        holder: holder({ confirmedAt: NOW - 90 * DAY }),
                        days: [forecastDay("2026-11-11")],
                    }),
                );
                expect(shownDay(body)).toBe("2026-11-11");
            });

            test("is not shown when it passes its last day while it is read", async () => {
                const database = copy({
                    holder: holder({ confirmedAt: NOW - 90 * DAY }),
                    days: [forecastDay("2026-11-11")],
                });
                const time = clock(NOW);
                const read = database.pause("readonly");
                const answer = visit(
                    savedWorker(database, { now: time.now }),
                    plantAddress("forecast", "2026-11-11"),
                );
                await read.reached;
                time.advance(200);
                read.release();
                const body = await (await answer).text();
                expect(body).toContain(EXPIRED);
                expect(body).not.toContain("North field");
                expect(database.plants()).toEqual([]);
                expect(database.days()).toEqual([]);
            });

            test("is not emptied when it comes within a day of the clock while it is read", async () => {
                const database = copy({
                    holder: holder({ confirmedAt: NOW + DAY + 1 }),
                    days: [forecastDay("2026-11-11")],
                });
                const time = clock(NOW);
                const read = database.pause("readonly");
                const answer = visit(
                    savedWorker(database, { now: time.now }),
                    plantAddress("forecast", "2026-11-11"),
                );
                await read.reached;
                time.advance(2);
                read.release();
                const body = await (await answer).text();
                expect(shownDay(body)).toBe("2026-11-11");
                expect(database.plants()).toHaveLength(1);
                expect(database.days()).toHaveLength(1);
                expect(database.transactions).toEqual([
                    "readonly holder,plants,days",
                ]);
            });

            test("is not emptied when it comes within a day of the clock before the removal starts", async () => {
                const database = copy({
                    holder: holder({ confirmedAt: NOW + DAY + 1 }),
                    days: [forecastDay("2026-11-11")],
                });
                const time = clock(NOW);
                const write = database.pause("readwrite");
                const answer = visit(
                    savedWorker(database, { now: time.now }),
                    plantAddress("forecast", "2026-11-11"),
                );
                await write.reached;
                time.advance(2);
                write.release();
                const body = await (await answer).text();
                expect(body).not.toContain(EXPIRED);
                expect(shownDay(body)).toBe("2026-11-11");
                expect(database.plants()).toHaveLength(1);
                expect(database.days()).toHaveLength(1);
            });

            test("says so once when two navigations overlap", async () => {
                const confirmedAt = NOW - 91 * DAY;
                const database = copy({
                    holder: holder({ confirmedAt }),
                    days: [forecastDay("2026-11-11")],
                });
                const reads = [
                    database.pause("readonly"),
                    database.pause("readonly", 1),
                ];
                const worker = savedWorker(database);
                const answers = [1, 2].map(() =>
                    visit(worker, plantAddress("forecast", "2026-11-11")),
                );
                await reads[1]!.created;
                for (const read of reads) read.release();
                const bodies = await Promise.all(
                    answers.map(async (answer) => (await answer).text()),
                );
                expect(
                    bodies.filter((body) => body.includes(EXPIRED)),
                ).toHaveLength(1);
                expect(
                    bodies.some((body) => body.includes("North field")),
                ).toBe(false);
                expect(database.transactions).toEqual([
                    "readonly holder,plants,days",
                    "readonly holder,plants,days",
                    "readwrite holder,plants,days",
                    "readwrite holder,plants,days",
                ]);
                expect(database.plants()).toEqual([]);
                expect(database.holder()).toEqual(holder({ confirmedAt }));
            });

            test("is not emptied when the holder was confirmed again before the removal", async () => {
                const database = copy({
                    holder: holder({ confirmedAt: NOW - 91 * DAY }),
                    days: [forecastDay("2026-11-11")],
                });
                const write = database.pause("readwrite");
                const answer = visit(
                    savedWorker(database),
                    plantAddress("forecast", "2026-11-11"),
                );
                await write.reached;
                database.replace({
                    holder: holder(),
                    plants: [plant()],
                    days: [forecastDay("2026-11-11", { total_mwh: 4.2 })],
                });
                write.release();
                const body = await (await answer).text();
                expect(body).not.toContain(EXPIRED);
                expect(shownDay(body)).toBe("2026-11-11");
                expect(body).toContain(totalText("Day total", "4.2"));
                expect(body).not.toContain(totalText("Day total", "1.735"));
                expect(database.plants()).toHaveLength(1);
                expect(database.days()).toHaveLength(1);
            });
        });

        test.describe("a removal that does not finish", () => {
            const REMOVALS = [
                {
                    name: "the expiry",
                    address: plantAddress("forecast", "2026-11-11"),
                    holder: holder({ confirmedAt: NOW - 91 * DAY }),
                },
                {
                    name: "the removal",
                    address: `/app/?saved=remove&token=${TOKEN}`,
                    holder: holder(),
                },
            ];
            const held = (stored: Row) =>
                copy({ holder: stored, days: [forecastDay("2026-11-11")] });
            const expectKept = async (database: SavedDatabase, stored: Row) => {
                await database.idle();
                expect(database.plants()).toHaveLength(1);
                expect(database.days()).toHaveLength(1);
                expect(database.holder()).toEqual(stored);
                expect(database.openConnections()).toBe(0);
            };

            for (const removal of REMOVALS) {
                test(`answers the notice and keeps the copy when ${removal.name} aborts`, async () => {
                    const database = held(removal.holder);
                    const write = database.pause("readwrite");
                    const answer = visit(
                        savedWorker(database),
                        removal.address,
                    );
                    await write.reached;
                    write.fail();
                    expectNotice(await (await answer).text());
                    await expectKept(database, removal.holder);
                });

                test(`answers the notice at the limit and writes nothing later when ${removal.name} has not started`, async () => {
                    const database = held(removal.holder);
                    const timers = manualTimers();
                    const write = database.pause("readwrite");
                    const answer = visit(
                        savedWorker(database, timers),
                        removal.address,
                    );
                    await write.reached;
                    timers.fire();
                    expectNotice(await (await answer).text());
                    write.release();
                    await expectKept(database, removal.holder);
                });

                test(`answers the notice at the limit and closes a connection opened later when ${removal.name} waits for the copy`, async () => {
                    const database = held(removal.holder);
                    const timers = manualTimers();
                    const opening = database.pause("open", 1);
                    const answer = visit(
                        savedWorker(database, timers),
                        removal.address,
                    );
                    await opening.reached;
                    timers.fire();
                    expectNotice(await (await answer).text());
                    opening.release();
                    await expectKept(database, removal.holder);
                    expect(database.transactions).not.toContain(
                        "readwrite holder,plants,days",
                    );
                });

                test(`answers the notice and writes nothing when ${removal.name} gets the copy after its limit before the limit's timer has run`, async () => {
                    const database = held(removal.holder);
                    const ticks = clock(0);
                    const opening = database.pause("open", 1);
                    const answer = visit(
                        savedWorker(database, {
                            ...manualTimers(),
                            monotonic: ticks.now,
                        }),
                        removal.address,
                    );
                    await opening.reached;
                    ticks.advance(3000);
                    opening.release();
                    expectNotice(await (await answer).text());
                    await expectKept(database, removal.holder);
                    expect(database.transactions).not.toContain(
                        "readwrite holder,plants,days",
                    );
                });

                test(`answers the notice and writes nothing when ${removal.name} starts after its limit before the limit's timer has run`, async () => {
                    const database = held(removal.holder);
                    const ticks = clock(0);
                    const write = database.pause("readwrite");
                    const answer = visit(
                        savedWorker(database, {
                            ...manualTimers(),
                            monotonic: ticks.now,
                        }),
                        removal.address,
                    );
                    await write.reached;
                    ticks.advance(3000);
                    write.release();
                    expectNotice(await (await answer).text());
                    await expectKept(database, removal.holder);
                });
            }
        });

        test.describe("the removal on a saved page", () => {
            test("asks for a confirmation whose link carries the copy's token", async () => {
                const database = copy({ days: [forecastDay("2026-11-11")] });
                const body = await savedPage("/app/?saved=remove", database);
                expect(body).toContain(
                    "<h2>Remove saved figures from this device?</h2>",
                );
                expect(body).toContain(
                    `<a href="/app/?saved=remove&amp;token=${TOKEN}">Remove saved figures</a>`,
                );
                expect(links(body)).toContainEqual([
                    "/app/",
                    "Keep the saved figures",
                ]);
                expect(database.plants()).toHaveLength(1);
            });

            test("with the stored token empties the copy and says so", async () => {
                const database = copy({ days: [forecastDay("2026-11-11")] });
                const body = await savedPage(
                    `/app/?saved=remove&token=${TOKEN}`,
                    database,
                );
                expect(body).toContain(`<p>${REMOVED}</p>`);
                expect(links(body)).toEqual([["", "Try again"]]);
                expect(database.plants()).toEqual([]);
                expect(database.days()).toEqual([]);
                expect(database.holder()).toEqual(holder({ active: false }));
                const after = await savedPage(
                    plantAddress("forecast", "2026-11-11"),
                    database,
                );
                expect(after).not.toContain("Plant day");
            });

            test("with the stored token a second time says nothing was removed and writes nothing", async () => {
                const database = copy({ days: [forecastDay("2026-11-11")] });
                const address = `/app/?saved=remove&token=${TOKEN}`;
                await savedPage(address, database);
                const written = database.transactions.length;
                const again = await savedPage(address, database);
                expect(again).not.toContain(REMOVED);
                expect(links(again)).toEqual([["", "Try again"]]);
                expect(database.transactions.slice(written)).toEqual([
                    "readonly holder,plants,days",
                ]);
            });

            test("says so once when two navigations with the stored token overlap", async () => {
                const database = copy({ days: [forecastDay("2026-11-11")] });
                const reads = [
                    database.pause("readonly"),
                    database.pause("readonly", 1),
                ];
                const worker = savedWorker(database);
                const answers = [1, 2].map(() =>
                    visit(worker, `/app/?saved=remove&token=${TOKEN}`),
                );
                await reads[1]!.created;
                for (const read of reads) read.release();
                const bodies = await Promise.all(
                    answers.map(async (answer) => (await answer).text()),
                );
                expect(
                    bodies.filter((body) => body.includes(REMOVED)),
                ).toHaveLength(1);
                expect(
                    bodies.some((body) => body.includes("North field")),
                ).toBe(false);
                expect(database.plants()).toEqual([]);
                expect(database.holder()).toEqual(holder({ active: false }));
            });

            test("shows the copy of another holder met by the removal instead of removing it", async () => {
                const database = copy({ days: [forecastDay("2026-11-11")] });
                const other = holder({
                    user: 9,
                    language: "tr",
                    removal: "f".repeat(32),
                });
                const write = database.pause("readwrite");
                const answer = visit(
                    savedWorker(database),
                    `/app/?saved=remove&token=${TOKEN}`,
                );
                await write.reached;
                database.replace({
                    holder: other,
                    plants: [plant({ site: 6, plant: 9, name: "South field" })],
                });
                write.release();
                const body = await (await answer).text();
                expect(body).toContain('<html lang="tr">');
                expect(body).not.toContain("silindi");
                expect(links(body)).toContainEqual([
                    routes.plant(3, 6),
                    "South field",
                ]);
                expect(body).not.toContain("North field");
                expect(database.plants()).toEqual([
                    plant({ site: 6, plant: 9, name: "South field" }),
                ]);
                expect(database.holder()).toEqual(other);
            });

            test("accepts a stored token in capital hex and compares the given one exactly", async () => {
                const upper = TOKEN.toUpperCase();
                const database = copy({
                    holder: holder({ removal: upper }),
                    days: [forecastDay("2026-11-11")],
                });
                expect(
                    shownDay(
                        await savedPage(
                            plantAddress("forecast", "2026-11-11"),
                            database,
                        ),
                    ),
                ).toBe("2026-11-11");
                expect(
                    await savedPage("/app/?saved=remove", database),
                ).toContain(`token=${upper}"`);
                await savedPage(`/app/?saved=remove&token=${TOKEN}`, database);
                expect(database.plants()).toHaveLength(1);
                expect(
                    await savedPage(
                        `/app/?saved=remove&token=${upper}`,
                        database,
                    ),
                ).toContain(REMOVED);
                expect(database.plants()).toEqual([]);
            });

            test("with another token removes nothing", async () => {
                for (const token of ["", TOKEN.toUpperCase(), "f".repeat(32)]) {
                    const database = copy({
                        days: [forecastDay("2026-11-11")],
                    });
                    const body = await savedPage(
                        `/app/?saved=remove&token=${token}`,
                        database,
                    );
                    expect(body).not.toContain("were removed");
                    expect(database.plants()).toHaveLength(1);
                    expect(database.days()).toHaveLength(1);
                    expect(database.holder()).toEqual(holder());
                    expect(database.transactions).toEqual([
                        "readonly holder,plants,days",
                    ]);
                }
            });
        });

        test("the worker does not open the copy when the navigation is answered, whatever the status", async () => {
            for (const status of [200, 404, 500, 503]) {
                const database = copy();
                const worker = loadScript("../public/app/sw.js", { database });
                const served = new Response("served", { status });
                worker.answerWith(() => Promise.resolve(served));
                const { answer } = await worker.navigate({
                    url: `${ORIGIN}${plantAddress()}`,
                });
                expect(answer).toBe(served);
                const preloaded = await worker.navigate({
                    url: `${ORIGIN}${plantAddress()}`,
                    preloadResponse: Promise.resolve(
                        new Response("", { status }),
                    ),
                });
                expect(preloaded.answer?.status).toBe(status);
                expect(database.opens).toEqual([]);
            }
        });

        test("the worker closes every connection it opens", async () => {
            const database = copy({ days: [forecastDay("2026-11-11")] });
            await savedPage(plantAddress("forecast", "2026-11-11"), database);
            await savedPage(`/app/?saved=remove&token=${TOKEN}`, database);
            expect(database.opens).toHaveLength(3);
            expect(database.openConnections()).toBe(0);
        });
    });

test.describe("the in-memory saved database", () => {
    type Transaction = {
        abort: () => void;
        oncomplete: (() => void) | null;
        onabort: (() => void) | null;
    };
    type Connection = {
        transaction: (names: string[], mode: "readwrite") => Transaction;
        close: () => void;
    };

    test("starts a transaction only after the one holding the lock ends when a queued one was aborted before its turn", async () => {
        const database = copy();
        const third = database.pause("readwrite", 2);
        const request = database.indexedDB.open("pyracast-saved");
        const connection = await new Promise<Connection>((resolve) => {
            request.onsuccess = () => resolve(request.result as Connection);
        });
        const order: string[] = [];
        const write = (name: string) => {
            const transaction = connection.transaction(["plants"], "readwrite");
            transaction.oncomplete = () => order.push(name);
            transaction.onabort = () => order.push(`${name} aborted`);
            return transaction;
        };
        const first = write("A");
        const second = write("B");
        write("C");
        write("D");
        first.oncomplete = () => {
            order.push("A");
            second.abort();
        };
        await third.reached;
        third.release();
        connection.close();
        await database.idle();
        expect(order).toEqual(["A", "B aborted", "C", "D"]);
    });
});
