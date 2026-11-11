import { test, expect } from "./support/test";
import { mockApi, paged, region } from "./support/workspace";
import AxeBuilder from "@axe-core/playwright";

test("renames a region without changing its location", async ({ page }) => {
    await mockApi(page);
    let current = region;
    await page.route("**/api/tenants/1/regions/", async (route) => {
        await route.fulfill({ json: paged([current]) });
    });
    await page.route("**/api/tenants/1/regions/11/", async (route) => {
        expect(route.request().method()).toBe("PATCH");
        expect(route.request().headers()["x-csrftoken"]).toBe("signed-token");
        expect(route.request().postDataJSON()).toEqual({ name: "New name" });
        current = { ...region, name: "New name" };
        await route.fulfill({ json: current });
    });
    await page.route("**/location-change-requests/", async (route) => {
        await route.fulfill({ json: paged([]) });
    });
    await page.goto("/");
    await page.getByRole("button", { name: /Antalya solar park/ }).click();
    await page.getByRole("button", { name: "Edit region" }).click();
    await page.getByLabel("Region name").fill("New name");
    await page.getByRole("button", { name: "Save name" }).click();
    await expect(page.getByRole("heading", { name: "New name" })).toBeVisible();
    await page.getByRole("button", { name: "All regions" }).click();
    await expect(page.getByRole("button", { name: /New name/ })).toBeVisible();
});

test("requests a location change and preserves the current location", async ({
    page,
}) => {
    await page.setViewportSize({ width: 390, height: 844 });
    await mockApi(page);
    let pending = false;
    let fail = true;
    await page.route("**/location-change-requests/", async (route) => {
        if (route.request().method() === "POST") {
            expect(route.request().headers()["x-csrftoken"]).toBe(
                "signed-token",
            );
            expect(route.request().postDataJSON()).toEqual({
                latitude: 37,
                longitude: 31,
                timezone: "Europe/Istanbul",
                reason: "Move",
                expected_revision: 1,
            });
            if (fail) {
                fail = false;
                await route.fulfill({
                    status: 400,
                    json: { detail: "Please try again." },
                });
                return;
            }
            pending = true;
        }
        const item = {
            id: 1,
            base_revision: 1,
            latitude: 37,
            longitude: 31,
            timezone: "Europe/Istanbul",
            reason: "Move",
            status: "pending",
            created_at: "2026-01-01T00:00:00Z",
            reviewed_at: null,
        };
        await route.fulfill({
            json:
                route.request().method() === "POST"
                    ? item
                    : paged(pending ? [item] : []),
        });
    });
    await page.goto("/");
    await page.getByRole("button", { name: /Antalya solar park/ }).click();
    await page.getByRole("button", { name: "Edit region" }).click();
    await page.getByText("Request location change", { exact: true }).click();
    await page.getByLabel("Latitude (°)").fill("37");
    await page.getByLabel("Longitude (°)").fill("31");
    await page.getByLabel("Reason (optional)").fill("Move");
    await page.getByRole("button", { name: "Submit location request" }).click();
    await expect(page.getByText("Please try again.")).toBeVisible();
    await expect(page.getByLabel("Latitude (°)")).toHaveValue("37");
    await page.getByRole("button", { name: "Submit location request" }).click();
    await expect(page.getByText("37°, 31° · Europe/Istanbul")).toBeVisible();
    await expect(
        page.getByText("Current location: 36.9°, 30.7° · Europe/Istanbul"),
    ).toBeVisible();
    await expect(page.getByText("pending", { exact: true })).toBeVisible();
    expect(
        await page.evaluate(
            () => document.documentElement.scrollWidth <= innerWidth,
        ),
    ).toBe(true);
    expect((await new AxeBuilder({ page }).analyze()).violations).toEqual([]);
});
