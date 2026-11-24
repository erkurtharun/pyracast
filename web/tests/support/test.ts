import { test as base } from "@playwright/test";
import {
    addBrowserCoverage,
    startBrowserCoverage,
} from "../../testing/coverage-maps";

export { expect, type Page } from "@playwright/test";

export const instant = new Date("2026-11-10T12:00:00Z");

// Coverage is read once, when the test ends. Whether code that ran in a document the scenario
// reloaded or left is still counted depends on Chromium, so a scenario that must be counted ends in
// the document whose behaviour it shows.
export const test = base.extend<{ coverage: void; clock: void }>({
    coverage: [
        async ({ page }, runTest) => {
            if (process.env.PLAYWRIGHT_COVERAGE !== "1") {
                await runTest();
                return;
            }
            const stop = await startBrowserCoverage(
                await page.context().newCDPSession(page),
            );
            try {
                await runTest();
            } finally {
                await addBrowserCoverage(await stop());
            }
        },
        { auto: true },
    ],
    clock: [
        async ({ context }, runTest) => {
            await context.clock.install({ time: instant });
            await runTest();
        },
        { auto: true },
    ],
});
