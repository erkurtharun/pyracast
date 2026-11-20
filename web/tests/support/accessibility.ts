import AxeBuilder from "@axe-core/playwright";
import { test, type Page } from "@playwright/test";

const TAGS = [
    "wcag2a",
    "wcag2aa",
    "wcag21a",
    "wcag21aa",
    "wcag22aa",
    "best-practice",
];

// WCAG 2.2 A and AA plus axe best practices; the full result is attached to the test.
export async function accessibilityViolations(page: Page, include?: string) {
    const builder = new AxeBuilder({ page }).withTags(TAGS);
    const results = await (
        include ? builder.include(include) : builder
    ).analyze();
    await test.info().attach("accessibility", {
        body: JSON.stringify(results, null, 2),
        contentType: "application/json",
    });
    return results.violations;
}
