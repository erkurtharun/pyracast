import {
    parse,
    TYPE,
    type MessageFormatElement,
} from "@formatjs/icu-messageformat-parser";
import { createTranslator } from "next-intl";
import { test, expect, type Page } from "./support/test";
import { accessibilityViolations } from "./support/accessibility";
import {
    accountWith,
    mockApi,
    paged,
    plant,
    region,
    sessionIdentity,
} from "./support/workspace";
import { messages } from "../src/i18n/messages";
import { parseProduction } from "../src/lib/production";
import { csvTexts } from "./support/production-csv";
import { errorBody } from "./support/errors";

type Language = "en" | "tr";

async function mockLanguage(page: Page, initial: Language, anonymous = false) {
    const account = await mockApi(page, {
        account: {
            current: anonymous
                ? null
                : accountWith({ language_preference: initial }),
        },
    });
    let language = initial;
    const session = () => ({ ...sessionIdentity(account.current), language });
    await page.route("**/api/session/", (route) =>
        route.fulfill({ json: session() }),
    );
    await page.route("**/api/session/language/", async (route) => {
        expect(route.request().method()).toBe("POST");
        expect(route.request().headers()["x-csrftoken"]).toBe(
            session().csrf_token,
        );
        language = route.request().postDataJSON().language as Language;
        if (account.current)
            account.current = accountWith({ language_preference: language });
        await route.fulfill({
            json: session(),
            headers: {
                "Set-Cookie": `pyracast_language=${language}; Path=/; HttpOnly; SameSite=Lax; Max-Age=31536000`,
            },
        });
    });
    return account;
}

test("a Turkish public signup applies guest language before showing the form and keeps a later choice", async ({
    page,
}) => {
    await page.context().addCookies([
        {
            name: "pyracast_language",
            value: "en",
            url: "http://127.0.0.1:3100",
        },
    ]);
    await mockLanguage(page, "en", true);
    let release!: () => void;
    const pending = new Promise<void>((resolve) => {
        release = resolve;
    });
    await page.route("**/api/session/language/", async (route) => {
        await pending;
        await route.fallback();
    });
    const requested = page.waitForRequest("**/api/session/language/");
    await page.goto("/tr/");
    await page
        .getByRole("link", { name: "Çalışma alanı oluştur", exact: true })
        .first()
        .click();
    await requested;
    await expect(
        page.getByRole("heading", { name: "Create workspace" }),
    ).toHaveCount(0);
    await expect(page.getByLabel("Email", { exact: true })).toHaveCount(0);
    release();
    await expect(
        page.getByRole("heading", { name: "Çalışma alanı oluştur" }),
    ).toBeVisible();
    await expect(page).toHaveURL("/app/?signup=1");
    await page.getByLabel("Dil", { exact: true }).selectOption("en");
    await expect(
        page.getByRole("heading", { name: "Create workspace" }),
    ).toBeVisible();
    await page.reload();
    await expect(
        page.getByRole("heading", { name: "Create workspace" }),
    ).toBeVisible();
});

test("public language never replaces a signed in account preference", async ({
    page,
}) => {
    await mockLanguage(page, "en");
    let writes = 0;
    await page.route("**/api/session/language/", async (route) => {
        writes += 1;
        await route.fallback();
    });
    await page.goto("/app/?language=tr");
    await expect(
        page.getByRole("heading", { name: "Plants", exact: true }),
    ).toBeVisible();
    await expect(page.locator("html")).toHaveAttribute("lang", "en");
    await expect(page).toHaveURL("/app/w/1/");
    expect(writes).toBe(0);
});

test("matching guest language does not write another preference", async ({
    page,
}) => {
    await mockLanguage(page, "tr", true);
    let writes = 0;
    await page.route("**/api/session/language/", async (route) => {
        writes += 1;
        await route.fallback();
    });
    await page.goto("/app/?language=tr");
    await expect(
        page.getByRole("heading", { name: "Tekrar hoş geldiniz" }),
    ).toBeVisible();
    await expect(page).toHaveURL("/app/");
    expect(writes).toBe(0);
});

