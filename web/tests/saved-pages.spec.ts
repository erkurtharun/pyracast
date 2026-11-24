import { expect, test } from "@playwright/test";
import { routes } from "../src/lib/routes";
import { instant } from "./support/test";
import { savedDatabase, type SavedRecords } from "./support/worker-database";
import { loadScript, SCOPE, type ScriptOptions } from "./support/worker-script";
import { workerWithdrawn } from "./support/worker-state";

const NOW = instant.getTime();
const HOUR = 3_600_000;
const DAY = 24 * HOUR;
const ORIGIN = new URL(SCOPE).origin;
const TOKEN = "0123456789abcdef0123456789abcdef";
const KEY = { tenant: 3, site: 5, plant: 8 };
const NOTICE_POLICY =
    "default-src 'none'; style-src 'unsafe-inline'; form-action 'none'; base-uri 'none'; frame-ancestors 'none'";

type Language = "en" | "tr";
type Row = Record<string, unknown>;

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

function hours(date: string, count: number, from = 9) {
    const start = Date.parse(`${date}T00:00:00Z`) + from * HOUR;
    return Array.from({ length: count }, (_, index) => ({
        start: new Date(start + index * HOUR).toISOString(),
        end: new Date(start + (index + 1) * HOUR).toISOString(),
    }));
}

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

async function savedAnswer(
    address: string,
    database = copy(),
    options: ScriptOptions = {},
) {
    const worker = loadScript("../public/app/sw.js", {
        database,
        now: NOW,
        zone: "Europe/Istanbul",
        ...options,
    });
    const { answer } = await worker.navigate({ url: `${ORIGIN}${address}` });
    expect(answer).toBeDefined();
    return { response: answer!, body: await answer!.text(), database };
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
    [...body.matchAll(/<a href="([^"]*)">([^<]*)<\/a>/g)].map(
        ([, href = "", text = ""]) => [href.replaceAll("&amp;", "&"), text],
    );

const tableRows = (body: string) =>
    [...body.matchAll(/<tr><td>(.*?)<\/tr>/g)].map(([row]) =>
        [...row.matchAll(/<td[^>]*>([^<]*)<\/td>/g)].map(([, cell]) => cell),
    );

const bars = (body: string, kind: string) =>
    body.match(new RegExp(`<rect class="${kind}"`, "g"))?.length ?? 0;

const shownDay = (body: string) => body.match(/Plant day (\S+),/)?.[1];

const bothLanguages = (body: string) =>
    body.includes('<section lang="en">') &&
    body.includes('<section lang="tr">');

const LABELS: Record<Language, Record<string, string>> = {
    en: {
        title: "<h1>Pyracast cannot be reached</h1>",
        read: "Read from the server on Nov 10, 2026, 12:00 GMT+3, 3 hours ago.",
        day: "Plant day 2026-11-11, time zone UTC.",
        source: "Source: Estimated actuals.",
        collected: "Source data collected on Nov 10, 2026, 09:00 GMT+3.",
        stale: "The newest provider collection is more than a day old.",
        total: "Day total: 1.735 MWh (partial estimate)",
        affected: "With outages: 1.2 MWh",
        heading: "<h2>North field, Solar Co: Forecast</h2>",
        caption: "<caption>Energy per interval</caption>",
    },
    tr: {
        title: "<h1>Pyracast'e ulaşılamıyor</h1>",
        read: "Sunucudan okunma: 10 Kas 2026 12:00 GMT+3, 3 saat önce.",
        day: "Santral günü 2026-11-11, saat dilimi UTC.",
        source: "Kaynak: Gerçekleşen tahmini.",
        collected: "Kaynak verinin alınma zamanı: 10 Kas 2026 09:00 GMT+3.",
        stale: "Sağlayıcıdan alınan en yeni veri bir günden eski.",
        total: "Gün toplamı: 1,735 MWh (kısmi tahmin)",
        affected: "Kesintiler dahil: 1,2 MWh",
        heading: "<h2>North field, Solar Co: Tahmin</h2>",
        caption: "<caption>Aralık başına enerji</caption>",
    },
};

