import AxeBuilder from "@axe-core/playwright";
import { expect, test, type Page, type TestInfo } from "@playwright/test";
import { totp } from "./support/totp";

async function checkPage(page: Page, testInfo: TestInfo, name: string) {
    await page.screenshot({
        path: testInfo.outputPath(`${name}.png`),
        fullPage: true,
    });
    expect(
        await page.evaluate(
            () => document.documentElement.scrollWidth <= window.innerWidth,
        ),
    ).toBe(true);
    expect(
        (
            await new AxeBuilder({ page })
                .withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"])
                .analyze()
        ).violations,
    ).toEqual([]);
    expect(
        await page.evaluate(
            () =>
                (window as Window & { cspViolations?: string[] })
                    .cspViolations ?? [],
        ),
    ).toEqual([]);
}

test("an MFA administrator reviews plants and operations on desktop and mobile under CSP", async ({
    page,
}, testInfo) => {
    test.setTimeout(120_000);
    const violations: string[] = [];
    page.on("console", (message) => {
        if (/content security policy/i.test(message.text()))
            violations.push(message.text());
    });
    await page.addInitScript(() => {
        document.addEventListener("securitypolicyviolation", (event) => {
            const target = window as Window & { cspViolations?: string[] };
            (target.cspViolations ??= []).push(event.violatedDirective);
        });
    });
    await page.setViewportSize({ width: 1440, height: 1000 });
    await page.goto("/");
    await page
        .getByLabel("Email", { exact: true })
        .fill(process.env.BROWSER_ADMIN_EMAIL!);
    await page
        .getByLabel("Password", { exact: true })
        .fill(process.env.BROWSER_ADMIN_PASSWORD!);
    await page.locator("form").getByRole("button", { name: "Sign in" }).click();
    await expect(
        page.getByRole("heading", { name: "Two-factor authentication" }),
    ).toBeVisible();
    await page
        .getByLabel("Authenticator or recovery code")
        .fill(totp(process.env.BROWSER_ADMIN_TOTP_SEED!));
    await page.getByRole("button", { name: "Verify code" }).click();
    await expect(page).toHaveURL("/admin/");
    const session = await page.request.get("/api/session/");
    expect((await session.json()).user.can_admin).toBe(true);
    const response = await page.goto("/admin/");
    expect(response?.status()).toBe(200);
    expect(response?.headers()["content-security-policy"]).toContain(
        "script-src",
    );
    await expect(
        page.getByRole("heading", { name: "Overview", exact: true }),
    ).toBeVisible();
    await expect(page.getByText("Review solar plant").first()).toBeVisible();
    const stylesheet = page.locator('link[rel="stylesheet"]').first();
    await expect(stylesheet).toBeAttached();
    expect(
        await stylesheet.evaluate(
            (element) => !!(element as HTMLLinkElement).sheet,
        ),
    ).toBe(true);
    await checkPage(page, testInfo, "admin-overview-desktop");

    await page.getByRole("link", { name: "Plants", exact: true }).click();
    await expect(
        page.getByRole("heading", { name: "Plants", exact: true }),
    ).toBeVisible();
    await checkPage(page, testInfo, "admin-plants-desktop");
    await page
        .getByLabel("Plant, location or company")
        .fill("No matching plant");
    await page.getByRole("button", { name: "Search", exact: true }).click();
    await expect(
        page.getByText("No plants match these filters."),
    ).toBeVisible();
    await page.getByRole("link", { name: "Clear", exact: true }).click();
    await page.getByLabel("Plant, location or company").fill("Review solar");
    await page
        .getByLabel("Review status", { exact: true })
        .selectOption("pending");
    await page.getByRole("button", { name: "Search", exact: true }).click();
    await expect(page.locator("tbody tr")).toHaveCount(1);
    await page
        .getByRole("link", { name: "Review solar plant", exact: true })
        .click();
    await expect(page).toHaveURL(
        `/admin/plants/${process.env.BROWSER_REVIEW_REGION!}/`,
    );
    await expect(
        page.getByRole("heading", { name: /Review solar plant/ }).first(),
    ).toBeVisible();
    await expect(
        page.getByText("Confirm the revised survey coordinates."),
    ).toBeVisible();
    await expect(
        page.getByRole("heading", { name: "EPİAŞ production connection" }),
    ).toBeVisible();
    await expect(page.getByLabel("EPİAŞ plant ID")).toBeVisible();
    await expect(page.getByLabel("EIC:", { exact: true })).toBeVisible();
    await expect(
        page.getByRole("button", {
            name: "Save EPİAŞ connection",
            exact: true,
        }),
    ).toBeDisabled();
    await checkPage(page, testInfo, "admin-plant-desktop");

    await page
        .locator("summary")
        .filter({ hasText: "Replace connection" })
        .click();
    const accountMode = page.getByLabel("Solcast account for this plant");
    const quota = page.getByLabel("New account daily request limit");
    const apiKey = page.getByLabel("Solcast API key");
    await accountMode.selectOption("new");
    await expect(quota).toBeVisible();
    await quota.fill("");
    expect(
        await quota.evaluate(
            (element) => (element as HTMLInputElement).validity.valueMissing,
        ),
    ).toBe(true);
    await quota.fill("10");
    await page
        .getByLabel("New Solcast account identity")
        .fill("browser-review-account");
    await page
        .getByLabel("New account service authorization reference")
        .fill("Browser fixture authorization");
    await apiKey.fill("browser-only-private-key");
    await page.getByLabel("Installation date").fill("2020-01-01");
    await page.getByLabel("I verified a horizontal reference").check();
    const referenceSettings = page
        .locator("summary")
        .filter({ hasText: "Advanced reference settings" });
    await referenceSettings.click();
    const azimuth = page.getByLabel("Azimuth degrees");
    await azimuth.fill("-1");
    await referenceSettings.click();
    await page
        .getByRole("button", { name: "Save private setup", exact: true })
        .click();
    await expect(azimuth).toBeVisible();
    await expect(azimuth).toBeFocused();
    await azimuth.fill("0");
    await accountMode.selectOption("existing");
    await expect(quota).not.toBeVisible();
    expect(
        await quota.evaluate(
            (element) => (element as HTMLInputElement).willValidate,
        ),
    ).toBe(false);
    await expect(apiKey).toBeEnabled();
    await apiKey.clear();
    await page
        .locator("summary")
        .filter({ hasText: "Replace connection" })
        .click();

    await page.setViewportSize({ width: 375, height: 812 });
    await checkPage(page, testInfo, "admin-plant-mobile");
    await page
        .getByRole("button", { name: "Reject location change", exact: true })
        .click();
    await expect(
        page.getByText("Confirm the revised survey coordinates."),
    ).not.toBeVisible();
    await page
        .getByRole("button", { name: "Approve plant", exact: true })
        .click();
    await expect(
        page.getByRole("status").filter({ hasText: "Plant approved." }),
    ).toBeVisible();
    await expect(
        page.getByRole("button", {
            name: "Save EPİAŞ connection",
            exact: true,
        }),
    ).toBeEnabled();
    await checkPage(page, testInfo, "admin-epias-mobile");
    await page.setViewportSize({ width: 1440, height: 1000 });
    await checkPage(page, testInfo, "admin-epias-desktop");
    await page.setViewportSize({ width: 375, height: 812 });

    await page.goto("/admin/");
    await checkPage(page, testInfo, "admin-overview-mobile");
    const navigation = page.locator("summary").filter({ hasText: "Menu" });
    await navigation.focus();
    await page.keyboard.press("Enter");
    await expect(
        page.getByRole("link", { name: "Plants", exact: true }),
    ).toBeVisible();
    await page.getByRole("link", { name: "Plants", exact: true }).click();
    await checkPage(page, testInfo, "admin-plants-mobile");

    await page.setViewportSize({ width: 1440, height: 1000 });
    await page.getByRole("link", { name: "Operations", exact: true }).click();
    await expect(
        page.getByRole("heading", { name: "Operations", exact: true }),
    ).toBeVisible();
    await expect(
        page.getByText("operations-recipient@example.test"),
    ).toBeVisible();
    await expect(
        page.getByRole("button", { name: /retry|resend/i }),
    ).toHaveCount(0);
    await checkPage(page, testInfo, "admin-operations-desktop");
    await page.getByRole("link", { name: "Settings", exact: true }).click();
    await expect(
        page.getByRole("heading", { name: "Settings", exact: true }),
    ).toBeVisible();
    const missingCsrf = await page.request.post("/admin/settings/", {
        form: { max_recipients: "6", expected_max_recipients: "5" },
    });
    expect(missingCsrf.status()).toBe(403);
    await page.locator('[name="max_recipients"]').fill("6");
    await page.locator('main button[type="submit"]').click();
    await expect(page.locator('[name="max_recipients"]')).toHaveValue("6");
    await checkPage(page, testInfo, "admin-settings-desktop");
    await page.setViewportSize({ width: 375, height: 812 });
    await checkPage(page, testInfo, "admin-settings-mobile");

    await page.setViewportSize({ width: 1440, height: 1000 });
    await page.goto("/admin/");
    await page
        .getByRole("combobox", { name: "Language", exact: true })
        .selectOption("tr");
    await page
        .getByRole("button", { name: "Apply language", exact: true })
        .click();
    try {
        await expect(page.locator("html")).toHaveAttribute("lang", "tr");
        await expect(
            page.getByRole("heading", { name: "Genel bakış", exact: true }),
        ).toBeVisible();
        await checkPage(page, testInfo, "admin-turkish-desktop");
        await page.setViewportSize({ width: 375, height: 812 });
        await checkPage(page, testInfo, "admin-turkish-mobile");
    } finally {
        await page.setViewportSize({ width: 1440, height: 1000 });
        await page
            .getByRole("combobox", { name: "Dil", exact: true })
            .selectOption("en");
        await page
            .getByRole("button", { name: "Dili uygula", exact: true })
            .click();
        await expect(page.locator("html")).toHaveAttribute("lang", "en");
    }
    await page.setViewportSize({ width: 375, height: 812 });
    await page.goto("/?workspace=customer");
    await expect(
        page.getByRole("link", { name: "Administration" }),
    ).toBeVisible();
    await page.getByRole("link", { name: "Administration" }).click();
    await expect(page).toHaveURL("/admin/");
    await page.locator("summary").filter({ hasText: "Menu" }).click();
    const signedOutSession = page.waitForResponse(
        (response) =>
            response.request().method() === "GET" &&
            new URL(response.url()).pathname === "/api/session/",
    );
    await page.getByRole("button", { name: "Sign out", exact: true }).click();
    const sessionResponse = await signedOutSession;
    expect(sessionResponse.status()).toBe(200);
    expect((await sessionResponse.json()).user).toBeNull();
    await expect(page.getByLabel("Email", { exact: true })).toBeVisible();
    expect(violations).toEqual([]);
});
