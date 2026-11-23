import { defineConfig, devices } from "@playwright/test";
import { coverageRun, serverOrigin, webServer } from "./tests/support/server";

const run = coverageRun();
const coverage = !!run;
export default defineConfig({
    testDir: "./tests",
    fullyParallel: true,
    forbidOnly: !!process.env.CI,
    retries: process.env.CI ? 2 : 0,
    failOnFlakyTests: !!process.env.CI,
    // Below the 25 minute CI job limit, so a hung run reports its tests instead of being killed.
    globalTimeout: 15 * 60_000,
    globalTeardown: coverage ? "./testing/server-coverage.ts" : undefined,
    reporter: coverage
        ? [["list"], ["./testing/coverage-reporter.ts"]]
        : "list",
    outputDir: coverage ? "coverage/test-results" : "test-results",
    use: { baseURL: serverOrigin(coverage), trace: "retain-on-failure" },
    projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
    webServer: webServer(run),
});
