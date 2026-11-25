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

test("a collecting plant shows its state, next collection and requests of the day", async ({
    page,
}) => {
    const status = await openCollection(page, "en", schedule({}));

    await expect(
        status.getByText("Updating automatically", { exact: true }),
    ).toBeVisible();
    await expect(
        status.getByText("Next planned update").locator(".."),
    ).toContainText("Jan 2, 2026, 12:00 AM");
    await expect(
        status.getByText("Forecast data requests today (UTC)").locator(".."),
    ).toContainText("2");
    await expect(status.getByRole("status")).toHaveCount(0);
});

const lastUpdate = {
    en: {
        label: "Last update",
        time: "Jan 1, 2026, 12:05 PM",
        forecastData: "Forecast data",
    },
    tr: {
        label: "Son güncelleme",
        time: "1 Oca 2026 12:05",
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
        label: "Gecikiyor",
        sentence: "Tahmin güncellemesi gecikti. Pyracast yeniden deneyecek.",
        next: "2 Oca 2026",
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
    {
        language: "en",
        state: "collecting",
        reason: "quota_too_low",
        label: "Updating automatically",
        sentence:
            "This plant has no forecast update times. Write to Pyracast: info@pyracast.app",
        next: "Jan 2, 2026, 12:00 AM",
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
        await expect(
            status
                .getByText(
                    item.language === "tr"
                        ? "Planlanan sonraki güncelleme"
                        : "Next planned update",
                )
                .locator(".."),
        ).toContainText(item.next);
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

for (const [cause, last, label, sentence] of [
    [
        "a timed pause",
        job({ status: "retry", last_error: "provider_allowance_exhausted" }),
        "Delayed",
        "Forecast update delayed. Pyracast will try again.",
    ],
    [
        "a rejected connection",
        job({ status: "failed", last_error: "provider_authentication_failed" }),
        "Not updating",
        "No new forecast data is arriving for this plant. Write to Pyracast: info@pyracast.app",
    ],
] as const)
    test(`a plant whose updates wait after ${cause} says what happens next`, async ({
        page,
    }) => {
        const status = await openCollection(
            page,
            "en",
            schedule({
                state: "provider_attention",
                inactive_reason: "provider_unavailable",
                recent_jobs: [last],
            }),
        );

        await expect(status.getByText(label, { exact: true })).toBeVisible();
        await expect(status.getByRole("status")).toHaveText(sentence);
    });

for (const [status, error, shown] of [
    ["failed", "publication_failure", "Forecast update failed."],
    ["stale", "history_budget_reserved", null],
    [
        "retry",
        "publication_failure",
        "Forecast update delayed. Pyracast will try again.",
    ],
] as const)
    test(`an update log row promises another attempt only while one is planned: ${status} after ${error}`, async ({
        page,
    }) => {
        await openCollection(
            page,
            "en",
            schedule({
                recent_jobs: [
                    job({
                        endpoint:
                            status === "stale"
                                ? "estimated_actuals"
                                : "forecasts",
                        status,
                        last_error: error,
                    }),
                ],
            }),
        );
        await page.getByText("Recent update jobs", { exact: true }).click();
        const row = page
            .locator("details.panel")
            .filter({ hasText: "Recent update jobs" })
            .locator("li");
        await expect(row).toHaveCount(1);
        if (shown) await expect(row).toContainText(` · ${shown}`);
        else await expect(row).toHaveText(/ · 1 attempt$/);
        if (status !== "retry")
            await expect(row).not.toContainText("Pyracast will try again");
    });
