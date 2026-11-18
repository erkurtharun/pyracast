import { defineConfig, devices } from "@playwright/test";
import { coverage, productionServer } from "./tests/support/server";
const port = coverage ? 3102 : 3100;
const startCommand = `npm start -- --port ${port}`;
const normalCommand = productionServer
    ? startCommand
    : `npm run dev -- --port ${port}`;
export default defineConfig({
    testDir: "./tests",
    fullyParallel: true,
    forbidOnly: !!process.env.CI,
    retries: process.env.CI ? 2 : 0,
    failOnFlakyTests: !!process.env.CI,
    // Below the 25 minute CI job limit, so a hung run reports its tests instead of being killed.
    globalTimeout: 15 * 60_000,
    reporter: coverage
        ? [["list"], ["./testing/coverage-reporter.ts"]]
        : "list",
    outputDir: coverage ? "coverage/test-results" : "test-results",
    use: { baseURL: `http://127.0.0.1:${port}`, trace: "retain-on-failure" },
    projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
    webServer: {
        command: coverage ? `npm run build && ${startCommand}` : normalCommand,
        url: `http://127.0.0.1:${port}`,
        reuseExistingServer: !coverage && !process.env.CI,
        timeout: 120_000,
    },
});
