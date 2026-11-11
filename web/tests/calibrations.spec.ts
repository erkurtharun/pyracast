import { test, expect, type Page } from "./support/test";
import { mockApi, paged } from "./support/workspace";

const run = (id: number) => ({
    id,
    window_start: "2026-01-01",
    window_end: "2026-01-10",
    status: "ready",
    assessment: "accepted",
    factor: 1.1,
    result: {},
});

async function openCalibration(page: Page) {
    await page.goto("/");
    await page.getByRole("button", { name: /Antalya solar park/ }).click();
    await page
        .getByRole("button", { name: "Solar plants", exact: true })
        .click();
    await page.getByRole("button", { name: /East array/ }).click();
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
