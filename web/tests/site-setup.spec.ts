import { test, expect, type Page } from "./support/test";
import { errorBody } from "./support/errors";
import { mockApi, paged, plant, region, tenantRow } from "./support/workspace";

const site = { ...region, plant, reminder: null };
const location = {
    ...region,
    id: 13,
    name: "North location",
    status: "pending",
    plant: null,
    reminder: null,
    attention: "setup_incomplete",
    edit_blocked_reason: null,
};

async function sites(page: Page, rows: unknown[]) {
    await page.route("**/api/tenants/1/sites/", (route) =>
        route.request().method() === "GET"
            ? route.fulfill({ json: paged(rows) })
            : route.fallback(),
    );
}

async function asViewer(page: Page) {
    await page.route("**/api/tenants/", (route) =>
        route.fulfill({
            json: paged([
                tenantRow({ id: 1, name: "Sunfield Energy", role: "viewer" }),
            ]),
        }),
    );
}

async function openWizard(page: Page) {
    await page.goto("/app/");
    await page.getByRole("button", { name: "Add a plant" }).click();
    await page.getByLabel("Plant name", { exact: true }).fill("West plant");
    await page.getByLabel("Latitude (°)").fill("37");
    await page.getByLabel("Longitude (°)").fill("31");
}

test("the portfolio marks each plant that needs attention and counts what a search shows", async ({
    page,
}) => {
    await mockApi(page);
    await sites(page, [
        { ...site, attention: null, edit_blocked_reason: null },
        {
            ...site,
            id: 12,
            name: "West location",
            status: "rejected",
            plant: { ...plant, id: 22, name: "West array" },
            attention: "review_rejected",
            edit_blocked_reason: null,
        },
        location,
    ]);
    await page.goto("/app/");
    const cards = page.locator(".region-card");
    await expect(cards).toHaveCount(3);
    await expect(cards.filter({ hasText: "East array" })).not.toContainText(
        "Needs attention",
    );
    await expect(cards.filter({ hasText: "West array" })).toContainText(
        "Needs attention: Not approved. Edit the plant and resubmit it.",
    );
    await expect(cards.filter({ hasText: "North location" })).toContainText(
        "Needs attention: Setup is not complete.",
    );
    await expect(
        page.getByText("2 plants need attention.", { exact: true }),
    ).toBeVisible();
    await page.getByLabel("Search plants").fill("west");
    await expect(cards).toHaveCount(1);
    await expect(
        page.getByRole("status").filter({ hasText: "1 of 3" }),
    ).toBeVisible();
    await page.getByLabel("Search plants").fill("Antalya solar park");
    await expect(cards).toHaveCount(0);
    await expect(
        page.getByRole("status").filter({ hasText: "0 of 3" }),
    ).toBeVisible();
});

test("a member sees that an administrator finishes the setup", async ({
    page,
}) => {
    await mockApi(page);
    await asViewer(page);
    await sites(page, [location]);
    await page.goto("/app/");
    const card = page.getByRole("button", { name: /North location/ });
    await expect(card).toContainText(
        "Waiting for an administrator to finish setup",
    );
    await expect(card).not.toContainText("Complete setup");
    await card.click();
    await expect(
        page.getByText("Waiting for an administrator to finish setup"),
    ).toBeVisible();
    await expect(page.getByLabel("DC nameplate capacity (MW)")).toHaveCount(0);
});

test("complete setup asks only for the technical details and shows the location it keeps", async ({
    page,
}) => {
    await mockApi(page);
    await sites(page, [location]);
    await page.goto("/app/");
    await page.getByRole("button", { name: /Complete setup/ }).click();
    await expect(
        page.getByRole("heading", { name: "Complete plant setup" }),
    ).toBeVisible();
    await expect(
        page
            .getByRole("list", { name: "Plant setup progress" })
            .getByRole("listitem"),
    ).toHaveText(["Technical details", "Review"]);
    await expect(
        page.getByText("36.9°, 30.7° · Europe/Istanbul"),
    ).toBeVisible();
    await expect(page.getByLabel("Latitude (°)")).toHaveCount(0);
});

test("a suspended location without a plant says why its setup cannot be completed", async ({
    page,
}) => {
    await mockApi(page);
    await sites(page, [
        { ...location, status: "suspended", edit_blocked_reason: "suspended" },
    ]);
    const writes: string[] = [];
    page.on("request", (request) => {
        if (request.method() === "POST") writes.push(request.url());
    });
    await page.goto("/app/");
    await page.getByRole("button", { name: /North location/ }).click();
    await expect(
        page.getByText(
            "The plant is suspended, so its technical details cannot change. Contact Pyracast support to resume it.",
        ),
    ).toBeVisible();
    await expect(
        page.getByRole("button", { name: "Continue", exact: true }),
    ).toHaveCount(0);
    expect(writes).toEqual([]);
});

