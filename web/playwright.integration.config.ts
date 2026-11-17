import { defineConfig } from "@playwright/test";

export default defineConfig({
    testDir: "./integration",
    outputDir: "integration-results",
    preserveOutput: "always",
    workers: 1,
    forbidOnly: !!process.env.CI,
    // Below the 240 s subprocess timeout of tests/test_browser.py, so Playwright reports first.
    globalTimeout: 200_000,
    use: {
        baseURL: "http://127.0.0.1:3101",
        browserName: "chromium",
        trace: "retain-on-failure",
    },
    webServer: {
        command: "npm run dev -- --port 3101",
        env: { ...process.env, NEXT_DIST_DIR: ".next-integration" },
        url: "http://127.0.0.1:3101",
        reuseExistingServer: false,
        timeout: 120_000,
    },
});
