import { test, expect, type Page } from "./support/test";
import {
    accountWith,
    answer,
    mockApi,
    paged,
    plant,
    region,
    sessionIdentity,
} from "./support/workspace";
import type { components } from "../src/lib/schema";

type Schedule = components["schemas"]["CollectionScheduleOutput"];
type Region = components["schemas"]["RegionOutput"];
type Language = "en" | "tr";

const navigation = {
    en: {
        irradiance: "Irradiance forecast",
        settings: "Settings",
        data: "Forecast update times",
    },
    tr: {
        irradiance: "Işınım tahmini",
        settings: "Ayarlar",
        data: "Tahmin güncelleme saatleri",
    },
};

function schedule(overrides: Partial<Schedule>): Schedule {
    return {
        enabled: true,
        times: ["16:00"],
        timezone: region.timezone,
        effective_from: "2026-01-01T00:00:00Z",
        last_success_at: "2026-01-01T13:05:00Z",
        next_collection_at: "2026-01-01T21:00:00Z",
        state: "collecting",
        inactive_reason: null,
        requests_today: 2,
        recent_jobs: [],
        history_requests_reserved_per_day: 1,
        history_local_time: "00:00",
        history_retry_until_local: "09:00",
        history_retry_minutes: 30,
        max_forecast_times: 9,
        ...overrides,
    };
}

type Job = Schedule["recent_jobs"][number];

function job(overrides: Partial<Job>): Job {
    return {
        id: 1,
        endpoint: "forecasts",
        status: "retry",
        scheduled_for: "2026-01-01T13:00:00Z",
        available_at: "2026-01-01T15:00:00Z",
        retry_until: null,
        attempts: 1,
        last_error: "provider_throttled",
        ...overrides,
    };
}

async function openCollection(
    page: Page,
    language: Language,
    data: Schedule,
    status: Region["status"] = "approved",
) {
    const account = await mockApi(page);
    await page.route(
        (url) => url.pathname === "/api/tenants/1/sites/",
        (route) =>
            route.fulfill({
                json: answer(
                    "PaginatedSiteOutputList",
                    paged([{ ...region, status, plant, reminder: null }]),
                ),
            }),
    );
    if (language === "tr") {
        account.current = accountWith({ language_preference: "tr" });
        await page.route("**/api/session/", (route) =>
            route.fulfill({
                json: answer("SessionOutput", {
                    ...sessionIdentity(account.current),
                    language: "tr",
                }),
            }),
        );
    }
    await page.route("**/collection-schedule/", (route) =>
        route.fulfill({ json: answer("CollectionScheduleOutput", data) }),
    );
    await page.goto("/app/");
    const labels = navigation[language];
    await page
        .getByRole("button", { name: /East array|Antalya solar park/ })
        .click();
    await page
        .getByRole("button", { name: labels.irradiance, exact: true })
        .click();
    await page
        .getByRole("button", { name: labels.settings, exact: true })
        .click();
    await page.getByRole("button", { name: labels.data }).click();
    return page.getByRole("region", {
        name: language === "tr" ? "Güncelleme durumu" : "Update status",
    });
}

const collecting = {
    en: {
        state: "Updating automatically",
        next: "Next planned update",
        time: "2 Jan 2026, 00:00",
        absent: [
            "Forecast data requests today (UTC)",
            "Recent update jobs",
            "Update details",
            /is updated daily at/,
            /· 1 attempt/,
        ],
    },
    tr: {
        state: "Otomatik güncelleniyor",
        next: "Planlanan sonraki güncelleme",
        time: "2 Ocak 2026, 00:00",
        absent: [
            "Bugünkü tahmin verisi istekleri (UTC)",
            "Son güncelleme işleri",
            "Güncelleme ayrıntıları",
            /her gün .* saatinde güncellenir/,
            /· 1 deneme/,
        ],
    },
};

