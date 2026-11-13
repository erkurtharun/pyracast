import { defineConfig } from "@playwright/test";

export default defineConfig({
    testDir: "./integration",
    preserveOutput: "always",
    workers: 1,
    use: { baseURL: "http://127.0.0.1:3101", browserName: "chromium" },
    webServer: {
        command: "npm run dev -- --port 3101",
        env: { ...process.env, NEXT_DIST_DIR: ".next-integration" },
        url: "http://127.0.0.1:3101",
        reuseExistingServer: false,
        timeout: 120_000,
    },
});
