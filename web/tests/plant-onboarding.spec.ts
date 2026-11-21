import { test, expect } from "./support/test";
import { mockApi, paged, plant, region } from "./support/workspace";
import { errorBody } from "./support/errors";

test("a temporary site refresh failure preserves an open technical draft", async ({
    page,
}) => {
    await page.clock.install();
    await mockApi(page);
    let fail = false;
    const message = "Site refresh temporarily failed.";
    await page.route("**/sites/", (route) =>
        fail
            ? route.fulfill({
                  status: 503,
                  json: errorBody("busy", message),
              })
            : route.fallback(),
    );
    await page.goto("/app/");
    await page.getByRole("button", { name: /East array/ }).click();
    await page.getByRole("button", { name: "Settings", exact: true }).click();
    await page.getByLabel("DC nameplate capacity (MW)").fill("6.25");
    fail = true;
    await page.clock.fastForward(120_000);
    await expect(page.getByLabel("DC nameplate capacity (MW)")).toHaveValue(
        "6.25",
    );
    await expect(page.locator("main").getByRole("alert")).toContainText(
        message,
    );
    fail = false;
    await page.clock.fastForward(120_000);
    await expect(page.locator("main").getByRole("alert")).toHaveCount(0);
    await expect(page.getByLabel("DC nameplate capacity (MW)")).toHaveValue(
        "6.25",
    );
});

test("an older pending submission cannot replace a saved technical update", async ({
    page,
}) => {
    await page.clock.install();
    await mockApi(page);
    const pending = { ...plant, current_configuration: null };
    let requestStarted!: () => void;
    let release!: () => void;
    const observed = new Promise<void>((resolve) => {
        requestStarted = resolve;
    });
    const gate = new Promise<void>((resolve) => {
        release = resolve;
    });
    let hold = false;
    let serverCapacity = 5;
    let serverRevision = 1;
    await page.route("**/sites/", async (route) => {
        const snapshot = {
            ...pending,
            parameters: { ...plant.parameters, dc_capacity_mw: serverCapacity },
            parameters_revision: serverRevision,
        };
        if (hold) {
            requestStarted();
            await gate;
        }
        await route.fulfill({
            json: paged([{ ...region, status: "pending", plant: snapshot }]),
        });
    });
    await page.route("**/plants/21/", (route) =>
        route.fulfill({
            json: {
                ...pending,
                parameters: { ...plant.parameters, dc_capacity_mw: 6.25 },
                parameters_revision: 2,
            },
        }),
    );
    await page.goto("/app/");
    await page.getByRole("button", { name: /East array/ }).click();
    await page.getByRole("button", { name: "Settings", exact: true }).click();
    await page.getByLabel("DC nameplate capacity (MW)").fill("6.25");
    hold = true;
    await page.clock.fastForward(120_000);
    await observed;
    await page.getByRole("button", { name: "Save configuration" }).click();
    const capacity = page.getByLabel("DC nameplate capacity (MW)");
    const refreshed = page.waitForResponse("**/sites/");
    try {
        await expect(capacity).toHaveValue("6.25");
    } finally {
        release();
    }
    await (await refreshed).finished();
    await page.evaluate(
        () =>
            new Promise<void>((resolve) =>
                requestAnimationFrame(() =>
                    requestAnimationFrame(() => resolve()),
                ),
            ),
    );
    await page.getByRole("button", { name: "Location", exact: true }).click();
    await page
        .getByRole("button", { name: "Technical details", exact: true })
        .click();
    await expect(capacity).toHaveValue("6.25");
    hold = false;
    serverCapacity = 6.25;
    serverRevision = 2;
    const confirmed = page.waitForResponse("**/sites/");
    await page.clock.fastForward(120_000);
    await (await confirmed).finished();
    await page.evaluate(
        () =>
            new Promise<void>((resolve) =>
                requestAnimationFrame(() =>
                    requestAnimationFrame(() => resolve()),
                ),
            ),
    );
    serverCapacity = 7;
    serverRevision = 3;
    const updated = page.waitForResponse("**/sites/");
    await page.clock.fastForward(120_000);
    await (await updated).finished();
    await page.getByRole("button", { name: "Location", exact: true }).click();
    await page
        .getByRole("button", { name: "Technical details", exact: true })
        .click();
    await expect(capacity).toHaveValue("7");
});