for (const outcome of ["success", "failure"] as const) {
    test(`an obsolete public language ${outcome} cannot replace a newer selection`, async ({
        page,
    }) => {
        const account = await mockLanguage(page, "en", true);
        let release!: () => void;
        const pending = new Promise<void>((resolve) => {
            release = resolve;
        });
        await page.route("**/api/session/language/", async (route) => {
            await pending;
            await route.fulfill(
                outcome === "success"
                    ? {
                          json: {
                              ...sessionIdentity(account.current),
                              language: "tr",
                          },
                      }
                    : { status: 503, body: "unavailable" },
            );
        });
        const requested = page.waitForRequest("**/api/session/language/");
        await page.goto("/app/?language=tr&signup=1");
        await requested;
        await page.evaluate(() =>
            window.history.replaceState(null, "", "/app/?language=en&signup=1"),
        );
        await expect(
            page.getByRole("heading", { name: "Create workspace" }),
        ).toBeVisible();
        const finished = page.waitForResponse("**/api/session/language/");
        release();
        await finished;
        await expect(
            page.getByRole("heading", { name: "Create workspace" }),
        ).toBeVisible();
        await expect(page.getByRole("main").getByRole("alert")).toHaveCount(0);
        await expect(page).toHaveURL("/app/?signup=1");
    });
}

test("failed public language selection offers a retry in the session language before exposing authentication", async ({
    page,
}) => {
    await mockLanguage(page, "en", true);
    let attempts = 0;
    await page.route("**/api/session/language/", async (route) => {
        if (++attempts === 1)
            await route.fulfill({ status: 503, body: "unavailable" });
        else await route.fallback();
    });
    await page.goto("/app/?language=tr&signup=1");
    await expect(page.getByRole("main").getByRole("alert")).toHaveText(
        "The language could not be saved. Try again.",
    );
    await expect(page.locator("html")).toHaveAttribute("lang", "en");
    await expect(page.getByLabel("Email", { exact: true })).toHaveCount(0);
    await page.getByRole("button", { name: "Try again" }).click();
    await expect(
        page.getByRole("heading", { name: "Çalışma alanı oluştur" }),
    ).toBeVisible();
    expect(attempts).toBe(2);
});

test("focus reads wait while the language hand-off is pending or failed", async ({
    page,
}) => {
    await mockLanguage(page, "en", true);
    let reads = 0;
    await page.route("**/api/session/", async (route) => {
        reads += 1;
        await route.fallback();
    });
    let attempts = 0;
    let release!: () => void;
    const held = new Promise<void>((resolve) => (release = resolve));
    await page.route("**/api/session/language/", async (route) => {
        if (++attempts > 1) return route.fallback();
        await held;
        await route.fulfill({ status: 503, body: "unavailable" });
    });
    const requested = page.waitForRequest("**/api/session/language/");
    await page.goto("/app/?language=tr&signup=1");
    await requested;
    const initial = reads;
    const focus = () =>
        page.evaluate(() => window.dispatchEvent(new Event("focus")));
    await focus();
    release();
    await expect(page.getByRole("main").getByRole("alert")).toHaveText(
        "The language could not be saved. Try again.",
    );
    await focus();
    await page.getByRole("button", { name: "Try again" }).click();
    await expect(
        page.getByRole("heading", { name: "Çalışma alanı oluştur" }),
    ).toBeVisible();
    expect(attempts).toBe(2);
    expect(reads).toBe(initial);
});

