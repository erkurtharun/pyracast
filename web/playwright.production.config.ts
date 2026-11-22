import { defineConfig, devices } from "@playwright/test";

const serverPort = 3110;
const frontPort = 3111;

export default defineConfig({
    testDir: "./production",
    outputDir: "production-results",
    workers: 1,
    forbidOnly: !!process.env.CI,
    retries: process.env.CI ? 2 : 0,
    failOnFlakyTests: !!process.env.CI,
    globalTimeout: 10 * 60_000,
    reporter: "list",
    use: {
        baseURL: `http://127.0.0.1:${frontPort}`,
        trace: "retain-on-failure",
        serviceWorkers: "allow",
    },
    projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
    webServer: [
        {
            command: `npm start -- --port ${serverPort}`,
            url: `http://127.0.0.1:${serverPort}/app/sw.js`,
            reuseExistingServer: false,
            timeout: 120_000,
        },
        {
            command: "node testing/front.ts",
            env: {
                FRONT_PORT: String(frontPort),
                FRONT_UPSTREAM: `http://127.0.0.1:${serverPort}`,
            },
            url: `http://127.0.0.1:${frontPort}/__front/state`,
            reuseExistingServer: false,
            timeout: 30_000,
        },
    ],
});
