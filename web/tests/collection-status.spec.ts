import { test, expect, type Page } from "./support/test";
import { mockApi, region, user } from "./support/workspace";
import type { components } from "../src/lib/schema";

type Schedule = components["schemas"]["CollectionScheduleOutput"];
type Language = "en" | "tr";

const navigation = {
    en: {
        irradiance: "Irradiance",
        settings: "Settings",
        data: "Data collection",
    },
    tr: { irradiance: "Işınım", settings: "Ayarlar", data: "Veri toplama" },
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

async function openCollection(page: Page, language: Language, data: Schedule) {
    await mockApi(page);
    if (language === "tr")
        await page.route("**/api/session/", (route) =>
            route.fulfill({
                json: {
                    user: { ...user, language_preference: "tr" },
                    csrf_token: "signed-token",
                    language: "tr",
                },
            }),
        );
    await page.route("**/collection-schedule/", (route) =>
        route.fulfill({ json: data }),
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
        name: language === "tr" ? "Toplama durumu" : "Collection status",
    });
}

test("a collecting plant shows its state, next collection and requests of the day", async ({
    page,
}) => {
    const status = await openCollection(page, "en", schedule({}));

    await expect(status.getByText("Collecting", { exact: true })).toBeVisible();
    await expect(
        status.getByText("Next collection").locator(".."),
    ).toContainText("Jan 2, 2026, 12:00 AM");
    await expect(
        status.getByText("Provider requests today (UTC)").locator(".."),
    ).toContainText("2");
    await expect(status.getByRole("status")).toHaveCount(0);
});

const inactive = [
    {
        language: "en",
        state: "paused",
        reason: "schedule_disabled",
        label: "Paused",
        sentence: "Data collection is turned off for this plant.",
        next: "Not scheduled",
    },
    {
        language: "tr",
        state: "paused",
        reason: "schedule_disabled",
        label: "Duraklatıldı",
        sentence: "Bu santral için veri toplama kapalı.",
        next: "Planlanmadı",
    },
    {
        language: "en",
        state: "waiting_for_approval",
        reason: "not_approved",
        label: "Waiting for approval",
        sentence: "Collection starts once the plant is approved.",
        next: "Not scheduled",
    },
    {
        language: "tr",
        state: "provider_attention",
        reason: "provider_unavailable",
        label: "Sağlayıcı bekleniyor",
        sentence:
            "Sağlayıcı şu anda istek kabul etmiyor; veri toplama kendiliğinden sürecek.",
        next: "2 Oca 2026",
    },
    {
        language: "en",
        state: "paused",
        reason: "not_validated",
        label: "Paused",
        sentence:
            "Collection starts once the provider connection is validated.",
        next: "Not scheduled",
    },
    {
        language: "tr",
        state: "paused",
        reason: "binding_not_current",
        label: "Duraklatıldı",
        sentence:
            "Sağlayıcı bağlantısı yeniden doğrulandığında veri toplama sürer.",
        next: "Planlanmadı",
    },
    {
        language: "en",
        state: "collecting",
        reason: "quota_too_low",
        label: "Collecting",
        sentence:
            "The provider allowance leaves no request for forecasts; only the daily history is collected.",
        next: "Jan 2, 2026, 12:00 AM",
    },
] as const;

for (const item of inactive)
    test(`an inactive reason is explained: ${item.reason} (${item.language})`, async ({
        page,
    }) => {
        const quiet =
            item.next === "Not scheduled" || item.next === "Planlanmadı";
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
                        ? "Sonraki toplama"
                        : "Next collection",
                )
                .locator(".."),
        ).toContainText(item.next);
    });