for (const language of ["en", "tr"] as const)
    test(`a collecting plant shows its state and next planned update without request counts, job log or daily satellite line (${language})`, async ({
        page,
    }) => {
        const status = await openCollection(
            page,
            language,
            schedule({ recent_jobs: [job({})] }),
        );
        const names = collecting[language];

        await expect(
            status.getByText(names.state, { exact: true }),
        ).toBeVisible();
        await expect(status.getByText(names.next).locator("..")).toContainText(
            names.time,
        );
        await expect(status.getByRole("status")).toHaveCount(0);
        for (const text of names.absent)
            await expect(page.getByText(text)).toHaveCount(0);
    });

const lastUpdate = {
    en: {
        label: "Last update",
        time: "1 Jan 2026, 12:05",
        forecastData: "Forecast data",
    },
    tr: {
        label: "Son güncelleme",
        time: "1 Ocak 2026, 12:05",
        forecastData: "Tahmin verisi",
    },
};

for (const language of ["en", "tr"] as const)
    test(`a satellite update after a forecast update is shown as the last update (${language})`, async ({
        page,
    }) => {
        const job = {
            status: "succeeded",
            retry_until: null,
            attempts: 1,
            last_error: "",
        } as const;
        const status = await openCollection(
            page,
            language,
            schedule({
                last_success_at: "2026-01-01T09:05:00Z",
                recent_jobs: [
                    {
                        ...job,
                        id: 2,
                        endpoint: "estimated_actuals",
                        scheduled_for: "2026-01-01T09:00:00Z",
                        available_at: "2026-01-01T09:00:00Z",
                    },
                    {
                        ...job,
                        id: 1,
                        endpoint: "forecasts",
                        scheduled_for: "2026-01-01T06:00:00Z",
                        available_at: "2026-01-01T06:00:00Z",
                    },
                ],
            }),
        );
        const names = lastUpdate[language];

        await expect(
            status.getByText(names.label, { exact: true }).locator(".."),
        ).toContainText(names.time);
        await expect(
            status.getByText(names.forecastData, { exact: true }),
        ).toHaveCount(0);
    });

const inactive = [
    {
        language: "en",
        state: "paused",
        reason: "schedule_disabled",
        label: "Not updating",
        sentence: "Automatic forecast updates are turned off for this plant.",
        next: "Not planned",
    },
    {
        language: "tr",
        state: "paused",
        reason: "schedule_disabled",
        label: "Güncellenmiyor",
        sentence: "Bu santral için tahminlerin otomatik güncellenmesi kapalı.",
        next: "Planlanmadı",
    },
    {
        language: "tr",
        state: "provider_attention",
        reason: "provider_unavailable",
        label: "Güncellenmiyor",
        sentence: "Tahminler şu anda güncellenmiyor.",
        next: null,
    },
    {
        language: "en",
        state: "paused",
        reason: "not_validated",
        label: "Not updating",
        sentence:
            "No new forecast data is arriving for this plant. Write to Pyracast: info@pyracast.app",
        next: "Not planned",
    },
    {
        language: "tr",
        state: "paused",
        reason: "binding_not_current",
        label: "Güncellenmiyor",
        sentence:
            "Bu santral için yeni tahmin verisi gelmiyor. Pyracast'e yazın: info@pyracast.app",
        next: "Planlanmadı",
    },
] as const;

for (const item of inactive)
    test(`an inactive reason is explained: ${item.reason} (${item.language})`, async ({
        page,
    }) => {
        const quiet =
            item.next === "Not planned" || item.next === "Planlanmadı";
        const status = await openCollection(
            page,
            item.language,
            schedule({
                state: item.state,
                inactive_reason: item.reason,
                next_collection_at: quiet ? null : "2026-01-01T21:00:00Z",
                requests_today: 0,
            }),
        );

        await expect(
            status.getByText(item.label, { exact: true }),
        ).toBeVisible();
        await expect(status.getByRole("status")).toHaveText(item.sentence);
        const next = status.getByText(collecting[item.language].next);
        if (item.next === null) {
            await expect(next).toHaveCount(0);
            await expect(
                status.getByText(collecting[item.language].time),
            ).toHaveCount(0);
            return;
        }
        await expect(next.locator("..")).toContainText(item.next);
    });