test("technical details shown before a reload ends follow the reloaded revision", async ({
    page,
}) => {
    await page.clock.install();
    await mockApi(page);
    let reloaded = false;
    let release!: () => void;
    const gate = new Promise<void>((resolve) => {
        release = resolve;
    });
    let started!: () => void;
    const reloading = new Promise<void>((resolve) => {
        started = resolve;
    });
    await page.route("**/sites/", async (route) => {
        if (reloaded) {
            started();
            await gate;
        }
        await route.fulfill({
            json: paged([
                {
                    ...region,
                    plant: reloaded
                        ? {
                              ...plant,
                              parameters: {
                                  ...plant.parameters,
                                  dc_capacity_mw: 7,
                              },
                              parameters_revision: 2,
                          }
                        : plant,
                },
            ]),
        });
    });
    await page.goto("/app/");
    await page.getByRole("button", { name: /East array/ }).click();
    await page.getByRole("button", { name: "Settings", exact: true }).click();
    const capacity = page.getByLabel("DC nameplate capacity (MW)");
    await expect(capacity).toHaveValue("5");
    reloaded = true;
    const answered = page.waitForResponse("**/sites/");
    await page.clock.fastForward(120_000);
    await reloading;
    release();
    await (await answered).finished();
    await expect(capacity).toHaveValue("7");
});

test("a save from an older revision keeps what the person typed and saves on the next try", async ({
    page,
}) => {
    await mockApi(page);
    const elsewhere = {
        ...plant,
        parameters: { ...plant.parameters, dc_capacity_mw: 6 },
        parameters_revision: 2,
    };
    const bodies: Record<string, unknown>[] = [];
    const message =
        "The technical details changed after you opened them. Check the values and save again.";
    await page.route("**/plants/21/", (route) => {
        if (route.request().method() !== "PUT") return route.fallback();
        bodies.push(route.request().postDataJSON());
        return bodies.length === 1
            ? route.fulfill({
                  status: 409,
                  json: {
                      ...errorBody("plant_parameters_changed", message),
                      current: elsewhere,
                  },
              })
            : route.fulfill({
                  json: {
                      ...elsewhere,
                      parameters: {
                          ...elsewhere.parameters,
                          export_limit_mw: 3,
                      },
                      parameters_revision: 3,
                  },
              });
    });
    await page.goto("/app/");
    await page.getByRole("button", { name: /East array/ }).click();
    await page.getByRole("button", { name: "Settings", exact: true }).click();
    await page.getByLabel("Grid export limit (MW, optional)").fill("3");
    await page.getByRole("button", { name: "Save configuration" }).click();
    await expect(page.locator("main").getByRole("alert")).toContainText(
        message,
    );
    await expect(page.getByLabel("DC nameplate capacity (MW)")).toHaveValue(
        "6",
    );
    await expect(
        page.getByLabel("Grid export limit (MW, optional)"),
    ).toHaveValue("3");
    await page.getByRole("button", { name: "Save configuration" }).click();
    await expect.poll(() => bodies.length).toBe(2);
    expect(bodies[0]).toMatchObject({
        expected_revision: 1,
        dc_capacity_mw: 5,
    });
    expect(bodies[1]).toMatchObject({
        expected_revision: 2,
        dc_capacity_mw: 6,
        export_limit_mw: 3,
    });
    await expect(page.locator("main").getByRole("alert")).toHaveCount(0);
});

