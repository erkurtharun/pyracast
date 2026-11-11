import { defineConfig, devices } from "@playwright/test";
const coverage = process.env.PLAYWRIGHT_COVERAGE === "1";
const port = coverage ? 3102 : 3100;
export default defineConfig({
    testDir: "./tests",
    fullyParallel: true,
    forbidOnly: !!process.env.CI,
    retries: process.env.CI ? 2 : 0,
    reporter: coverage
        ? [["list"], ["./testing/coverage-reporter.ts"]]
        : "list",
    outputDir: coverage ? "coverage/test-results" : "test-results",
    use: { baseURL: `http://127.0.0.1:${port}`, trace: "retain-on-failure" },
    projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
    webServer: {
        command: coverage
            ? `npm run build && npm start -- --port ${port}`
            : `npm run dev -- --port ${port}`,
        url: `http://127.0.0.1:${port}`,
        reuseExistingServer: !coverage && !process.env.CI,
        timeout: 120_000,
    },
});