test("a resolved account language overrides the browser and stale cookie without flashing another workspace", async ({
    page,
}) => {
    await page.context().addCookies([
        {
            name: "pyracast_language",
            value: "en",
            url: "http://127.0.0.1:3100",
        },
    ]);
    await mockLanguage(page, "tr");
    await page.addInitScript(() => {
        const headings: string[] = [];
        Object.assign(window, { seenHeadings: headings });
        new MutationObserver(() => {
            for (const heading of document.querySelectorAll("h1"))
                headings.push(heading.textContent ?? "");
        }).observe(document, { childList: true, subtree: true });
    });
    await page.goto("/app/");
    await expect(
        page.getByRole("heading", { name: "Santraller" }),
    ).toBeVisible();
    await expect(page.locator("html")).toHaveAttribute("lang", "tr");
    await expect(page.getByLabel("Dil", { exact: true })).toHaveValue("tr");
    const headings = await page.evaluate(
        () => (window as unknown as { seenHeadings: string[] }).seenHeadings,
    );
    expect(headings).not.toContain("Plants");
    await page.getByRole("button", { name: "Hesap", exact: true }).click();
    await expect(
        page.getByRole("heading", { name: "İki adımlı doğrulama" }),
    ).toBeVisible();
});

test("guest selection persists on reload and preserves the form while switching", async ({
    page,
}) => {
    await mockLanguage(page, "en", true);
    await page.goto("/app/");
    await page.getByLabel("Email", { exact: true }).fill("owner@example.test");
    await page.getByLabel("Language", { exact: true }).selectOption("tr");
    await expect(
        page.getByRole("heading", { name: "Tekrar hoş geldiniz" }),
    ).toBeVisible();
    await expect(page.getByLabel("E-posta", { exact: true })).toHaveValue(
        "owner@example.test",
    );
    await expect(
        page.getByRole("link", { name: "Ana sayfa", exact: true }),
    ).toHaveAttribute("href", "/tr/");
    await expect(
        page.getByRole("link", { name: "Gizlilik", exact: true }),
    ).toHaveAttribute("href", "/tr/privacy/");
    await expect(
        page.getByRole("link", { name: "Kullanım koşulları", exact: true }),
    ).toHaveAttribute("href", "/tr/terms/");
    await page.reload();
    await expect(
        page.getByRole("heading", { name: "Tekrar hoş geldiniz" }),
    ).toBeVisible();
    const cookie = (await page.context().cookies()).find(
        (item) => item.name === "pyracast_language",
    );
    expect(cookie).toMatchObject({
        value: "tr",
        httpOnly: true,
        sameSite: "Lax",
    });
    await page.getByLabel("Dil", { exact: true }).selectOption("en");
    await expect(
        page.getByRole("heading", { name: "Welcome back" }),
    ).toBeVisible();
    await expect(
        page.getByRole("link", { name: "Homepage", exact: true }),
    ).toHaveAttribute("href", "/");
    await expect(
        page.getByRole("link", { name: "Privacy", exact: true }),
    ).toHaveAttribute("href", "/privacy/");
});

test("a failed save keeps the current language and offers an accessible error", async ({
    page,
}) => {
    await mockLanguage(page, "en", true);
    await page.route("**/api/session/language/", (route) =>
        route.fulfill({ status: 503, body: "unavailable" }),
    );
    await page.goto("/app/");
    await page.getByLabel("Language", { exact: true }).selectOption("tr");
    await expect(page.getByRole("main").getByRole("alert")).toHaveText(
        "The language could not be saved. Try again.",
    );
    await expect(page.getByLabel("Language", { exact: true })).toHaveValue(
        "en",
    );
    await expect(page.getByLabel("Language", { exact: true })).toBeEnabled();
    await expect(page.locator("html")).toHaveAttribute("lang", "en");
});

test("a language save whose answer was lost says its outcome is not known", async ({
    page,
}) => {
    await mockLanguage(page, "en", true);
    await page.route("**/api/session/language/", (route) =>
        route.fulfill({ status: 502, body: "lost" }),
    );
    await page.goto("/app/");
    await page.getByLabel("Language", { exact: true }).selectOption("tr");
    await expect(page.getByRole("main").getByRole("alert")).toHaveText(
        "It is not known whether it was saved. Check before you send it again.",
    );
    await expect(page.getByLabel("Language", { exact: true })).toHaveValue(
        "en",
    );
});

