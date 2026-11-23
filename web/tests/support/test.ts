import { test as base } from "@playwright/test";
import { addBrowserCoverage } from "../../testing/coverage-maps";

export { expect, type Page } from "@playwright/test";

// Coverage is read once, when the test ends. Whether code that ran in a document the scenario
// reloaded or left is still counted depends on Chromium, so a scenario that must be counted ends in
// the document whose behaviour it shows.
export const test = base.extend<{ coverage: void }>({
    coverage: [
        async ({ page }, runTest) => {
            if (process.env.PLAYWRIGHT_COVERAGE !== "1") {
                await runTest();
                return;
            }
            await page.coverage.startJSCoverage({ resetOnNavigation: false });
            try {
                await runTest();
            } finally {
                await addBrowserCoverage(await page.coverage.stopJSCoverage());
            }
        },
        { auto: true },
    ],
});
