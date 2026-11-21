import type { Page } from "@playwright/test";
import { test, expect } from "./support/test";
import { accessibilityViolations } from "./support/accessibility";
import { answer, mockApi, paged, plant, region } from "./support/workspace";
import { errorBody } from "./support/errors";

test("renames a plant without changing its location", async ({ page }) => {
    await mockApi(page);
    let current = plant;
    await page.route("**/api/tenants/1/sites/", (route) =>
        route.fulfill({ json: paged([{ ...region, plant: current }]) }),
    );
    await page.route(
        "**/api/tenants/1/regions/11/plants/21/",
        async (route) => {
            expect(route.request().method()).toBe("PUT");
            expect(route.request().headers()["x-csrftoken"]).toBe(
                "signed-token",
            );
            expect(route.request().postDataJSON()).toMatchObject({
                name: "New name",
                dc_capacity_mw: 5,
            });
            current = { ...plant, name: "New name" };
            await route.fulfill({ json: current });
        },
    );
    await page.goto("/app/");
    await page.getByRole("button", { name: /East array/ }).click();
    await page.getByRole("button", { name: "Settings", exact: true }).click();
    await page.getByLabel("Plant name").fill("New name");
    await page.getByRole("button", { name: "Save configuration" }).click();
    await expect(page.getByRole("heading", { name: "New name" })).toBeVisible();
    await page.getByRole("button", { name: "Location", exact: true }).click();
    await expect(
        page.getByText(
            "Current location: 36.9\u00b0, 30.7\u00b0 \u00b7 Europe/Istanbul",
            { exact: true },
        ),
    ).toBeVisible();
    await page.getByRole("button", { name: "All plants" }).click();
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
                    json: errorBody("invalid_request", "Please try again."),
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
            decision_reason: "",
        };
        await route.fulfill({
            json:
                route.request().method() === "POST"
                    ? item
                    : paged(pending ? [item] : []),
        });
    });
    await page.goto("/app/");
    await page
        .getByRole("button", { name: /East array|Antalya solar park/ })
        .click();
    await page.getByRole("button", { name: "Irradiance", exact: true }).click();
    await page.getByRole("button", { name: "Settings", exact: true }).click();
    await page.getByRole("button", { name: "Location", exact: true }).click();
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
    await expect(page.getByText("In review", { exact: true })).toBeVisible();
    expect(
        await page.evaluate(
            () => document.documentElement.scrollWidth <= innerWidth,
        ),
    ).toBe(true);
    expect(await accessibilityViolations(page)).toEqual([]);
});

test("a failed reload after a location request keeps the form until a retry shows the request", async ({
    page,
}) => {
    await mockApi(page);
    let pending = false;
    let unavailable = false;
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
        decision_reason: "",
    };
    await page.route("**/location-change-requests/", async (route) => {
        if (route.request().method() === "POST") {
            pending = true;
            unavailable = true;
            return route.fulfill({ json: item });
        }
        if (unavailable)
            return route.fulfill({
                status: 503,
                json: errorBody("busy", "Location service is busy."),
            });
        return route.fulfill({ json: paged(pending ? [item] : []) });
    });
    await page.goto("/app/");
    await page
        .getByRole("button", { name: /East array|Antalya solar park/ })
        .click();
    await page.getByRole("button", { name: "Settings", exact: true }).click();
    await page.getByRole("button", { name: "Location", exact: true }).click();
    await page.getByText("Request location change", { exact: true }).click();
    await page.getByLabel("Latitude (°)").fill("37");
    await page.getByRole("button", { name: "Submit location request" }).click();
    const notice = page
        .getByRole("alert")
        .filter({ hasText: "Could not refresh" });
    await expect(notice).toContainText("Location service is busy.");
    await expect(page.getByLabel("Latitude (°)")).toHaveValue("37");
    unavailable = false;
    await notice
        .getByRole("button", { name: "Try again", exact: true })
        .click();
    await expect(notice).toHaveCount(0);
    await expect(page.getByText("37°, 31° · Europe/Istanbul")).toBeVisible();
});

const pendingRequest = {
    id: 1,
    base_revision: 1,
    latitude: 37,
    longitude: 31,
    timezone: "Europe/Istanbul",
    reason: "Move",
    status: "pending",
    created_at: "2026-01-01T00:00:00Z",
    reviewed_at: null,
    decision_reason: "",
};

async function openLocationSettings(page: Page) {
    await page.goto("/app/");
    await page
        .getByRole("button", { name: /East array|Antalya solar park/ })
        .click();
    await page.getByRole("button", { name: "Settings", exact: true }).click();
    await page.getByRole("button", { name: "Location", exact: true }).click();
    await page.getByText("Request location change", { exact: true }).click();
}

