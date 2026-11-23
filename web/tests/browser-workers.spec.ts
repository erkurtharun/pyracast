import { expect, test } from "@playwright/test";

test("the mocked browser suite registers no worker", async ({ page }) => {
    await page.goto("/privacy/");
    const registered = await page.evaluate(async () => {
        await navigator.serviceWorker.register("/app/sw.js", {
            scope: "/app/",
        });
        return (await navigator.serviceWorker.getRegistrations()).length;
    });
    expect(registered).toBe(0);
});