if (!workerWithdrawn)
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
                    expect(body).toContain('<a href="">');
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
                expect(body).toContain("With outages: — MWh");
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
                    "With outages: 1 MWh (partial estimate)",
                );
            });

            test("writes an interval of a 23 and of a 25 hour day once each, naming the offset where it changes", async () => {
                const day = (date: string, start: string, count: number) =>
                    forecastDay(date, {
                        timezone: "Europe/Berlin",
                        rows: Array.from({ length: count }, (_, index) => ({
                            start: new Date(
                                Date.parse(start) + index * HOUR,
                            ).toISOString(),
                            end: new Date(
                                Date.parse(start) + (index + 1) * HOUR,
                            ).toISOString(),
                            mwh: index,
                            affected_mwh: null,
                            sources: null,
                        })),
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
                expect(bars(short, "energy")).toBe(23);
                expect(tableRows(short)[2]?.[0]).toBe(
                    "03:00 GMT+2–04:00 GMT+2",
                );
                const intervals = tableRows(long).map(([interval]) => interval);
                expect(intervals).toHaveLength(25);
                expect(bars(long, "energy")).toBe(25);
                expect(new Set(intervals).size).toBe(25);
                expect(intervals.slice(2, 4)).toEqual([
                    "02:00 GMT+2–02:00 GMT+1",
                    "02:00 GMT+1–03:00 GMT+1",
                ]);
            });
        });

        test.describe("a saved production page", () => {
            test("shows the day's marks, total and table", async () => {
                const body = await savedPage(
                    plantAddress("production", "2026-11-09"),
                    copy({ days: [productionDay("2026-11-09")] }),
                );
                expect(body).toContain(
                    "<h2>North field, Solar Co: Production</h2>",
                );
                expect(body).toContain("Plant day 2026-11-09, time zone UTC.");
                expect(body).toContain('<p class="mark">Incomplete day</p>');
                expect(body).toContain("Day total: 0.75 MWh");
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
                ]);
                expect(body).not.toContain("<svg");
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
                expect(body).toContain("Gün toplamı: 0,75 MWh");
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
                        forecastDay(date, { timezone: "Pacific/Kiritimati" }),
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
                    ["", "Try again"],
                    [
                        plantAddress("forecast", "2026-11-09"),
                        "Previous saved day",
                    ],
                    [plantAddress("forecast", "2026-11-12"), "Next saved day"],
                    [plantAddress("production"), "Production"],
                    [routes.entry(), "Saved plants"],
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
                    "Try again",
                    "Forecast",
                    "Saved plants",
                    "Remove saved figures from this device",
                ]);
                expect(links(body)[1]?.[0]).toBe(plantAddress("forecast"));
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
                expect(list).toContain(
                    `>${escaped(name)}</a>, ${escaped(workspace)}.`,
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
                expect(body).toContain("<h2>Bu cihazda kayıtlı rakamlar</h2>");
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
                    "/app/account/",
                ]) {
                    const body = await savedPage(address);
                    expect(links(body), address).toContainEqual([
                        routes.plant(3, 5),
                        "North field",
                    ]);
                    expect(body, address).not.toContain("Plant day");
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
                        ],
                    }),
                );
                expect(links(body).map(([href]) => href)).toEqual([
                    "",
                    routes.plant(3, 5),
                    "/app/?saved=remove",
                ]);
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
                        rows: Array.from({ length: 53 }, () => ({
                            start: "2026-11-11T00:00:00Z",
                            end: "2026-11-11T00:30:00Z",
                            mwh: 1,
                            affected_mwh: null,
                            sources: null,
                        })),
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

            test("shows a day of the most rows a local day has", async () => {
                const rows = Array.from({ length: 52 }, (_, index) => ({
                    start: new Date(
                        Date.parse("2026-10-24T22:00:00Z") + (index * HOUR) / 2,
                    ).toISOString(),
                    end: new Date(
                        Date.parse("2026-10-24T22:30:00Z") + (index * HOUR) / 2,
                    ).toISOString(),
                    mwh: 1,
                    affected_mwh: null,
                    sources: null,
                }));
                const body = await savedPage(
                    plantAddress("forecast", "2026-10-25"),
                    copy({ days: [forecastDay("2026-10-25", { rows })] }),
                );
                expect(tableRows(body)).toHaveLength(52);
            });
        });

        test.describe("the notice without a copy that can be shown", () => {
            const expectNotice = (body: string) => {
                expect(bothLanguages(body)).toBe(true);
                expect(links(body)).toEqual([
                    ["", "Try again"],
                    ["", "Tekrar dene"],
                ]);
            };

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
                    expect(first).toContain(
                        "The saved figures were removed from this device because no one signed in here for 90 days or the device clock was changed.",
                    );
                    expect(first).not.toContain("North field");
                    expect(database.plants()).toEqual([]);
                    expect(database.days()).toEqual([]);
                    expect(database.holder()).toEqual(
                        holder({ confirmedAt, active: false }),
                    );
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
                expect(body).toContain(
                    "<p>The saved figures were removed from this device.</p>",
                );
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