test("Turkish mobile navigation remains accessible without horizontal overflow", async ({
    page,
}, testInfo) => {
    await page.setViewportSize({ width: 360, height: 800 });
    await mockLanguage(page, "tr");
    await page.goto("/app/");
    await expect(
        page.getByRole("heading", { name: "Santraller" }),
    ).toBeVisible();
    await expect(page.getByLabel("Dil", { exact: true })).toBeVisible();
    const selector = await page
        .getByLabel("Dil", { exact: true })
        .boundingBox();
    const signOut = await page
        .getByRole("button", { name: "Çıkış yap", exact: true })
        .boundingBox();
    expect(selector).not.toBeNull();
    expect(signOut).not.toBeNull();
    expect(selector!.x + selector!.width).toBeLessThanOrEqual(signOut!.x);
    expect(
        await page.evaluate(
            () => document.documentElement.scrollWidth <= window.innerWidth,
        ),
    ).toBe(true);
    expect(await accessibilityViolations(page)).toEqual([]);
    await page.screenshot({
        path: testInfo.outputPath("turkish-mobile.png"),
        fullPage: true,
    });
    await page.getByRole("button", { name: /East array/ }).click();
    await expect(
        page.getByRole("button", { name: "Tahmin", exact: true }),
    ).toBeVisible();
    await page.screenshot({
        path: testInfo.outputPath("turkish-plant-mobile.png"),
        fullPage: true,
    });
    expect(
        await page.evaluate(
            () => document.documentElement.scrollWidth <= window.innerWidth,
        ),
    ).toBe(true);
});

function flatten(input: object, prefix = ""): Record<string, string> {
    return Object.fromEntries(
        Object.entries(input).flatMap(([key, value]) => {
            const name = prefix ? `${prefix}.${key}` : key;
            return typeof value === "string"
                ? [[name, value]]
                : Object.entries(flatten(value, name));
        }),
    );
}

function argumentsIn(elements: MessageFormatElement[]): string[] {
    return [
        ...new Set(
            elements.flatMap((element): string[] => {
                if (
                    element.type === TYPE.literal ||
                    element.type === TYPE.pound
                )
                    return [];
                const argument =
                    element.type === TYPE.tag
                        ? `tag:${element.value}`
                        : element.value;
                if (
                    element.type === TYPE.select ||
                    element.type === TYPE.plural
                )
                    return [
                        argument,
                        ...Object.values(element.options).flatMap((option) =>
                            argumentsIn(option.value),
                        ),
                    ];
                return element.type === TYPE.tag
                    ? [argument, ...argumentsIn(element.children)]
                    : [argument];
            }),
        ),
    ].sort();
}

test("all translated messages are valid ICU and preserve keys and interpolation contracts", () => {
    const english = flatten(messages.en);
    const turkish = flatten(messages.tr);
    expect(Object.keys(turkish).sort()).toEqual(Object.keys(english).sort());
    for (const [key, value] of Object.entries(english)) {
        const translated = turkish[key];
        expect(typeof translated, key).toBe("string");
        expect(translated?.trim(), key).not.toBe("");
        expect(argumentsIn(parse(translated ?? "")), key).toEqual(
            argumentsIn(parse(value)),
        );
    }
});

test("CSV validation is localized while ISO timestamps, units and values remain unchanged", () => {
    const csv =
        "starts_at,ends_at,energy_kwh,quality,source\n2026-01-01T09:00:00+03:00,2026-01-01T10:00:00+03:00,1234.5,valid,meter";
    expect(parseProduction(csv, ...csvTexts("tr"))).toEqual(
        parseProduction(csv, ...csvTexts("en")),
    );
    expect(() =>
        parseProduction(csv.replace("1234.5", "-1"), ...csvTexts("tr")),
    ).toThrow(
        "Satır 2: energy_kwh sütununa sıfır veya pozitif bir sayı yazın.",
    );
});

