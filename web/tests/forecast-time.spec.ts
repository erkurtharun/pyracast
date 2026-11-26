import type { Locator } from "@playwright/test";
import { expect, test, type Page } from "./support/test";
import { mockApi, region } from "./support/workspace";
import { messages } from "../src/i18n/messages";

const fetches = [
    { forecast_id: 12, fetched_at: "2026-11-10T14:00:00Z", energy: 11.25 },
    { forecast_id: 11, fetched_at: "2026-11-10T03:00:00Z", energy: 12.5 },
];
const irradianceOnly = {
    forecast_id: 13,
    fetched_at: "2026-11-10T08:00:00Z",
    energy: 9.5,
};
const satelliteAt = "2026-11-12T01:00:00Z";
const today = "2026-11-12";
const PLANT_TIMES = new Map([
    ["2026-11-10T14:00:00Z", "10 Nov 2026, 17:00"],
    ["2026-11-10T03:00:00Z", "10 Nov 2026, 06:00"],
]);
const plantTime = (value: string) => PLANT_TIMES.get(value) ?? value;
const SHOWN_DAYS = new Map([
    ["2026-11-11", "11 Nov 2026"],
    ["2026-11-12", "12 Nov 2026"],
]);

function answer(url: URL) {
    const date = url.searchParams.get("date");
    const chosen = Number(url.searchParams.get("forecast_id")) || undefined;
    if (url.searchParams.get("data_source") === "satellite")
        return {
            json: {
                date,
                timezone: region.timezone,
                source_kind: "estimated_actuals",
                freshness: "archived",
                selection: null,
                result: {
                    complete: true,
                    energy_mwh: 10,
                    intervals: [],
                    provenance: { collected_at: satelliteAt },
                },
            },
        };
    const latest = date! >= today;
    const offered = url.pathname.includes("/plants/")
        ? fetches
        : [fetches[0]!, irradianceOnly, fetches[1]!];
    const shown = offered.find(
        (fetch) => fetch.forecast_id === (chosen ?? (latest ? 12 : 11)),
    );
    if (!shown)
        return {
            status: 400,
            json: {
                code: "invalid_request",
                message: "Check the highlighted values and try again.",
                fields: {
                    forecast_id: "Choose one of this day's forecast times.",
                },
            },
        };
    return {
        json: {
            date,
            timezone: region.timezone,
            source_kind: "forecast",
            freshness: latest ? "fresh" : "archived",
            selection: {
                mode: chosen ? "manual" : "auto",
                rule: chosen
                    ? null
                    : latest
                      ? "latest"
                      : "closest_to_production",
                forecast_id: shown.forecast_id,
                options: offered.map(({ forecast_id, fetched_at }) => ({
                    forecast_id,
                    fetched_at,
                })),
            },
            result: {
                complete: true,
                energy_mwh: shown.energy,
                coverage_fraction: 1,
                intervals: [],
                quality_flags: [],
                provenance: { collected_at: shown.fetched_at },
            },
        },
    };
}

async function openForecast(page: Page, date: string) {
    await page.clock.setFixedTime(new Date("2026-11-12T12:00:00Z"));
    await mockApi(page);
    const requests: URL[] = [];
    await page.route(
        (url) =>
            url.pathname.startsWith("/api/") &&
            url.pathname.endsWith("/forecast/"),
        (route) => {
            const url = new URL(route.request().url());
            requests.push(url);
            return route.fulfill(answer(url));
        },
    );
    await page.goto("/app/");
    await page
        .getByRole("button", { name: /East array|Antalya solar park/ })
        .click();
    await page.getByLabel("Forecast date").fill(date);
    const panel = page.locator("section.panel");
    await expect(
        panel.getByText(SHOWN_DAYS.get(date) ?? date, { exact: true }),
    ).toBeVisible();
    return {
        requests,
        last: () => requests.at(-1)!,
        controls: page.locator(".forecast-controls"),
        times: page.getByLabel("Forecast time"),
        panel,
    };
}

test("the data choice offers Forecast and Satellite data only, with Forecast chosen", async ({
    page,
}) => {
    const { controls } = await openForecast(page, "2026-11-11");
    const data = page.getByRole("group", { name: "Data" });
    await expect(data.getByRole("radio")).toHaveCount(2);
    await expect(data.locator("label")).toHaveText([
        "Forecast",
        "Satellite data",
    ]);
    await expect(data.getByRole("radio", { name: "Forecast" })).toBeChecked();
    await expect(controls.locator("select")).toHaveCount(2);
});

async function showsAutomatic(controls: Locator, times: Locator) {
    await expect(times).toHaveValue("");
    await expect(times.locator("option:checked")).toHaveText("Automatic");
    await expect(controls.locator(".tag")).toHaveCount(0);
    await expect(
        controls.getByRole("button", { name: "Back to automatic" }),
    ).toHaveCount(0);
}

