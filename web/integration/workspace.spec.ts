import { expect, test } from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";

test("a customer manages an approved region through the real API", async ({
    page,
}, testInfo) => {
    await page.goto("/");
    await expect(page.getByLabel("Username")).toBeVisible();
    await page.screenshot({
        path: testInfo.outputPath("login-desktop.png"),
        fullPage: true,
    });
    await page.getByLabel("Username").fill(process.env.BROWSER_USERNAME!);
    await page.getByLabel("Password").fill(process.env.BROWSER_PASSWORD!);
    await page.getByRole("button", { name: /sign in/i }).click();
    await expect(
        page.getByRole("heading", { name: "A clearer outlook." }),
    ).toBeVisible();

    const forbidden = await page.evaluate(
        async ({ tenant, region }) => {
            const response = await fetch(
                `/api/tenants/${tenant}/regions/${region}/`,
                {
                    credentials: "same-origin",
                    cache: "no-store",
                },
            );
            return response.status;
        },
        {
            tenant: process.env.BROWSER_OTHER_TENANT,
            region: process.env.BROWSER_OTHER_REGION,
        },
    );
    expect([403, 404]).toContain(forbidden);

    await page.getByRole("button", { name: /Site/ }).click();
    await page.getByLabel("Forecast date").fill(process.env.BROWSER_DATE!);
    await expect(
        page.getByRole("heading", { name: "Regional irradiance" }),
    ).toBeVisible();
    await expect(
        page.getByRole("img", { name: /Irradiance by interval/ }),
    ).toBeVisible();
    await expect(page.getByText("kWh/m²").first()).toBeVisible();
    await page.screenshot({
        path: testInfo.outputPath("region-desktop.png"),
        fullPage: true,
    });
    const regionA11y = await new AxeBuilder({ page })
        .withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"])
        .analyze();
    expect(
        regionA11y.violations.filter(
            (item) => item.impact === "critical" || item.impact === "serious",
        ),
    ).toEqual([]);

    await page.getByRole("button", { name: "Solar plants" }).click();
    await page.getByRole("button", { name: "+ Add a plant" }).click();
    await page.getByLabel("Plant name").fill("Browser plant");
    await page.getByLabel("DC nameplate capacity (MW)").fill("130");
    await page.getByLabel("Inverter AC capacity (MW, optional)").fill("100");
    await page.getByRole("button", { name: "Create plant" }).click();
    await expect(
        page.getByRole("heading", { name: "Browser plant" }),
    ).toBeVisible();
    await expect(page.getByLabel("Forecast date")).toHaveValue(
        process.env.BROWSER_DATE!,
    );
    await page.getByLabel("Resolution").selectOption("60");
    await expect(
        page.getByText("View interval data (24 intervals)"),
    ).toBeVisible();
    await page.getByLabel("Resolution").selectOption("30");
    await expect(
        page.getByText("View interval data (48 intervals)"),
    ).toBeVisible();
    await page.getByLabel("Forecast date").fill(process.env.BROWSER_TODAY!);
    await expect(
        page.getByRole("heading", { name: "No configuration for this day" }),
    ).toBeVisible();
    await expect(page.getByText("configuration_unavailable")).toHaveCount(0);
    await page.getByLabel("Forecast date").fill(process.env.BROWSER_DATE!);
    await expect(
        page.getByText("View interval data (48 intervals)"),
    ).toBeVisible();

    await page.getByRole("button", { name: /all plants/i }).click();
    await page.getByRole("button", { name: "Email reminders" }).click();
    await page.getByLabel("Enable forecast emails for this region").check();
    await page.getByLabel(/Delivery times/).fill("10:00");
    await page.getByLabel("Email address (optional override)").fill("");
    await page.getByRole("button", { name: "Save reminders" }).click();
    await expect(page.getByText(/member@example.com/)).toBeVisible();
    await expect(page.getByLabel(/Delivery times/)).toHaveValue("10:00");
    await expect(page.getByText("Enabled", { exact: true })).toBeVisible();
    await page.setViewportSize({ width: 390, height: 844 });
    await page.screenshot({
        path: testInfo.outputPath("reminders-mobile.png"),
        fullPage: true,
    });
    expect(
        await page.evaluate(
            () => document.documentElement.scrollWidth <= window.innerWidth,
        ),
    ).toBe(true);
    const mobileA11y = await new AxeBuilder({ page })
        .withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"])
        .analyze();
    expect(
        mobileA11y.violations.filter(
            (item) => item.impact === "critical" || item.impact === "serious",
        ),
    ).toEqual([]);
});
