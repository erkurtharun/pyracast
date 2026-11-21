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
