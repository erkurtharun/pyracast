import type { Locator } from "@playwright/test";
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

async function toReview(page: Page) {
    await page.getByRole("button", { name: "Continue", exact: true }).click();
    await page.getByLabel("DC nameplate capacity (MW)").fill("5");
    await page.getByRole("button", { name: "Continue", exact: true }).click();
}

function refuse(page: Page, fields: Record<string, string>) {
    return page.route("**/api/tenants/1/sites/", (route) =>
        route.request().method() === "POST"
            ? route.fulfill({
                  status: 400,
                  json: errorBody(
                      "invalid_request",
                      "Check the highlighted values and try again.",
                      { fields },
                  ),
              })
            : route.fallback(),
    );
}

const binned = (id: number, name: string, deleted_at: string) => ({
    id,
    name,
    plant_name: name,
    latitude: 37,
    longitude: 31,
    deleted_at,
    purge_after: "2026-12-30T12:00:00Z",
    restore_blocker: null,
});

test("a pasted single number with a decimal comma is not split into a pair", async ({
    page,
}) => {
    await mockApi(page, { empty: true });
    await page.goto("/app/");
    await page.getByRole("button", { name: "Add a plant" }).click();
    const latitude = page.getByLabel("Latitude (°)");
    const split = await latitude.evaluate((element) => {
        const data = new DataTransfer();
        data.setData("text/plain", "41,0082");
        const event = new ClipboardEvent("paste", {
            clipboardData: data,
            bubbles: true,
            cancelable: true,
        });
        element.dispatchEvent(event);
        return event.defaultPrevented;
    });
    expect(split).toBe(false);
    await expect(page.getByLabel("Longitude (°)")).toHaveValue("");
});

test("a refusal of fields on two steps opens the earlier step on its refused field", async ({
    page,
}) => {
    await mockApi(page, { empty: true });
    await refuse(page, {
        dc_capacity_mw: "Enter a positive capacity.",
        timezone: "An IANA timezone is required.",
    });
    await openWizard(page);
    await toReview(page);
    await page
        .getByRole("button", { name: "Submit plant for approval" })
        .click();
    await expect(page.getByLabel("Time zone")).toBeFocused();
});

test("a refused field no step holds stays on the review with its message", async ({
    page,
}) => {
    await mockApi(page, { empty: true });
    await refuse(page, { "plant.serial": "The serial number is unknown." });
    await openWizard(page);
    await toReview(page);
    await page
        .getByRole("button", { name: "Submit plant for approval" })
        .click();
    await expect(page.getByRole("alert")).toContainText(
        "The serial number is unknown.",
    );
    await expect(
        page
            .getByRole("list", { name: "Plant setup progress" })
            .getByRole("listitem")
            .last(),
    ).toHaveAttribute("aria-current", "step");
});

test("a second submit before the first answer sends one request", async ({
    page,
}) => {
    await mockApi(page, { empty: true });
    let posts = 0;
    let release = () => {};
    const held = new Promise<void>((resolve) => (release = resolve));
    await page.route("**/api/tenants/1/sites/", async (route) => {
        if (route.request().method() !== "POST") return route.fallback();
        posts++;
        await held;
        return route.fallback();
    });
    await openWizard(page);
    await toReview(page);
    const form = page
        .locator("form")
        .filter({ has: page.getByLabel("DC nameplate capacity (MW)") });
    await form.evaluate((form: HTMLFormElement) => {
        form.requestSubmit();
        form.requestSubmit();
    });
    await expect.poll(() => posts).toBe(1);
    release();
    await expect(
        page.getByRole("heading", { name: "West plant" }),
    ).toBeVisible();
    expect(posts).toBe(1);
});

test("a restore through the wizard holds the form and goes nowhere once the wizard is left", async ({
    page,
}) => {
    await mockApi(page, { empty: true });
    await page.route("**/api/tenants/1/trash/regions/", (route) =>
        route.fulfill({
            json: paged([binned(15, "Old plant", "2026-11-10T12:00:00Z")]),
        }),
    );
    let restores = 0;
    let release = () => {};
    const held = new Promise<void>((resolve) => (release = resolve));
    await page.route("**/api/tenants/1/regions/15/restore/", async (route) => {
        restores++;
        await held;
        return route.fulfill({ json: { ...region, id: 15 } });
    });
    await openWizard(page);
    await page.getByRole("button", { name: "Continue", exact: true }).click();
    await page.getByRole("button", { name: "Restore Old plant" }).click();
    await expect(
        page.getByRole("button", { name: "Cancel", exact: true }),
    ).toBeDisabled();
    await expect(
        page.getByRole("button", { name: "Continue", exact: true }),
    ).toBeDisabled();
    await expect(page.getByLabel("Latitude (°)")).toBeDisabled();
    await page.getByRole("button", { name: "Recycle bin" }).click();
    await expect(page).toHaveURL("/app/w/1/trash/");
    release();
    await expect.poll(() => restores).toBe(1);
    await expect(page).toHaveURL("/app/w/1/trash/");
});