const quota = {
    en: {
        time: "Update time 1",
        sentence:
            "No new forecast data is arriving for this plant. Write to Pyracast: info@pyracast.app",
    },
    tr: {
        time: "Güncelleme saati 1",
        sentence:
            "Bu santral için yeni tahmin verisi gelmiyor. Pyracast'e yazın: info@pyracast.app",
    },
};

for (const language of ["en", "tr"] as const)
    test(`a plant whose quota allows no forecast update keeps its saved times and says no new forecast data is arriving (${language})`, async ({
        page,
    }) => {
        const status = await openCollection(
            page,
            language,
            schedule({
                inactive_reason: "quota_too_low",
                times: ["16:00"],
                max_forecast_times: 0,
            }),
        );

        await expect(
            page.getByLabel(quota[language].time, { exact: true }),
        ).toHaveValue("16:00");
        await expect(status.getByRole("status")).toHaveText(
            quota[language].sentence,
        );
    });

const plantStates = [
    {
        status: "pending",
        state: "waiting_for_approval",
        reason: "not_approved",
        label: "Waiting for approval by Pyracast",
        sentence: "Waiting for approval by Pyracast.",
        resubmit: false,
    },
    {
        status: "suspended",
        state: "paused",
        reason: "not_approved",
        label: "Not updating",
        sentence:
            "Forecasts for this plant are stopped. Write to Pyracast: info@pyracast.app",
        resubmit: false,
    },
    {
        status: "rejected",
        state: "paused",
        reason: "not_approved",
        label: "Not updating",
        sentence: "Pyracast did not approve this plant.",
        resubmit: true,
    },
    {
        status: "approved",
        state: "paused",
        reason: "not_validated",
        label: "Not updating",
        sentence:
            "No new forecast data is arriving for this plant. Write to Pyracast: info@pyracast.app",
        resubmit: false,
    },
    {
        status: "approved",
        state: "collecting",
        reason: null,
        label: "Updating automatically",
        sentence: null,
        resubmit: false,
    },
] as const;

for (const item of plantStates)
    test(`one sentence says why collection is off: ${item.status} plant, ${item.state}`, async ({
        page,
    }) => {
        const status = await openCollection(
            page,
            "en",
            schedule({ state: item.state, inactive_reason: item.reason }),
            item.status,
        );

        await expect(
            status.getByText(item.label, { exact: true }),
        ).toBeVisible();
        const notices = page.locator(".notice");
        if (!item.sentence) {
            await expect(notices).toHaveCount(0);
            return;
        }
        await expect(notices).toHaveCount(1);
        await expect(
            notices.getByText(item.sentence, { exact: true }),
        ).toBeVisible();
        await expect(
            notices.getByRole("button", { name: "Send for approval again" }),
        ).toHaveCount(item.resubmit ? 1 : 0);
    });

for (const [cause, jobs] of [
    [
        "a timed pause",
        [job({ status: "retry", last_error: "provider_allowance_exhausted" })],
    ],
    [
        "a rejected key",
        [
            job({
                status: "failed",
                last_error: "provider_authentication_failed",
            }),
        ],
    ],
    [
        "no failed update of this plant",
        [job({ status: "succeeded", last_error: "" })],
    ],
] as const)
    test(`a plant whose provider account refuses requests says only that forecasts are not updated and shows no planned update: ${cause}`, async ({
        page,
    }) => {
        const status = await openCollection(
            page,
            "en",
            schedule({
                state: "provider_attention",
                inactive_reason: "provider_unavailable",
                recent_jobs: [...jobs],
            }),
        );

        await expect(
            status.getByText("Not updating", { exact: true }),
        ).toBeVisible();
        await expect(status.getByRole("status")).toHaveText(
            "Forecasts are not being updated right now.",
        );
        await expect(status.getByText("Next planned update")).toHaveCount(0);
        await expect(status.getByText("2 Jan 2026, 00:00")).toHaveCount(0);
    });
