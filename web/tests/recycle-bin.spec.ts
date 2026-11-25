import { test, expect, type Page } from "./support/test";
import { errorBody } from "./support/errors";
import { mockApi, paged, plant, region, tenantRow } from "./support/workspace";

const binRow = {
    id: region.id,
    name: region.name,
    plant_name: plant.name,
    latitude: region.latitude,
    longitude: region.longitude,
    deleted_at: "2026-11-10T12:00:00Z",
    purge_after: "2026-12-10T12:00:00Z",
    restore_blocker: null,
};

async function bin(page: Page, state: { deleted: boolean }) {
    await page.route("**/api/tenants/1/**", async (route) => {
        const path = new URL(route.request().url()).pathname;
        const method = route.request().method();
        if (path === "/api/tenants/1/regions/11/" && method === "DELETE") {
            state.deleted = true;
            await route.fulfill({ status: 204 });
        } else if (path === "/api/tenants/1/regions/11/restore/") {
            state.deleted = false;
            await route.fulfill({ json: region });
        } else if (path === "/api/tenants/1/sites/" && method === "GET")
            await route.fulfill({
                json: paged(state.deleted ? [] : [{ ...region, plant }]),
            });
        else if (path === "/api/tenants/1/trash/regions/")
            await route.fulfill({ json: paged(state.deleted ? [binRow] : []) });
        else await route.fallback();
    });
}

test("bin rows are titled with the plant name and a restored plant opens from the notice", async ({
    page,
}) => {
    await mockApi(page);
    await bin(page, { deleted: true });
    await page.goto("/app/w/1/trash/");
    const row = page.getByRole("listitem").filter({ hasText: plant.name });
    await expect(row.getByRole("heading", { name: plant.name })).toBeVisible();
    await expect(row).not.toContainText(region.name);
    await row.getByRole("button", { name: `Restore ${plant.name}` }).click();
    const notice = page.getByRole("status").filter({
        hasText: `${plant.name} restored.`,
    });
    await expect(notice).toBeVisible();
    await notice.getByRole("button", { name: "Open plant" }).click();
    await expect(page).toHaveURL("/app/w/1/plants/11/forecast/");
});

test("an empty recycle bin says so in one sentence", async ({ page }) => {
    await mockApi(page);
    await bin(page, { deleted: false });
    await page.goto("/app/w/1/trash/");
    await expect(
        page.getByText("No deleted plants.", { exact: true }),
    ).toBeVisible();
    await expect(page.getByText("Nothing to restore.")).toHaveCount(0);
});

const busy = errorBody(
    "busy",
    "This could not be saved right now. Try again in a moment.",
);

test("a failed restore moves the focus to its reason", async ({ page }) => {
    await mockApi(page);
    await bin(page, { deleted: true });
    await page.route("**/api/tenants/1/regions/11/restore/", (route) =>
        route.fulfill({ status: 503, json: busy }),
    );
    await page.goto("/app/w/1/trash/");
    await page
        .getByRole("button", { name: `Restore ${plant.name}` })
        .press("Enter");
    await expect(
        page
            .getByRole("alert")
            .filter({ hasText: "This could not be saved right now." }),
    ).toBeFocused();
});

test("a restored plant's row leaves the focus on the notice that opens it", async ({
    page,
}) => {
    await mockApi(page);
    await bin(page, { deleted: true });
    await page.goto("/app/w/1/trash/");
    await page
        .getByRole("button", { name: `Restore ${plant.name}` })
        .press("Enter");
    const notice = page.getByRole("status").filter({
        hasText: `${plant.name} restored.`,
    });
    await expect(notice).toBeFocused();
    await expect(
        notice.getByRole("button", { name: "Open plant" }),
    ).toBeVisible();
});

test("a failed move to the recycle bin moves the focus to its reason", async ({
    page,
}) => {
    await mockApi(page);
    await page.route("**/api/tenants/1/regions/11/", (route) =>
        route.request().method() === "DELETE"
            ? route.fulfill({ status: 503, json: busy })
            : route.fallback(),
    );
    await page.goto("/app/w/1/plants/11/settings/");
    await page
        .getByRole("button", { name: "Delete plant", exact: true })
        .press("Enter");
    const confirmation = page.getByRole("region", {
        name: "Delete plant",
    });
    await confirmation.getByRole("button", { name: "Delete" }).press("Enter");
    await expect(confirmation.getByRole("alert")).toBeFocused();
});

test("cancelling the move to the recycle bin returns the focus to Delete plant", async ({
    page,
}) => {
    await mockApi(page);
    await page.goto("/app/w/1/plants/11/settings/");
    const deletePlant = page.getByRole("button", {
        name: "Delete plant",
        exact: true,
    });
    await deletePlant.press("Enter");
    await page
        .getByRole("region", { name: "Delete plant" })
        .getByRole("button", { name: "Cancel" })
        .press("Enter");
    await expect(deletePlant).toBeFocused();
});

