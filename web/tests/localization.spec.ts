import AxeBuilder from "@axe-core/playwright";
import {
    parse,
    TYPE,
    type MessageFormatElement,
} from "@formatjs/icu-messageformat-parser";
import { test, expect, type Page } from "./support/test";
import { mockApi, user, region, plant, paged } from "./support/workspace";
import { messages } from "../src/i18n/messages";
import { parseProduction } from "../src/lib/production";
import { errorBody } from "./support/errors";

type Language = "en" | "tr";

async function mockLanguage(page: Page, initial: Language, anonymous = false) {
    await mockApi(page, { anonymous });
    let language = initial;
    const session = () => ({
        user: anonymous ? null : { ...user, language_preference: language },
        csrf_token: anonymous ? "anonymous-token" : "signed-token",
        language,
    });
    await page.route("**/api/session/", (route) =>
        route.fulfill({ json: session() }),
    );
    await page.route("**/api/session/language/", async (route) => {
        expect(route.request().method()).toBe("POST");
        expect(route.request().headers()["x-csrftoken"]).toBe(
            session().csrf_token,
        );
        language = route.request().postDataJSON().language as Language;
        await route.fulfill({
            json: session(),
            headers: {
                "Set-Cookie": `pyracast_language=${language}; Path=/; HttpOnly; SameSite=Lax; Max-Age=31536000`,
            },
        });
    });
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
        page.getByRole("heading", { name: "Create your company account" }),
    ).toHaveCount(0);
    await expect(page.getByLabel("Email", { exact: true })).toHaveCount(0);
    release();
    await expect(
        page.getByRole("heading", { name: "Şirket hesabınızı oluşturun" }),
    ).toBeVisible();
    await expect(page).toHaveURL("/app/?signup=1");
    await page.getByLabel("Dil", { exact: true }).selectOption("en");
    await expect(
        page.getByRole("heading", { name: "Create your company account" }),
    ).toBeVisible();
    await page.reload();
    await expect(
        page.getByRole("heading", { name: "Create your company account" }),
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
        await mockLanguage(page, "en", true);
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
                              user: null,
                              csrf_token: "anonymous-token",
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
            page.getByRole("heading", { name: "Create your company account" }),
        ).toBeVisible();
        const finished = page.waitForResponse("**/api/session/language/");
        release();
        await finished;
        await expect(
            page.getByRole("heading", { name: "Create your company account" }),
        ).toBeVisible();
        await expect(page.getByRole("main").getByRole("alert")).toHaveCount(0);
        await expect(page).toHaveURL("/app/?signup=1");
    });
}

test("failed public language selection offers a retry in the requested language before exposing authentication", async ({
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
        "Dil kaydedilemedi. Tekrar deneyin.",
    );
    await expect(page.locator("html")).toHaveAttribute("lang", "tr");
    await expect(page.getByLabel("Email", { exact: true })).toHaveCount(0);
    await page.getByRole("button", { name: "Tekrar dene" }).click();
    await expect(
        page.getByRole("heading", { name: "Şirket hesabınızı oluşturun" }),
    ).toBeVisible();
    expect(attempts).toBe(2);
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
        "Language could not be saved. Please try again.",
    );
    await expect(page.getByLabel("Language", { exact: true })).toHaveValue(
        "en",
    );
    await expect(page.getByLabel("Language", { exact: true })).toBeEnabled();
    await expect(page.locator("html")).toHaveAttribute("lang", "en");
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
    expect((await new AxeBuilder({ page }).analyze()).violations).toEqual([]);
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
        "starts_at,ends_at,energy,quality,source\n2026-01-01T09:00:00+03:00,2026-01-01T10:00:00+03:00,1234.5,valid,meter";
    expect(parseProduction(csv, "tr")).toEqual(parseProduction(csv, "en"));
    expect(() => parseProduction(csv.replace("1234.5", "-1"), "tr")).toThrow(
        "Satır 2: enerji sıfır veya pozitif bir sayı olmalı.",
    );
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
                        current_configuration: {
                            ...plant.current_configuration,
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
    await expect(page.getByText("1.235 MW DC", { exact: false })).toBeVisible();
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
                    ? "Choose a completed local day for historical estimates."
                    : "Geçmiş tahminleri için tamamlanmış bir yerel gün seçin.",
            ),
        });
    });
    await page.goto("/app/");
    await page.getByRole("button", { name: /East array/ }).click();
    await expect(page.getByRole("main").getByRole("alert")).toHaveText(
        "Geçmiş tahminleri için tamamlanmış bir yerel gün seçin.",
    );
    await page.getByLabel("Dil", { exact: true }).selectOption("en");
    await expect(page.getByRole("main").getByRole("alert")).toHaveText(
        "Choose a completed local day for historical estimates.",
    );
});

test.describe("the session end notice", () => {
    test.use({ timezoneId: "Europe/Istanbul" });
    for (const [language, heading, notice] of [
        [
            "en",
            "Plants",
            "Your session ends at 10:00 PM. Save your work; you will need to sign in again.",
        ],
        [
            "tr",
            "Santraller",
            "Oturumunuz 22:00 saatinde sona erecek. Çalışmanızı kaydedin; yeniden oturum açmanız gerekecek.",
        ],
    ] as const) {
        test(`appears five minutes before the session ends in ${language}`, async ({
            page,
        }) => {
            await page.clock.install({
                time: new Date("2026-11-18T18:54:00Z"),
            });
            await mockLanguage(page, language);
            await page.route("**/api/session/", (route) =>
                route.fulfill({
                    json: {
                        user: { ...user, language_preference: language },
                        csrf_token: "signed-token",
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
