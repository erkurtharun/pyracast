import { defineConfig } from "@playwright/test";

const origin = process.env.BROWSER_ORIGIN;
const port = process.env.BROWSER_WEB_PORT;
if (!origin || !port)
    throw new Error(
        "Run the integration specs through tests/e2e; they seed the backend and set BROWSER_ORIGIN and BROWSER_WEB_PORT.",
    );

export default defineConfig({
    testDir: "./integration",
    outputDir: "integration-results",
    preserveOutput: "always",
    workers: 1,
    forbidOnly: !!process.env.CI,
    // Below the 300 s spec timeout of tests/support/browser.py, so Playwright reports first.
    globalTimeout: 240_000,
    use: {
        baseURL: origin,
        browserName: "chromium",
        trace: "retain-on-failure",
    },
    webServer: {
        command: `npm start -- --port ${port}`,
        env: { ...process.env, NEXT_TELEMETRY_DISABLED: "1" },
        url: `http://127.0.0.1:${port}`,
        reuseExistingServer: false,
        timeout: 120_000,
    },
});