test("a count in a sentence is grouped as the language groups numbers", () => {
    const count = { count: 1284 };
    const row = { row: 1284 };
    const sentences: [
        string,
        Record<string, number | string>,
        string,
        string,
    ][] = [
        [
            "Dashboard.shownOf",
            { shown: 1284, total: 2391 },
            "1,284 of 2,391 plants shown",
            "2.391 santralden 1.284 tanesi gösteriliyor",
        ],
        [
            "Dashboard.needAttention",
            { attention: 1284 },
            "1,284 plants need action.",
            "1.284 santralde işlem gerekiyor.",
        ],
        [
            "Security.recoveryCount",
            { unused: 1284, total: 2391 },
            "1,284 of 2,391 recovery codes unused.",
            "2.391 kurtarma kodundan 1.284 tanesi kullanılmamış.",
        ],
        [
            "KeepFigures.kept",
            count,
            "Figures of 1,284 plants are kept on this device.",
            "1.284 santralin değerleri bu cihazda saklanıyor.",
        ],
        [
            "ProductionCsv.interval",
            { ...row, start: "starts_at", end: "ends_at" },
            "Row 1,284: write starts_at and ends_at with their offset, 30 or 60 minutes apart, as in the example.",
            "Satır 1.284: starts_at ve ends_at sütunlarına saatleri örnekteki gibi saat farkıyla, aralarında 30 veya 60 dakika olacak şekilde yazın.",
        ],
        [
            "ProductionCsv.future",
            row,
            "Row 1,284: the end lies in the future. Remove the row or correct its time.",
            "Satır 1.284: bitiş zamanı gelecekte. Satırı kaldırın veya saatini düzeltin.",
        ],
        [
            "ProductionCsv.energy",
            { ...row, column: "energy_kwh" },
            "Row 1,284: write energy_kwh as zero or a positive number.",
            "Satır 1.284: energy_kwh sütununa sıfır veya pozitif bir sayı yazın.",
        ],
        [
            "ProductionCsv.quality",
            { ...row, status: "quality", source: "source" },
            "Row 1,284: write Normal, Outage, Production curtailed or Invalid value in the quality column and up to 200 characters in the source column.",
            "Satır 1.284: quality sütununa Normal, Kesinti, Üretim kısıldı veya Geçersiz değer; source sütununa en fazla 200 karakter yazın.",
        ],
        [
            "ProductionCsv.mixed",
            row,
            "Row 1,284: the half hours of this hour have different statuses. Correct each half hour separately.",
            "Satır 1.284: bu saatin yarım saatlerinin durumu farklı. Her yarım saati ayrı düzeltin.",
        ],
        [
            "ProductionCsv.overlap",
            { first: 1284, second: 2391 },
            "Rows 1,284 and 2,391 cover the same time. Remove one of them or correct its time.",
            "Satır 1.284 ile satır 2.391 aynı saati kapsıyor. Birini kaldırın veya saatini düzeltin.",
        ],
        [
            "ForecastPerformance.daily_comparison",
            count,
            "Days compared (1,284)",
            "Karşılaştırılan günler (1.284)",
        ],
        [
            "ForecastPerformance.unavailable_dates",
            count,
            "Days not compared (1,284)",
            "Karşılaştırılamayan günler (1.284)",
        ],
        [
            "ProductionHistory.missing_intervals",
            count,
            "1,284 intervals without a value",
            "1.284 aralıkta değer yok",
        ],
        [
            "ProductionImport.validated_file",
            { ...count, file: "production.csv" },
            "production.csv · 1,284 rows read",
            "production.csv · 1.284 satır okundu",
        ],
        [
            "ProductionImport.import_complete",
            { created: 1284, existing: 2391 },
            "Imported. New values: 1,284 · Already saved: 2,391",
            "İçe aktarıldı. Yeni değer: 1.284 · Zaten kayıtlı: 2.391",
        ],
        [
            "ProductionImport.confirm_import",
            { count: 1000 },
            "Import 1,000 values",
            "1.000 değeri içe aktar",
        ],
        [
            "PlantOnboarding.inBinMore",
            count,
            "1,284 more plants from this location are in the recycle bin.",
            "Bu konumdan 1.284 santral daha geri dönüşüm kutusunda.",
        ],
        [
            "WorkspaceMembers.reminderLimitWarning",
            count,
            "1,284 plants send their forecast email to all members and are at the recipient limit. A new member pauses that email until recipients are removed.",
            "1.284 santralin tahmin e-postası tüm üyelere gidiyor ve alıcı sınırına ulaştı. Yeni bir üye eklenirse, alıcı sayısı azaltılana kadar bu e-posta duraklatılır.",
        ],
        [
            "ChangeRequests.pendingContext",
            count,
            "Your change requests waiting for approval: 1,284. See them in the Inbox.",
            "Onay bekleyen değişiklik talepleriniz: 1.284. Gelen kutusunda görebilirsiniz.",
        ],
    ];
    const translators = {
        en: createTranslator({ locale: "en", messages: messages.en }),
        tr: createTranslator({ locale: "tr", messages: messages.tr }),
    };
    for (const [key, values, english, turkish] of sentences) {
        expect(translators.en(key as never, values as never), key).toBe(
            english,
        );
        expect(translators.tr(key as never, values as never), key).toBe(
            turkish,
        );
    }
});