for (const section of ["Forecast", "Settings"] as const) {
    test(`administrator approval refreshes a submitted plant while ${section} is open`, async ({
        page,
    }) => {
        await page.clock.install();
        await mockApi(page, { empty: true });
        let submitted = false;
        let approved = false;
        let writes = 0;
        let siteReads = 0;
        const mutations: string[] = [];
        page.on("request", (request) => {
            const path = new URL(request.url()).pathname;
            if (path.startsWith("/api/") && request.method() !== "GET")
                mutations.push(`${request.method()} ${path}`);
        });
        const pendingPlant = {
            ...plant,
            name: "New plant",
            current_configuration: null,
        };
        const currentPlant = () =>
            approved ? { ...plant, name: "New plant" } : pendingPlant;
        await page.route("**/sites/", async (route) => {
            if (route.request().method() === "POST") {
                writes++;
                submitted = true;
                return route.fulfill({
                    json: { ...region, status: "pending", plant: pendingPlant },
                });
            }
            siteReads++;
            await route.fulfill({
                json: paged(
                    submitted
                        ? [
                              {
                                  ...region,
                                  status: approved ? "approved" : "pending",
                                  plant: currentPlant(),
                              },
                          ]
                        : [],
                ),
            });
        });
        await page.route("**/plants/21/forecast/**", (route) =>
            route.fulfill({
                json: {
                    timezone: region.timezone,
                    freshness: approved ? "fresh" : "missing",
                    configuration_id: approved ? 31 : null,
                    result: approved
                        ? { complete: true, energy_mwh: 10, intervals: [] }
                        : null,
                },
            }),
        );
        await page.goto("/app/");
        await page.getByRole("button", { name: "Add a plant" }).click();
        await page.getByLabel("Plant name", { exact: true }).fill("New plant");
        await page.getByLabel("Latitude").fill("36.9");
        await page.getByLabel("Longitude").fill("30.7");
        await page
            .getByRole("button", { name: "Continue", exact: true })
            .click();
        await page.getByLabel("DC nameplate capacity (MW)").fill("5");
        await page
            .getByRole("button", { name: "Continue", exact: true })
            .click();
        await page
            .getByRole("button", { name: "Submit plant for approval" })
            .click();
        await expect(
            page.getByText(/Awaiting administrator approval/),
        ).toBeVisible();
        await expect(
            page.getByRole("list", { name: "Next steps" }),
        ).toBeVisible();
        await expect(
            page.getByRole("heading", { name: "No forecast for this day yet" }),
        ).toHaveCount(0);
        if (section === "Settings") {
            await page
                .getByRole("button", { name: "Settings", exact: true })
                .click();
            await page.getByLabel("DC nameplate capacity (MW)").fill("6.25");
        }
        approved = true;
        await page.clock.fastForward(120_000);
        await expect(page.locator(".plant-identity .tag")).toHaveText(
            "Approved",
        );
        await expect(
            page.getByText(/Awaiting administrator approval/),
        ).toHaveCount(0);
        if (section === "Settings") {
            await expect(
                page.getByRole("button", { name: "Settings", exact: true }),
            ).toHaveAttribute("aria-current", "page");
            await expect(
                page.getByLabel("DC nameplate capacity (MW)"),
            ).toHaveValue("6.25");
            await page
                .getByRole("button", { name: "Forecast", exact: true })
                .click();
        }
        await expect(
            page.getByRole("heading", { name: "Electrical generation" }),
        ).toBeVisible();
        await expect(
            page.getByRole("button", { name: "Production", exact: true }),
        ).toBeEnabled();
        expect(siteReads).toBeGreaterThanOrEqual(2);
        expect(writes).toBe(1);
        expect(mutations).toEqual(["POST /api/tenants/1/sites/"]);
    });
}