test("withdraws a pending location request and offers the form again", async ({
    page,
}) => {
    await mockApi(page);
    let rows: object[] = [pendingRequest];
    await page.route(
        "**/location-change-requests/1/withdraw/",
        async (route) => {
            expect(route.request().method()).toBe("POST");
            const withdrawn = {
                ...pendingRequest,
                status: "withdrawn",
                reviewed_at: "2026-01-02T00:00:00Z",
            };
            rows = [withdrawn];
            await route.fulfill({ json: withdrawn });
        },
    );
    await page.route("**/location-change-requests/", (route) =>
        route.fulfill({ json: paged(rows) }),
    );
    await openLocationSettings(page);
    await page.getByRole("button", { name: "Withdraw request" }).click();
    await expect(
        page.getByText("Location change withdrawn on Jan 2, 2026."),
    ).toBeVisible();
    await expect(
        page.getByRole("button", { name: "Submit location request" }),
    ).toBeVisible();
});

test("a direct move of a never-approved plant refills the form with the stored location", async ({
    page,
}) => {
    await mockApi(page);
    let current = region;
    let rows: object[] = [];
    await page.route("**/api/tenants/1/sites/", (route) =>
        route.fulfill({ json: paged([{ ...current, plant }]) }),
    );
    await page.route("**/location-change-requests/", async (route) => {
        if (route.request().method() !== "POST")
            return route.fulfill({ json: paged(rows) });
        const moved = {
            ...pendingRequest,
            latitude: 37.123457,
            reason: "",
            status: "approved",
            reviewed_at: "2026-01-02T00:00:00Z",
        };
        current = { ...region, latitude: 37.123457, revision: 2 };
        rows = [moved];
        await route.fulfill({ status: 201, json: moved });
    });
    await openLocationSettings(page);
    await page.getByLabel("Latitude (°)").fill("37.1234567");
    await page.getByLabel("Reason (optional)").fill("Survey");
    await page.getByRole("button", { name: "Submit location request" }).click();
    await expect(
        page.getByText("Location change approved on Jan 2, 2026."),
    ).toBeVisible();
    await expect(page.getByLabel("Latitude (°)")).toHaveValue("37.123457");
    await expect(page.getByLabel("Reason (optional)")).toHaveValue("");
});

test("shows the operator's decision and note on the latest request", async ({
    page,
}) => {
    await mockApi(page);
    await page.route("**/location-change-requests/", (route) =>
        route.fulfill({
            json: paged([
                {
                    ...pendingRequest,
                    id: 2,
                    status: "approved",
                    reviewed_at: "2026-01-03T00:00:00Z",
                    decision_reason: "Matches the grid connection record",
                },
            ]),
        }),
    );
    await openLocationSettings(page);
    await expect(
        page.getByText(
            "Location change approved on Jan 3, 2026. Forecasts pause until the new connection is approved.",
        ),
    ).toBeVisible();
    await expect(
        page.getByText("Note: Matches the grid connection record"),
    ).toBeVisible();
});

test("a rejected plant shows its reason in plant time and is resubmitted for review", async ({
    page,
}) => {
    await mockApi(page);
    const rejected = answer("RegionOutput", {
        ...region,
        status: "rejected",
        reviewed_at: "2026-11-20T09:15:00Z",
        review_reason: "The declared capacity does not match the licence.",
    });
    let current = answer("SiteOutput", { ...rejected, plant });
    await page.route("**/api/tenants/1/sites/", (route) =>
        route.fulfill({
            json: answer("PaginatedSiteOutputList", paged([current])),
        }),
    );
    await page.route("**/api/tenants/1/regions/11/resubmit/", async (route) => {
        expect(route.request().method()).toBe("POST");
        expect(route.request().postDataJSON()).toEqual({
            expected_revision: 1,
        });
        current = { ...current, status: "pending" };
        await route.fulfill({
            json: answer("RegionOutput", { ...rejected, status: "pending" }),
        });
    });
    await page.goto("/app/");
    await page.getByRole("button", { name: /East array/ }).click();

    const notice = page.getByRole("status").filter({
        hasText: "Pyracast support did not approve this plant.",
    });
    await expect(notice).toContainText(
        "The declared capacity does not match the licence.",
    );
    await expect(notice).toContainText("12:15");
    await expect(notice).toContainText("(plant time)");
    await notice.getByRole("button", { name: "Resubmit for review" }).click();

    await expect(
        page.getByText("Awaiting administrator approval."),
    ).toBeVisible();
    await expect(
        page.getByText("Pyracast support did not approve this plant."),
    ).toHaveCount(0);
});