test("a server time-zone refusal reopens the location step with focus on the zone", async ({
    page,
}) => {
    await mockApi(page, { empty: true });
    await page.route("**/api/tenants/1/sites/", (route) =>
        route.request().method() === "POST"
            ? route.fulfill({
                  status: 400,
                  json: errorBody(
                      "invalid_request",
                      "Check the highlighted values and try again.",
                      { fields: { timezone: "An IANA timezone is required." } },
                  ),
              })
            : route.fallback(),
    );
    await openWizard(page);
    await page.getByRole("button", { name: "Continue", exact: true }).click();
    await page.getByLabel("DC nameplate capacity (MW)").fill("5");
    await page.getByRole("button", { name: "Continue", exact: true }).click();
    await page
        .getByRole("button", { name: "Submit plant for approval" })
        .click();
    const zone = page.getByLabel("Time zone");
    await expect(zone).toBeFocused();
    await expect(zone).toHaveAttribute("aria-invalid", "true");
    await expect(page.getByText("An IANA timezone is required.")).toBeVisible();
    await expect(
        page
            .getByRole("list", { name: "Plant setup progress" })
            .getByRole("listitem")
            .first(),
    ).toHaveAttribute("aria-current", "step");
    await expect(page.getByLabel("Plant name", { exact: true })).toHaveValue(
        "West plant",
    );
});

test("an open wizard keeps its input while a site refresh fails", async ({
    page,
}) => {
    await page.clock.install();
    await mockApi(page, { empty: true });
    let fail = false;
    await page.route("**/api/tenants/1/sites/", (route) =>
        fail && route.request().method() === "GET"
            ? route.fulfill({
                  status: 503,
                  json: errorBody("busy", "Site refresh temporarily failed."),
              })
            : route.fallback(),
    );
    await openWizard(page);
    fail = true;
    await page.clock.fastForward(120_000);
    await expect(page.locator("main").getByRole("alert")).toContainText(
        "Site refresh temporarily failed.",
    );
    await expect(page.getByLabel("Plant name", { exact: true })).toHaveValue(
        "West plant",
    );
    await expect(page.getByLabel("Latitude (°)")).toHaveValue("37");
});

test("Cancel waits while the plant is submitted", async ({ page }) => {
    await mockApi(page, { empty: true });
    let release = () => {};
    const held = new Promise<void>((resolve) => (release = resolve));
    await page.route("**/api/tenants/1/sites/", async (route) => {
        if (route.request().method() !== "POST") return route.fallback();
        await held;
        return route.fallback();
    });
    await openWizard(page);
    await page.getByRole("button", { name: "Continue", exact: true }).click();
    await page.getByLabel("DC nameplate capacity (MW)").fill("5");
    await page.getByRole("button", { name: "Continue", exact: true }).click();
    await page
        .getByRole("button", { name: "Submit plant for approval" })
        .click();
    await expect(
        page.getByRole("button", { name: "Cancel", exact: true }),
    ).toBeDisabled();
    release();
    await expect(
        page.getByRole("heading", { name: "West plant" }),
    ).toBeVisible();
});

test("pasting a coordinate pair into latitude fills both fields", async ({
    page,
}) => {
    await mockApi(page, { empty: true });
    await page.goto("/app/");
    await page.getByRole("button", { name: "Add a plant" }).click();
    await expect(
        page.getByText(
            "Paste a pair such as 41.0082, 28.9784 into Latitude to fill both.",
        ),
    ).toBeVisible();
    const latitude = page.getByLabel("Latitude (°)");
    const pasted = await latitude.evaluate((element) => {
        const data = new DataTransfer();
        data.setData("text/plain", "41.0082, 28.9784");
        const event = new ClipboardEvent("paste", {
            clipboardData: data,
            bubbles: true,
            cancelable: true,
        });
        element.dispatchEvent(event);
        return event.defaultPrevented;
    });
    expect(pasted).toBe(true);
    await expect(latitude).toHaveValue("41.0082");
    await expect(page.getByLabel("Longitude (°)")).toHaveValue("28.9784");
});

test("the zone list is prefilled from the device and offers the available zones", async ({
    page,
}) => {
    await mockApi(page, { empty: true });
    await page.goto("/app/");
    await page.getByRole("button", { name: "Add a plant" }).click();
    const zone = page.getByLabel("Time zone");
    await expect(zone).toHaveValue(
        await page.evaluate(
            () => Intl.DateTimeFormat().resolvedOptions().timeZone,
        ),
    );
    await zone.selectOption("Asia/Tokyo");
    await expect(zone).toHaveValue("Asia/Tokyo");
});

