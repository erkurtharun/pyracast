import { defineConfig, devices } from "@playwright/test";
import { coverageRun, serverOrigin, webServer } from "./tests/support/server";

const run = coverageRun();
const coverage = !!run;
export default defineConfig({
    testDir: "./tests",
    fullyParallel: true,
    forbidOnly: !!process.env.CI,
    retries: process.env.CI ? 2 : 0,
    // A run of main accepts a pass on the retry: the pull request run already fails such a case.
    failOnFlakyTests:
        !!process.env.CI && !process.env.PLAYWRIGHT_ACCEPT_PASS_ON_RETRY,
    // Within the time limit of each CI job that runs the suite, so a hung run reports its tests.
    globalTimeout: 28 * 60_000,
    globalTeardown: coverage ? "./testing/server-coverage.ts" : undefined,
    reporter: coverage
        ? [["list"], ["./testing/coverage-reporter.ts"]]
        : "list",
    outputDir: coverage ? "coverage/test-results" : "test-results",
    use: {
        baseURL: serverOrigin(coverage),
        trace: "retain-on-failure",
        serviceWorkers: "block",
    },
    projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
    webServer: webServer(run),
});
