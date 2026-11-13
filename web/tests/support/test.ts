import { test as base } from "@playwright/test";
import { CoverageReport } from "monocart-coverage-reports";
import { coverageOptions } from "../../testing/coverage-options";

export { expect, type Page } from "@playwright/test";

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
                const coverage = await page.coverage.stopJSCoverage();
                if (coverage.length) {
                    await new CoverageReport(coverageOptions).add(coverage);
                }
            }
        },
        { auto: true },
    ],
});