test("a plant in the recycle bin at the entered location is offered for restore first", async ({
    page,
}) => {
    await mockApi(page, { empty: true });
    let restored = false;
    const posts: string[] = [];
    await page.route("**/api/tenants/1/trash/regions/", (route) =>
        route.fulfill({
            json: paged([
                {
                    id: 15,
                    name: "Old location",
                    plant_name: "Old plant",
                    latitude: 37,
                    longitude: 31,
                    deleted_at: "2026-11-10T12:00:00Z",
                    purge_after: "2026-12-10T12:00:00Z",
                    restore_blocker: null,
                },
            ]),
        }),
    );
    await page.route("**/api/tenants/1/regions/15/restore/", (route) => {
        restored = true;
        return route.fulfill({ json: { ...region, id: 15 } });
    });
    page.on("request", (request) => {
        if (request.method() === "POST")
            posts.push(new URL(request.url()).pathname);
    });
    await openWizard(page);
    await page.getByRole("button", { name: "Continue", exact: true }).click();
    const offer = page.getByRole("group", {
        name: "Old plant is in the recycle bin",
    });
    await expect(offer).toContainText("Restore it to keep its saved data");
    await expect(
        offer.getByRole("button", { name: "Restore Old plant" }),
    ).toBeFocused();
    await offer.getByRole("button", { name: "Add a new plant" }).click();
    await expect(page.getByLabel("DC nameplate capacity (MW)")).toBeVisible();
    await page.getByRole("button", { name: "Back", exact: true }).click();
    await page.getByRole("button", { name: "Continue", exact: true }).click();
    await page
        .getByRole("group", { name: "Old plant is in the recycle bin" })
        .getByRole("button", { name: "Restore Old plant" })
        .click();
    await expect(page).toHaveURL("/app/w/1/plants/15/forecast/");
    expect(restored).toBe(true);
    expect(posts).toEqual(["/api/tenants/1/regions/15/restore/"]);
});

test("a refused duplicate location offers to open the plant that holds it", async ({
    page,
}) => {
    await mockApi(page, { empty: true });
    await page.route("**/api/tenants/1/sites/", (route) =>
        route.request().method() === "POST"
            ? route.fulfill({
                  status: 400,
                  json: {
                      ...errorBody(
                          "location_already_exists",
                          "This location is already in your company's portfolio. Open the existing plant.",
                      ),
                      region_id: 11,
                  },
              })
            : route.fallback(),
    );
    await openWizard(page);
    await page.getByRole("button", { name: "Continue", exact: true }).click();
    await page.getByLabel("DC nameplate capacity (MW)").fill("5");
    await page.getByRole("button", { name: "Continue", exact: true }).click();
    await page
        .getByRole("button", { name: "Submit plant for approval" })
        .click();
    await page.getByRole("button", { name: "Open the existing plant" }).click();
    await expect(page).toHaveURL("/app/w/1/plants/11/forecast/");
});

test("a suspended plant shows its technical details read-only with the reason", async ({
    page,
}) => {
    await mockApi(page);
    await sites(page, [
        {
            ...site,
            status: "suspended",
            edit_blocked_reason: "suspended",
            attention: "suspended",
        },
    ]);
    await page.goto("/app/w/1/plants/11/settings/");
    await expect(
        page.getByText(
            "The plant is suspended, so its technical details cannot change. Contact Pyracast support to resume it.",
        ),
    ).toBeVisible();
    await expect(
        page.getByRole("button", { name: "Save configuration" }),
    ).toHaveCount(0);
    await expect(page.getByLabel("DC nameplate capacity (MW)")).toBeDisabled();
});

test("the plant CSV is offered with the technical details", async ({
    page,
}) => {
    await mockApi(page);
    await page.goto("/app/w/1/plants/11/settings/");
    await expect(
        page.getByRole("button", { name: "Download plant CSV" }),
    ).toBeVisible();
    await page.getByRole("button", { name: "Location", exact: true }).click();
    await expect(page.getByText("Current location:")).toBeVisible();
    await expect(
        page.getByRole("button", { name: "Download plant CSV" }),
    ).toHaveCount(0);
});

test("a pending plant lists its next steps instead of an empty forecast", async ({
    page,
}) => {
    await mockApi(page);
    await sites(page, [
        {
            ...site,
            status: "pending",
            plant: { ...plant, current_configuration: null },
        },
    ]);
    await page.goto("/app/w/1/plants/11/");
    const steps = page.getByRole("list", { name: "Next steps" });
    await expect(steps.getByRole("listitem")).toHaveCount(3);
    await expect(page.getByText("No forecast for this day yet")).toHaveCount(0);
});

test("a location request offers the available zones with the current one chosen", async ({
    page,
}) => {
    await mockApi(page);
    await page.goto("/app/w/1/plants/11/settings/?view=location");
    await page.getByText("Request location change").click();
    const zone = page.getByLabel("Time zone");
    await expect(zone).toHaveValue("Europe/Istanbul");
    await zone.selectOption("Asia/Tokyo");
    await expect(zone).toHaveValue("Asia/Tokyo");
});