test("the bin offer names the most recently deleted plant at the place and says there are more", async ({
    page,
}) => {
    await mockApi(page, { empty: true });
    await page.route("**/api/tenants/1/trash/regions/", (route) =>
        route.fulfill({
            json: paged([
                binned(15, "Old plant", "2026-10-25T12:00:00Z"),
                binned(16, "Newer plant", "2026-11-20T12:00:00Z"),
            ]),
        }),
    );
    await openWizard(page);
    await page.getByRole("button", { name: "Continue", exact: true }).click();
    const offer = page.getByRole("group", {
        name: "Newer plant is in the recycle bin",
    });
    await expect(offer).toContainText(
        "1 more plant from this location is in the recycle bin.",
    );
    await expect(
        offer.getByRole("button", { name: "Restore Newer plant" }),
    ).toBeFocused();
});

test("a blocked location without a plant is opened, not offered for setup", async ({
    page,
}) => {
    await mockApi(page);
    await sites(page, [
        {
            ...location,
            status: "suspended",
            attention: "suspended",
            edit_blocked_reason: "suspended",
        },
    ]);
    await page.goto("/app/");
    const card = page.getByRole("button", { name: /North location/ });
    await expect(card).toContainText(
        "Needs attention: Suspended. Forecasts are paused.",
    );
    await expect(card).toContainText("Open plant");
    await expect(card).not.toContainText("Complete setup");
});

test("the zone list offers UTC and no empty choice", async ({ page }) => {
    await mockApi(page, { empty: true });
    await page.goto("/app/");
    await page.getByRole("button", { name: "Add a plant" }).click();
    const zone = page.getByLabel("Time zone");
    await zone.selectOption("UTC");
    await expect(zone).toHaveValue("UTC");
    await expect(zone.locator('option[value=""]')).toHaveCount(0);
});

test("a browser without a zone list asks for the zone as text", async ({
    page,
}) => {
    await page.addInitScript(() => {
        Reflect.deleteProperty(Intl, "supportedValuesOf");
    });
    await mockApi(page, { empty: true });
    await page.goto("/app/");
    await page.getByRole("button", { name: "Add a plant" }).click();
    await expect(page.getByRole("textbox", { name: "Time zone" })).toHaveValue(
        await page.evaluate(
            () => Intl.DateTimeFormat().resolvedOptions().timeZone,
        ),
    );
});

test("a location request without a browser zone list asks for the zone as text", async ({
    page,
}) => {
    await page.addInitScript(() => {
        Reflect.deleteProperty(Intl, "supportedValuesOf");
    });
    await mockApi(page);
    await page.goto("/app/w/1/plants/11/settings/?view=location");
    await page.getByText("Request location change").click();
    await expect(page.getByRole("textbox", { name: "Time zone" })).toHaveValue(
        "Europe/Istanbul",
    );
});

test("an approved plant whose connection stopped says so where the portfolio opens it", async ({
    page,
}) => {
    await mockApi(page);
    await sites(page, [
        {
            ...site,
            attention: "connection_unavailable",
            edit_blocked_reason: "connection_unavailable",
        },
    ]);
    await page.goto("/app/");
    const marker =
        "Needs attention: The data connection needs Pyracast support.";
    const card = page.getByRole("button", { name: /East array/ });
    await expect(card).toContainText(marker);
    await card.click();
    await expect(page).toHaveURL("/app/w/1/plants/11/forecast/");
    await expect(
        page.getByRole("status").filter({ hasText: marker }),
    ).toBeVisible();
});

type Entry = "typed" | "pasted";

// A synthetic paste runs the page's paste handlers but has no default action, so the text is
// then inserted as the browser's own paste inserts it.
async function enter(field: Locator, text: string, entry: Entry) {
    await field.fill("");
    if (entry === "typed") {
        await field.pressSequentially(text);
        return;
    }
    await field.focus();
    const prevented = await field.evaluate((element, text) => {
        const data = new DataTransfer();
        data.setData("text/plain", text);
        const event = new ClipboardEvent("paste", {
            clipboardData: data,
            bubbles: true,
            cancelable: true,
        });
        element.dispatchEvent(event);
        return event.defaultPrevented;
    }, text);
    expect(prevented).toBe(false);
    await field.page().keyboard.insertText(text);
}

