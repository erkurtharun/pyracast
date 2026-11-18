import AxeBuilder from "@axe-core/playwright";
import { expect, type Page, type TestInfo } from "@playwright/test";
import { totp } from "./totp";

export async function recordCspViolations(page: Page): Promise<string[]> {
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
    return violations;
}

export async function checkPage(page: Page, testInfo: TestInfo, name: string) {
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

export async function signInToConsole(
    page: Page,
    email: string,
    password: string,
    totpSeed: string,
) {
    await page.goto("/app/");
    await page.getByLabel("Email", { exact: true }).fill(email);
    await page.getByLabel("Password", { exact: true }).fill(password);
    await page
        .locator("form")
        .getByRole("button", { name: "Sign in", exact: true })
        .click();
    await expect(
        page.getByRole("heading", { name: "Two-factor authentication" }),
    ).toBeVisible();
    await page
        .getByLabel("Authenticator or recovery code")
        .fill(totp(totpSeed));
    await page.getByRole("button", { name: "Verify code" }).click();
    await expect(page).toHaveURL("/admin/");
}
