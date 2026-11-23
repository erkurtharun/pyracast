import { defineConfig, devices } from "@playwright/test";
import {
    coverage,
    coveragePort,
    coverageReadinessPath,
    productionServer,
    serverInspector,
    serverSnapshots,
} from "./tests/support/server";
const port = coverage ? coveragePort : 3100;
const origin = `http://127.0.0.1:${port}`;
const startCommand = `npm start -- --port ${port}`;
const normalCommand = productionServer
    ? startCommand
    : `npm run dev -- --port ${port}`;
const coverageCommand = `${startCommand} --inspect=${serverInspector.host}:${serverInspector.port}`;
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
    use: { baseURL: origin, trace: "retain-on-failure" },
    projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
    webServer: {
        command: coverage ? coverageCommand : normalCommand,
        env: coverage ? { NODE_V8_COVERAGE: serverSnapshots } : {},
        url: coverage ? `${origin}${coverageReadinessPath}` : origin,
        reuseExistingServer: !coverage && !process.env.CI,
        timeout: 120_000,
    },
});