const labels = {
    en: {
        addPlant: "Add a plant",
        name: "Plant name",
        latitude: "Latitude (°)",
        longitude: "Longitude (°)",
        continue: "Continue",
        capacity: "DC nameplate capacity (MW)",
        submit: "Submit plant for approval",
        review: "41.0082°, 17.5°",
        requestChange: "Request location change",
        submitRequest: "Submit location request",
    },
    tr: {
        addPlant: "Santral ekle",
        name: "Santral adı",
        latitude: "Enlem (°)",
        longitude: "Boylam (°)",
        continue: "Devam et",
        capacity: "DC kurulu güç (MW)",
        submit: "Santrali onaya gönder",
        review: "41,0082°, 17,5°",
        requestChange: "Konum değişikliği iste",
        submitRequest: "Konum talebini gönder",
    },
} as const;

for (const language of ["en", "tr"] as const)
    for (const locale of ["en-US", "tr-TR"])
        test.describe(`a ${language} session in a ${locale} browser`, () => {
            test.use({ locale });
            const text = labels[language];
            for (const entry of ["typed", "pasted"] as const) {
                test(`decimal commas ${entry} into the wizard's coordinates are sent as those numbers`, async ({
                    page,
                    baseURL,
                }) => {
                    await mockApi(page, { empty: true });
                    await page.context().addCookies([
                        {
                            name: "pyracast_language",
                            value: language,
                            url: baseURL!,
                        },
                    ]);
                    const sent: unknown[] = [];
                    await page.route("**/api/tenants/1/sites/", (route) => {
                        if (route.request().method() === "POST")
                            sent.push(route.request().postDataJSON());
                        return route.fallback();
                    });
                    await page.goto("/app/");
                    await page
                        .getByRole("button", { name: text.addPlant })
                        .click();
                    await page
                        .getByLabel(text.name, { exact: true })
                        .fill("West plant");
                    const latitude = page.getByLabel(text.latitude);
                    const longitude = page.getByLabel(text.longitude);
                    await expect(latitude).not.toHaveAttribute("inputmode");
                    await expect(longitude).not.toHaveAttribute("inputmode");
                    await enter(latitude, "41,0082", entry);
                    await enter(longitude, "17,5", entry);
                    await expect(latitude).toHaveValue("41,0082");
                    await expect(longitude).toHaveValue("17,5");
                    await page
                        .getByRole("button", {
                            name: text.continue,
                            exact: true,
                        })
                        .click();
                    await page.getByLabel(text.capacity).fill("5");
                    await page
                        .getByRole("button", {
                            name: text.continue,
                            exact: true,
                        })
                        .click();
                    await expect(
                        page.getByText(text.review, { exact: true }),
                    ).toBeVisible();
                    await page
                        .getByRole("button", { name: text.submit })
                        .click();
                    await expect.poll(() => sent.length).toBe(1);
                    expect(sent[0]).toMatchObject({
                        latitude: 41.0082,
                        longitude: 17.5,
                    });
                });

                test(`decimal commas ${entry} into a location request are sent as those numbers`, async ({
                    page,
                    baseURL,
                }) => {
                    await mockApi(page);
                    await page.context().addCookies([
                        {
                            name: "pyracast_language",
                            value: language,
                            url: baseURL!,
                        },
                    ]);
                    const sent: unknown[] = [];
                    await page.route(
                        "**/location-change-requests/",
                        (route) => {
                            if (route.request().method() !== "POST")
                                return route.fallback();
                            sent.push(route.request().postDataJSON());
                            return route.fulfill({
                                status: 400,
                                json: errorBody(
                                    "invalid_request",
                                    "Please try again.",
                                ),
                            });
                        },
                    );
                    await page.goto(
                        "/app/w/1/plants/11/settings/?view=location",
                    );
                    await page.getByText(text.requestChange).click();
                    const latitude = page.getByLabel(text.latitude);
                    const longitude = page.getByLabel(text.longitude);
                    await expect(latitude).not.toHaveAttribute("inputmode");
                    await expect(longitude).not.toHaveAttribute("inputmode");
                    await enter(latitude, "41,0082", entry);
                    await enter(longitude, "17,5", entry);
                    await page
                        .getByRole("button", { name: text.submitRequest })
                        .click();
                    await expect.poll(() => sent.length).toBe(1);
                    expect(sent[0]).toMatchObject({
                        latitude: 41.0082,
                        longitude: 17.5,
                    });
                });
            }
        });

test("a coordinate with both a comma and a point stays on the location step and says which numbers it takes", async ({
    page,
}) => {
    await mockApi(page, { empty: true });
    await openWizard(page);
    const latitude = page.getByLabel("Latitude (°)");
    await enter(latitude, "1.234,5", "typed");
    await page.getByRole("button", { name: "Continue", exact: true }).click();
    await expect(latitude).toBeFocused();
    expect(
        await latitude.evaluate(
            (element: HTMLInputElement) => element.validity.patternMismatch,
        ),
    ).toBe(true);
    await expect(latitude).toHaveAttribute(
        "title",
        "A latitude from -90 to 90, such as 41.0082 or 41,0082.",
    );
    await expect(page.getByLabel("DC nameplate capacity (MW)")).toBeHidden();
});