test("plant submission errors preserve review and every field for retry", async ({
    page,
}) => {
    await mockApi(page, { empty: true });
    let attempts = 0;
    await page.route("**/sites/", async (route) => {
        if (route.request().method() !== "POST") return route.fallback();
        attempts++;
        expect(route.request().postDataJSON()).toMatchObject({
            name: "West plant",
            dc_capacity_mw: 5,
            inverter_capacity_mw: null,
            export_limit_mw: null,
        });
        if (attempts === 1)
            return route.fulfill({
                status: 400,
                json: errorBody(
                    "location_already_exists",
                    "This location is already in your company's portfolio. Open the existing plant.",
                ),
            });
        return route.fallback();
    });
    await page.goto("/app/");
    await page.getByRole("button", { name: "Add a plant" }).click();
    await page.getByLabel("Plant name", { exact: true }).fill("West plant");
    await page.getByLabel("Latitude").fill("37");
    await page.getByLabel("Longitude").fill("31");
    await page.getByRole("button", { name: "Continue", exact: true }).click();
    await page.getByLabel("DC nameplate capacity (MW)").fill("5");
    await page.getByRole("button", { name: "Continue", exact: true }).click();
    await page
        .getByRole("button", { name: "Submit plant for approval" })
        .click();
    await expect(page.locator("main").getByRole("alert")).toContainText(
        "already in your company's portfolio",
    );
    await expect(
        page.getByRole("heading", { name: "Check your plant details" }),
    ).toBeVisible();
    await page.getByRole("button", { name: "Back", exact: true }).click();
    await expect(page.getByLabel("DC nameplate capacity (MW)")).toHaveValue(
        "5",
    );
    await page.getByRole("button", { name: "Continue", exact: true }).click();
    await page
        .getByRole("button", { name: "Submit plant for approval" })
        .click();
    await expect(
        page.getByRole("heading", { name: "West plant" }),
    ).toBeVisible();
    expect(attempts).toBe(2);
});

test("an existing location without a plant offers complete setup", async ({
    page,
}) => {
    await mockApi(page);
    let created = false;
    await page.route("**/sites/", (route) =>
        route.fulfill({
            json: paged([{ ...region, plant: created ? plant : null }]),
        }),
    );
    await page.route("**/regions/11/plants/", (route) => {
        expect(route.request().method()).toBe("POST");
        expect(route.request().postDataJSON()).toMatchObject({
            name: region.name,
            dc_capacity_mw: 5,
        });
        created = true;
        return route.fulfill({ json: plant });
    });
    await page.goto("/app/");
    await page.getByRole("button", { name: /Complete setup/ }).click();
    await expect(
        page.getByRole("heading", { name: "Complete plant setup" }),
    ).toBeVisible();
    await page.getByLabel("DC nameplate capacity (MW)").fill("5");
    await page.getByRole("button", { name: "Continue", exact: true }).click();
    await page
        .getByRole("button", { name: "Submit plant for approval" })
        .click();
    await expect(page.getByRole("heading", { name: plant.name })).toBeVisible();
});

test("portfolio filters span all pages and a plant section survives refresh", async ({
    page,
}) => {
    await mockApi(page);
    await page.route("**/sites/**", (route) => {
        const second = new URL(route.request().url()).searchParams.has("page");
        return route.fulfill({
            json: {
                ...paged([
                    {
                        ...region,
                        id: second ? 12 : 11,
                        name: second ? "West location" : region.name,
                        status: second ? "pending" : "approved",
                        plant: {
                            ...plant,
                            name: second ? "West plant" : plant.name,
                        },
                    },
                ]),
                count: 2,
                next: second ? null : "/api/tenants/1/sites/?page=2",
            },
        });
    });
    await page.goto("/app/");
    await expect(page.locator(".region-card")).toHaveCount(2);
    await page.getByLabel("Approval status").selectOption("pending");
    await expect(page.locator(".region-card")).toHaveCount(1);
    await expect(page.locator(".region-card")).toContainText("West plant");
    await page.getByLabel("Approval status").selectOption("all");
    await page.getByLabel("Search plants").fill("East");
    await expect(page.locator(".region-card")).toHaveCount(1);
    await page.locator(".region-card").click();
    await page.getByRole("button", { name: "Settings", exact: true }).click();
    await expect(page).toHaveURL("/app/w/1/plants/11/settings/");
    await page.reload();
    await expect(
        page.getByRole("button", { name: "Settings", exact: true }),
    ).toHaveAttribute("aria-current", "page");
    await expect(page.getByLabel("DC nameplate capacity (MW)")).toHaveValue(
        "5",
    );
});