test("a past day opens in Automatic with no mark and a chosen time is sent and marked manual", async ({
    page,
}) => {
    const { last, controls, times, panel } = await openForecast(
        page,
        "2026-11-11",
    );
    const total = panel.locator(".stat strong");
    await expect(times.locator("option")).toHaveText([
        "Automatic",
        plantTime(fetches[0]!.fetched_at),
        plantTime(fetches[1]!.fetched_at),
    ]);
    await showsAutomatic(controls, times);
    await expect(total).toHaveText("12.50 MWh");
    await expect(panel.getByText(/^Forecast data: /)).toHaveText(
        `Forecast data: ${plantTime(fetches[1]!.fetched_at)}`,
    );

    await times.selectOption("12");
    await expect.poll(() => last().searchParams.get("forecast_id")).toBe("12");
    await expect(controls.getByText("Manual", { exact: true })).toBeVisible();
    await expect(times).toHaveValue("12");
    await expect(total).toHaveText("11.25 MWh");
    await expect(panel.getByText(/^Forecast data: /)).toHaveText(
        `Forecast data: ${plantTime(fetches[0]!.fetched_at)}`,
    );
    await expect(page.locator("section.panel .tag")).toHaveCount(0);

    await page.getByRole("button", { name: "Back to automatic" }).click();
    await showsAutomatic(controls, times);
    await expect(total).toHaveText("12.50 MWh");
});

test("today opens in Automatic with no mark", async ({ page }) => {
    const { last, controls, times, panel } = await openForecast(page, today);
    await expect(panel.getByText(/^Forecast data: /)).toHaveText(
        `Forecast data: ${plantTime(fetches[0]!.fetched_at)}`,
    );
    await showsAutomatic(controls, times);
    expect(last().searchParams.get("data_source")).toBe("forecast");
    expect(last().searchParams.has("forecast_id")).toBe(false);
});

test("a time only the irradiance view lists is refused by the generation view, which shows Automatic", async ({
    page,
}) => {
    const { requests, controls, times, panel } = await openForecast(
        page,
        "2026-11-11",
    );
    const sent = (plant: boolean) =>
        requests
            .filter((url) => url.pathname.includes("/plants/") === plant)
            .map((url) => url.searchParams.get("forecast_id"));
    const irradiance = page.getByRole("button", {
        name: "Irradiance forecast",
        exact: true,
    });
    await irradiance.click();
    await times.selectOption("13");
    await expect.poll(() => sent(false).at(-1)).toBe("13");

    await page
        .getByRole("button", { name: "Generation forecast", exact: true })
        .click();
    await expect.poll(() => sent(true)).toContain("13");
    await showsAutomatic(controls, times);
    await expect(panel.locator(".stat strong")).toHaveText("12.50 MWh");

    await irradiance.click();
    await expect(times).toHaveValue("13");
    await expect(controls.getByText("Manual", { exact: true })).toBeVisible();
    expect(sent(false).at(-1)).toBe("13");
});

test("the irradiance view sends the chosen time and leaving the date returns to Automatic", async ({
    page,
}) => {
    const { requests, last, controls, times } = await openForecast(
        page,
        "2026-11-11",
    );
    await times.selectOption("12");
    await expect.poll(() => last().searchParams.get("forecast_id")).toBe("12");
    await page
        .getByRole("button", { name: "Irradiance forecast", exact: true })
        .click();
    await expect
        .poll(() =>
            requests
                .filter((url) => !url.pathname.includes("/plants/"))
                .map((url) => url.searchParams.get("forecast_id")),
        )
        .toEqual(["12"]);
    await expect(controls.getByText("Manual", { exact: true })).toBeVisible();
    await expect(times).toHaveValue("12");

    await page.getByLabel("Forecast date").fill("2026-11-10");
    await expect.poll(() => last().searchParams.get("date")).toBe("2026-11-10");
    expect(last().searchParams.has("forecast_id")).toBe(false);
    await showsAutomatic(controls, times);

    const returned = requests.length;
    await page.getByLabel("Forecast date").fill("2026-11-11");
    await expect(page.locator("section.panel")).toContainText("11 Nov 2026");
    await showsAutomatic(controls, times);
    expect(
        requests
            .slice(returned)
            .map((url) => url.searchParams.get("forecast_id")),
    ).not.toContain("12");
});

test("Satellite data hides the forecast time and the way back keeps the chosen time", async ({
    page,
}) => {
    const { last, controls, times, panel } = await openForecast(
        page,
        "2026-11-11",
    );
    await times.selectOption("12");
    await expect.poll(() => last().searchParams.get("forecast_id")).toBe("12");

    await page.getByRole("radio", { name: "Satellite data" }).check();
    await expect
        .poll(() => last().searchParams.get("data_source"))
        .toBe("satellite");
    expect(last().searchParams.has("forecast_id")).toBe(false);
    await expect(
        panel.getByRole("heading", { name: "Satellite data" }),
    ).toBeVisible();
    await expect(times).toHaveCount(0);
    await expect(controls.getByText("Manual", { exact: true })).toHaveCount(0);

    await page.getByRole("radio", { name: "Forecast" }).check();
    await expect(times).toHaveValue("12");
    await expect(controls.getByText("Manual", { exact: true })).toBeVisible();
    await expect(panel.getByText(/^Forecast data: /)).toHaveText(
        `Forecast data: ${plantTime(fetches[0]!.fetched_at)}`,
    );
});

test("neither language names a data source the screen no longer offers", () => {
    const texts = JSON.stringify(messages).toLocaleLowerCase("tr");
    for (const retired of [
        "before sunrise",
        "calculated from satellite",
        "doğumu öncesi",
        "verisinden hesaplanan",
    ])
        expect(texts).not.toContain(retired);
});
