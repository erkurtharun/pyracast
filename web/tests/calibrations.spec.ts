import { test, expect, type Page } from "./support/test";
import { mockApi, paged } from "./support/workspace";

const run = (id: number) => ({
    id,
    configuration_id: 31,
    window_start: "2026-01-01",
    window_end: "2026-01-10",
    status: "ready",
    assessment: "accepted",
    factor: 1.1,
    result: {},
});

async function openCalibration(page: Page) {
    await page.goto("/");
    await page
        .getByRole("button", { name: /East array|Antalya solar park/ })
        .click();
    await page
        .getByRole("button", { name: "Calibration", exact: true })
        .click();
    await expect(
        page.getByRole("heading", { name: "Calibration history" }),
    ).toBeVisible();
}

for (const staleError of [false, true]) {
    test(`latest calibration details survive an older ${staleError ? "error" : "response"}`, async ({
        page,
    }) => {
        await mockApi(page);
        let release!: () => void;
        let started!: () => void;
        const barrier = new Promise<void>((resolve) => {
            release = resolve;
        });
        const observed = new Promise<void>((resolve) => {
            started = resolve;
        });
        await page.route("**/plants/21/calibrations/", (route) =>
            route.fulfill({ json: paged([run(40), run(41)]) }),
        );
        await page.route("**/plants/21/calibrations/40/", async (route) => {
            started();
            await barrier;
            await route.fulfill(
                staleError
                    ? { status: 503, json: { detail: "Old detail failed" } }
                    : { json: run(40) },
            );
        });
        await page.route("**/plants/21/calibrations/41/", (route) =>
            route.fulfill({ json: run(41) }),
        );
        await openCalibration(page);
        await page
            .getByRole("row", { name: /#40/ })
            .getByRole("button", { name: "View details" })
            .click();
        await observed;
        await page
            .getByRole("row", { name: /#41/ })
            .getByRole("button", { name: "View details" })
            .click();
        await expect(
            page.getByRole("heading", { name: "Calibration #41" }),
        ).toBeVisible();
        const response = page.waitForResponse("**/plants/21/calibrations/40/");
        release();
        await (await response).finished();
        await page.evaluate(
            () =>
                new Promise<void>((resolve) =>
                    requestAnimationFrame(() =>
                        requestAnimationFrame(() => resolve()),
                    ),
                ),
        );
        await expect(
            page.getByRole("heading", { name: "Calibration #41" }),
        ).toBeVisible();
        await expect(page.getByText("Old detail failed")).toHaveCount(0);
    });
}

test("refresh during activation still reloads the completed selection", async ({
    page,
}) => {
    await mockApi(page);
    let release!: () => void;
    let started!: () => void;
    const barrier = new Promise<void>((resolve) => {
        release = resolve;
    });
    const observed = new Promise<void>((resolve) => {
        started = resolve;
    });
    let activated = false;
    await page.route("**/plants/21/calibrations/", (route) =>
        route.fulfill({
            json: paged(activated ? [run(40), run(41)] : [run(40)]),
        }),
    );
    await page.route(
        "**/plants/21/calibrations/40/activate/",
        async (route) => {
            started();
            await barrier;
            activated = true;
            await route.fulfill({ json: {} });
        },
    );
    await openCalibration(page);
    await page.getByRole("button", { name: "Activate", exact: true }).click();
    await observed;
    const refreshed = page.waitForResponse("**/plants/21/calibrations/");
    await page.getByRole("button", { name: "Refresh", exact: true }).click();
    await refreshed;
    release();
    await expect(page.getByRole("row", { name: /#41/ })).toBeVisible();
});

test("detail and activation errors remain independent", async ({ page }) => {
    await mockApi(page);
    await page.route("**/plants/21/calibrations/", (route) =>
        route.fulfill({ json: paged([run(40)]) }),
    );
    await page.route("**/plants/21/calibrations/40/activate/", (route) =>
        route.fulfill({ status: 503, json: { detail: "Activation failed" } }),
    );
    await page.route("**/plants/21/calibrations/40/", (route) =>
        route.fulfill({ status: 503, json: { detail: "Detail failed" } }),
    );
    await openCalibration(page);
    await page.getByRole("button", { name: "Activate", exact: true }).click();
    await expect(page.getByText("Activation failed")).toBeVisible();
    await page.getByRole("button", { name: "View details" }).click();
    await expect(page.getByText("Detail failed")).toBeVisible();
    await expect(page.getByText("Activation failed")).toBeVisible();
});

test("automatic calibration can be enabled before a fit and disabled without resetting", async ({
    page,
}) => {
    await mockApi(page);
    let enabled = false;
    const changes: boolean[] = [];
    await page.route("**/plants/21/calibrations/", (route) =>
        route.fulfill({ json: paged(enabled ? [run(40)] : []) }),
    );
    await page.route("**/plants/21/calibration/automation/", (route) => {
        if (route.request().method() === "PATCH") {
            expect(route.request().headers()["x-csrftoken"]).toBe(
                "signed-token",
            );
            enabled = route.request().postDataJSON().enabled;
            changes.push(enabled);
        }
        return route.fulfill({
            json: {
                enabled,
                authorized: enabled,
                configuration_id: enabled ? 31 : null,
                next_check_at: enabled ? "2026-11-12T03:00:00Z" : null,
                last_attempt: enabled
                    ? {
                          calibration_id: 40,
                          status: "skipped",
                          reason: "awaiting_new_holdout_days",
                          created_at: "2026-11-11T03:00:00Z",
                      }
                    : null,
            },
        });
    });
    await openCalibration(page);
    await page
        .getByRole("button", { name: "Enable automatic calibration" })
        .click();
    await expect(
        page.getByRole("button", { name: "Disable automatic calibration" }),
    ).toBeVisible();
    await expect(page.getByRole("row", { name: /#40/ })).toBeVisible();
    await expect(
        page.getByText("Waiting for seven new held-out days."),
    ).toBeVisible();
    await page
        .getByRole("button", { name: "Disable automatic calibration" })
        .click();
    await expect(
        page.getByRole("button", { name: "Enable automatic calibration" }),
    ).toBeVisible();
    expect(changes).toEqual([true, false]);
});

test("automatic status reloads after manual activation and reset", async ({
    page,
}) => {
    await mockApi(page);
    let enabled = false;
    await page.route("**/plants/21/calibrations/", (route) =>
        route.fulfill({ json: paged([run(40)]) }),
    );
    await page.route("**/plants/21/calibrations/40/activate/", (route) => {
        enabled = true;
        return route.fulfill({ json: {} });
    });
    await page.route("**/plants/21/calibration/reset/", (route) => {
        enabled = false;
        return route.fulfill({ json: {} });
    });
    await page.route("**/plants/21/calibration/automation/", (route) =>
        route.fulfill({
            json: {
                enabled,
                authorized: enabled,
                configuration_id: enabled ? 31 : null,
                next_check_at: null,
                last_attempt: null,
            },
        }),
    );
    await openCalibration(page);
    await page.getByRole("button", { name: "Activate", exact: true }).click();
    await expect(
        page.getByRole("button", { name: "Disable automatic calibration" }),
    ).toBeVisible();
    await page.getByRole("button", { name: "Reset to baseline" }).click();
    await expect(
        page.getByRole("button", { name: "Enable automatic calibration" }),
    ).toBeVisible();
});

test("an older automation response cannot undo an enabled setting", async ({
    page,
}) => {
    await mockApi(page);
    let enabled = false;
    let delayNext = false;
    let release!: () => void;
    let started!: () => void;
    const barrier = new Promise<void>((resolve) => {
        release = resolve;
    });
    const observed = new Promise<void>((resolve) => {
        started = resolve;
    });
    await page.route("**/plants/21/calibration/automation/", async (route) => {
        if (route.request().method() === "PATCH")
            enabled = route.request().postDataJSON().enabled;
        else if (delayNext) {
            delayNext = false;
            started();
            await barrier;
            return route.fulfill({
                json: {
                    enabled: false,
                    authorized: false,
                    configuration_id: null,
                    next_check_at: null,
                    last_attempt: null,
                },
            });
        }
        return route.fulfill({
            json: {
                enabled,
                authorized: enabled,
                configuration_id: enabled ? 31 : null,
                next_check_at: null,
                last_attempt: enabled
                    ? {
                          calibration_id: 40,
                          status: "skipped",
                          reason: "insufficient_current_source_validation",
                          created_at: "2026-11-11T03:00:00Z",
                      }
                    : null,
            },
        });
    });
    await openCalibration(page);
    await expect(
        page.getByRole("button", { name: "Enable automatic calibration" }),
    ).toBeVisible();
    delayNext = true;
    await page.getByRole("button", { name: "Refresh", exact: true }).click();
    await observed;
    await page
        .getByRole("button", { name: "Enable automatic calibration" })
        .click();
    await expect(
        page.getByRole("button", { name: "Disable automatic calibration" }),
    ).toBeVisible();
    await expect(
        page.getByText(
            /Waiting for 30 matched days from the current live reference source/,
        ),
    ).toBeVisible();
    const oldResponse = page.waitForResponse(
        "**/plants/21/calibration/automation/",
    );
    release();
    await (await oldResponse).finished();
    await expect(
        page.getByRole("button", { name: "Disable automatic calibration" }),
    ).toBeVisible();
    await expect(
        page.getByText(
            /Waiting for 30 matched days from the current live reference source/,
        ),
    ).toBeVisible();
});

test("ready calibrations from previous configurations remain visible but cannot be activated", async ({
    page,
}) => {
    await mockApi(page);
    await page.route("**/plants/21/calibrations/", (route) =>
        route.fulfill({
            json: paged([{ ...run(40), configuration_id: 30 }, run(41)]),
        }),
    );
    await openCalibration(page);
    const previous = page.getByRole("row", { name: /#40/ });
    await expect(previous.getByText("Previous configuration")).toBeVisible();
    await expect(
        previous.getByText("Only the current configuration can be activated."),
    ).toBeVisible();
    await expect(
        previous.getByRole("button", { name: "Activate", exact: true }),
    ).toHaveCount(0);
    await expect(
        page
            .getByRole("row", { name: /#41/ })
            .getByRole("button", { name: "Activate", exact: true }),
    ).toBeEnabled();
});
