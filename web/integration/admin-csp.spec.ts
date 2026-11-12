import { expect, test } from "@playwright/test";
import { totp } from "./support/totp";

test("the authenticated Django admin loads its assets under CSP", async ({
    page,
}) => {
    test.setTimeout(60_000);
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
    await expect(page.getByRole("button", { name: "Sign out" })).toBeVisible();
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
    const adminOrigin = new URL(process.env.BROWSER_ADMIN_ORIGIN!);
    adminOrigin.hostname = new URL(page.url()).hostname;
    const response = await page.goto(new URL("/admin/", adminOrigin).href);
    expect(response?.status()).toBe(200);
    expect(response?.headers()["content-security-policy"]).toContain(
        "script-src",
    );
    await expect(
        page.getByRole("link", { name: "Pyracast operations" }),
    ).toBeVisible();
    await expect(
        page.getByRole("heading", { name: "Site administration" }),
    ).toBeVisible();
    const stylesheet = page.locator('link[rel="stylesheet"]').first();
    await expect(stylesheet).toBeAttached();
    expect(
        await stylesheet.evaluate(
            (element) => !!(element as HTMLLinkElement).sheet,
        ),
    ).toBe(true);
    const beforeTheme = await page.locator("html").getAttribute("data-theme");
    await page.locator("button.theme-toggle").first().click();
    await expect
        .poll(() => page.locator("html").getAttribute("data-theme"))
        .not.toBe(beforeTheme);
    expect(
        await page.evaluate(
            () =>
                (window as Window & { cspViolations?: string[] })
                    .cspViolations ?? [],
        ),
    ).toEqual([]);
    expect(violations).toEqual([]);
});