test("Turkish plant search and capacity formatting follow the selected language", async ({
    page,
}) => {
    await mockLanguage(page, "tr");
    await page.route("**/api/tenants/1/sites/", (route) =>
        route.fulfill({
            json: paged([
                {
                    ...region,
                    plant: {
                        ...plant,
                        name: "IŞIK GES",
                        parameters: {
                            ...plant.parameters,
                            dc_capacity_mw: 1234.5,
                        },
                    },
                },
            ]),
        }),
    );
    await page.goto("/app/");
    await page.getByRole("searchbox", { name: "Santral ara" }).fill("ışık");
    await expect(page.getByRole("heading", { name: "IŞIK GES" })).toBeVisible();
    await expect(page.getByText("1.234,5 MWp", { exact: true })).toBeVisible();
});

test("coded forecast errors are explained in the selected language", async ({
    page,
}) => {
    await mockLanguage(page, "tr");
    await page.route("**/plants/21/forecast/**", async (route) => {
        const cookie = (await route.request().headerValue("cookie")) ?? "";
        await route.fulfill({
            status: 400,
            json: errorBody(
                "completed_day_required",
                cookie.includes("pyracast_language=en")
                    ? "Choose today or an earlier day."
                    : "Bugünü veya daha önceki bir günü seçin.",
            ),
        });
    });
    await page.goto("/app/");
    await page.getByRole("button", { name: /East array/ }).click();
    const failure = page.getByRole("main").getByRole("alert");
    await expect(failure.locator("p")).toHaveText(
        "Bugünü veya daha önceki bir günü seçin.",
    );
    await expect(failure.getByRole("button")).toHaveText("Tekrar dene");
    await page.getByLabel("Dil", { exact: true }).selectOption("en");
    await expect(failure.locator("p")).toHaveText(
        "Choose today or an earlier day.",
    );
    await expect(failure.getByRole("button")).toHaveText("Try again");
});

test.describe("the session end notice", () => {
    test.use({ timezoneId: "Europe/Istanbul" });
    for (const [language, heading, notice] of [
        [
            "en",
            "Plants",
            "Automatic sign-out at 22:00 (UTC+3). Save your work.",
        ],
        [
            "tr",
            "Santraller",
            "Otomatik çıkış saati: 22:00 (UTC+3). Çalışmanızı kaydedin.",
        ],
    ] as const) {
        test(`appears five minutes before the session ends in ${language}`, async ({
            page,
        }) => {
            await page.clock.install({
                time: new Date("2026-11-18T18:54:00Z"),
            });
            const account = await mockLanguage(page, language);
            await page.route("**/api/session/", (route) =>
                route.fulfill({
                    json: {
                        ...sessionIdentity(account.current),
                        language,
                        expires_at: "2026-11-18T19:00:00Z",
                    },
                }),
            );
            await page.goto("/app/");
            await expect(
                page.getByRole("heading", { name: heading, exact: true }),
            ).toBeVisible();
            await expect(page.getByText(notice)).toHaveCount(0);
            await page.clock.runFor(60_000);
            await expect(page.getByText(notice)).toBeVisible();
        });
    }
});