test("a failed delete keeps the confirmation open with the reason and lets the person retry", async ({
    page,
}) => {
    await mockApi(page);
    let attempts = 0;
    await page.route("**/api/tenants/1/regions/11/", (route) => {
        if (route.request().method() !== "DELETE") return route.fallback();
        attempts++;
        return route.fulfill({
            status: 503,
            json: errorBody(
                "busy",
                "This could not be saved right now. Try again in a moment.",
            ),
        });
    });
    await page.goto("/app/w/1/plants/11/settings/");
    await page
        .getByRole("button", { name: "Delete plant", exact: true })
        .click();
    const confirmation = page.getByRole("region", {
        name: "Delete plant",
    });
    const move = confirmation.getByRole("button", {
        name: "Delete",
    });
    await move.click();
    await expect(confirmation.getByRole("alert")).toContainText(
        "This could not be saved right now.",
    );
    await expect(move).toBeEnabled();
    await move.click();
    await expect.poll(() => attempts).toBe(2);
});

test("undo after a delete restores the plant on the portfolio", async ({
    page,
}) => {
    await mockApi(page);
    const state = { deleted: false };
    await bin(page, state);
    await page.goto("/app/w/1/plants/11/settings/");
    await page
        .getByRole("button", { name: "Delete plant", exact: true })
        .click();
    await page.getByRole("button", { name: "Delete", exact: true }).click();
    const notice = page.getByRole("status").filter({
        hasText: `${plant.name} is in the recycle bin.`,
    });
    await expect(notice).toBeVisible();
    await expect(page.locator(".region-card")).toHaveCount(0);
    await page.getByRole("button", { name: "Undo" }).click();
    await expect(page.locator(".region-card")).toHaveCount(1);
    await expect(
        page.getByRole("status").filter({ hasText: `${plant.name} restored.` }),
    ).toBeVisible();
    expect(state.deleted).toBe(false);
});

async function deleteFromSettings(page: Page) {
    await page.goto("/app/w/1/plants/11/settings/");
    await page
        .getByRole("button", { name: "Delete plant", exact: true })
        .click();
    await page.getByRole("button", { name: "Delete", exact: true }).click();
}

const deletionNotice = (page: Page) =>
    page.getByRole("status").filter({
        hasText: `${plant.name} is in the recycle bin.`,
    });

test("a failed undo shows its alert outside the deletion's status", async ({
    page,
}) => {
    await mockApi(page);
    await bin(page, { deleted: false });
    await page.route("**/api/tenants/1/regions/11/restore/", (route) =>
        route.fulfill({
            status: 503,
            json: errorBody(
                "busy",
                "This could not be saved right now. Try again in a moment.",
            ),
        }),
    );
    await deleteFromSettings(page);
    await expect(deletionNotice(page)).toBeVisible();
    await page.getByRole("button", { name: "Undo" }).click();
    await expect(
        page
            .getByRole("alert")
            .filter({ hasText: "This could not be saved right now." }),
    ).toBeVisible();
    await expect(page.getByRole("status").getByRole("alert")).toHaveCount(0);
    await expect(deletionNotice(page)).toBeVisible();
});

test("a plant restored through the add-plant wizard no longer offers its undo", async ({
    page,
}) => {
    await mockApi(page);
    await bin(page, { deleted: false });
    await deleteFromSettings(page);
    await expect(deletionNotice(page)).toBeVisible();
    await page.getByRole("button", { name: "Add plant", exact: true }).click();
    await page.getByLabel("Plant name", { exact: true }).fill("Again");
    await page.getByLabel("Latitude (°)").fill(String(region.latitude));
    await page.getByLabel("Longitude (°)").fill(String(region.longitude));
    await page.getByRole("button", { name: "Continue", exact: true }).click();
    await page.getByRole("button", { name: `Restore ${plant.name}` }).click();
    await expect(page).toHaveURL("/app/w/1/plants/11/forecast/");
    await page.getByRole("button", { name: /All plants/ }).click();
    await expect(page.locator(".region-card")).toHaveCount(1);
    await expect(deletionNotice(page)).toHaveCount(0);
});

test("the undo notice is gone after leaving the workspace and coming back", async ({
    page,
}) => {
    await mockApi(page);
    await bin(page, { deleted: false });
    await page.route("**/api/tenants/", (route) =>
        route.fulfill({
            json: paged([
                tenantRow({ id: 1, name: "Sunfield Energy", role: "admin" }),
                tenantRow({ id: 2, name: "Second Energy", role: "admin" }),
            ]),
        }),
    );
    await page.route("**/api/tenants/2/sites/", (route) =>
        route.fulfill({ json: paged([]) }),
    );
    await deleteFromSettings(page);
    await expect(deletionNotice(page)).toBeVisible();
    await page
        .getByRole("combobox", { name: "Workspace", exact: true })
        .selectOption("2");
    await expect(page).toHaveURL("/app/w/2/");
    await page
        .getByRole("combobox", { name: "Workspace", exact: true })
        .selectOption("1");
    await expect(page).toHaveURL("/app/w/1/");
    await expect(
        page.getByRole("button", { name: "Add plant", exact: true }),
    ).toBeVisible();
    await expect(deletionNotice(page)).toHaveCount(0);
});
