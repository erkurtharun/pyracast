import AxeBuilder from "@axe-core/playwright";
import { expect, type Page, type TestInfo } from "@playwright/test";

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

// The Django console keeps its own WCAG 2.1 scan: the web app's shared rules (WCAG 2.2 and best
// practice, tests/support/accessibility.ts) were never applied to the console's pages.
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

// Recovery codes answer both second-factor prompts: an authenticator code minted from the clock
// can reach the server in the next 30-second step and be refused; auth-security.spec.ts and the
// backend tests cover authenticator codes.
export async function signInToConsole(
    page: Page,
    email: string,
    password: string,
    recoveryCodes: string,
) {
    const [signInCode, confirmationCode] = recoveryCodes.split(",");
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
    await page.getByLabel("Authenticator or recovery code").fill(signInCode!);
    await page.getByRole("button", { name: "Verify code" }).click();
    await expect(page).toHaveURL("/admin/");
    // Console writes need a confirmation after sign-in.
    await page
        .getByRole("link", { name: "Confirm with your authenticator" })
        .click();
    await page
        .getByLabel("Authenticator or recovery code")
        .fill(confirmationCode!);
    await page.getByRole("button", { name: "Confirm", exact: true }).click();
    await expect(page).toHaveURL("/admin/");
}
