import { readFile } from "node:fs/promises";
import Papa from "papaparse";
import { test, expect, type Page } from "./support/test";
import { answer, mockApi, paged, type Mock } from "./support/workspace";
import { errorBody } from "./support/errors";
import type { components } from "../src/lib/schema";

type Schemas = components["schemas"];

// Whether a run can be requested again depends on its window ending before the plant's today.
test.beforeEach(async ({ page }) => {
    await page.clock.install({ time: new Date("2026-01-12T12:00:00Z") });
});

const run = (id: number, fields: Mock<Schemas["CalibrationOutput"]> = {}) =>
    answer("CalibrationOutput", {
        id,
        configuration_id: 31,
        binding_id: 7,
        requested_by_id: 1,
        window_start: "2026-01-01",
        window_end: "2026-01-10",
        cutoff_at: "2026-01-11T00:00:00Z",
        created_at: "2026-01-11T01:00:00Z",
        completed_at: "2026-01-11T02:00:00Z",
        status: "ready",
        reason: null,
        selection_state: "candidate",
        assessment: "evaluated",
        factor: 1.1,
        result: {},
        dataset_sha256: "",
        activation_blocker: null,
        dataset_manifest: {},
        ...fields,
    });

const automation = (
    fields: Mock<Schemas["CalibrationAutomationOutput"]> = {},
) =>
    answer("CalibrationAutomationOutput", {
        enabled: false,
        authorized: false,
        configuration_id: 31,
        configuration_current: true,
        next_check_at: null,
        disabled_cause: null,
        current_calibration: null,
        last_attempt: null,
        ...fields,
    });

const inForce = (calibration_id: number | null) => ({
    activation_id: 3,
    calibration_id,
    factor: calibration_id === null ? 1 : 1.1,
    actor_id: 1,
    origin: "manual" as const,
    created_at: "2026-01-12T09:00:00Z",
    effective: true,
});

async function routeAutomation(
    page: Page,
    fields: Mock<Schemas["CalibrationAutomationOutput"]>,
) {
    await page.route("**/plants/21/calibration/automation/", (route) =>
        route.fulfill({ json: automation(fields) }),
    );
}

async function routeRuns(page: Page, runs: () => ReturnType<typeof run>[]) {
    await page.route("**/plants/21/calibrations/", (route) =>
        route.fulfill({ json: paged(runs()) }),
    );
    await page.route(/\/plants\/21\/calibrations\/\d+\/$/, (route) => {
        const id = Number(
            new URL(route.request().url()).pathname.split("/").at(-2),
        );
        return route.fulfill({ json: runs().find((row) => row.id === id) });
    });
}

async function reviewActivation(page: Page, id: number) {
    await page
        .getByRole("row", { name: new RegExp(`#${id}`) })
        .getByRole("button", { name: "Review activation" })
        .click();
}

async function activate(page: Page, id: number) {
    await reviewActivation(page, id);
    await page
        .getByRole("button", { name: "Activate calibration", exact: true })
        .click();
}

async function openCalibration(page: Page) {
    await page.goto("/app/");
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
                    ? {
                          status: 503,
                          json: errorBody("busy", "Old detail failed"),
                      }
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
    await routeRuns(page, () => (activated ? [run(40), run(41)] : [run(40)]));
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
    await activate(page, 40);
    await observed;
    const refreshed = page.waitForResponse("**/plants/21/calibrations/");
    await page.getByRole("button", { name: "Refresh", exact: true }).click();
    await refreshed;
    release();
    await expect(page.getByRole("row", { name: /#41/ })).toBeVisible();
});

const selectionChanges = [
    {
        kind: "an activation",
        before: "candidate",
        after: "active",
        tag: "In use",
        exports: 2,
        endpoint: "**/plants/21/calibrations/40/activate/",
        act: (page: Page) => activate(page, 40),
    },
    {
        kind: "a reset",
        before: "active",
        after: "candidate",
        tag: "Calculated",
        exports: 1,
        endpoint: "**/plants/21/calibration/reset/",
        act: async (page: Page) => {
            await page
                .getByRole("button", { name: "Reset to baseline" })
                .click();
            await page
                .getByRole("button", { name: "Reset to baseline" })
                .click();
        },
    },
] as const;

for (const change of selectionChanges)
    test(`the calibration export waits for the runs reloaded after ${change.kind}`, async ({
        page,
    }) => {
        await mockApi(page);
        let changed = false;
        let holding = false;
        let release!: () => void;
        const gate = new Promise<void>((resolve) => {
            release = resolve;
        });
        let reloading!: () => void;
        const reload = new Promise<void>((resolve) => {
            reloading = resolve;
        });
        const current = () =>
            run(40, {
                selection_state: changed ? change.after : change.before,
            });
        await page.route("**/plants/21/calibrations/", async (route) => {
            if (holding) {
                reloading();
                await gate;
            }
            return route.fulfill({ json: paged([current()]) });
        });
        await page.route("**/plants/21/calibrations/40/", (route) =>
            route.fulfill({ json: current() }),
        );
        await page.route(change.endpoint, (route) => {
            changed = true;
            holding = true;
            return route.fulfill({ json: {} });
        });
        await page.route("**/plants/21/calibration/automation/", (route) =>
            route.fulfill({
                json: automation({
                    current_calibration: inForce(
                        (change.before === "active") !== changed ? 40 : null,
                    ),
                }),
            }),
        );
        await openCalibration(page);
        await change.act(page);
        await reload;
        await expect(
            page.getByText(
                "Calibration selection updated. Refresh the forecast to see the current result.",
            ),
        ).toBeVisible();
        const working = page.getByRole("button", { name: "Working…" });
        await expect(working).toHaveCount(change.exports);
        for (const button of await working.all())
            await expect(button).toBeDisabled();
        await expect(
            page.getByRole("button", { name: /^Download calibration/ }),
        ).toHaveCount(0);
        holding = false;
        release();
        await expect(page.getByRole("row", { name: /#40/ })).toContainText(
            change.tag,
        );
        const download = page.waitForEvent("download");
        await page
            .getByRole("button", { name: "Download calibration CSV" })
            .click();
        const records = Papa.parse<Record<string, string>>(
            await readFile(await (await download).path(), "utf8"),
            { header: true, skipEmptyLines: true },
        ).data;
        expect(records.map((record) => record.selection_state)).toEqual([
            change.after,
        ]);
    });

test("the calibration exports wait for the runs reloaded after a request", async ({
    page,
}) => {
    await mockApi(page);
    let requested = false;
    let holding = false;
    let release!: () => void;
    const gate = new Promise<void>((resolve) => {
        release = resolve;
    });
    let reloading!: () => void;
    const reload = new Promise<void>((resolve) => {
        reloading = resolve;
    });
    const blocked = run(40, { activation_blocker: "incumbent_changed" });
    const current = () =>
        requested
            ? [
                  run(41, {
                      status: "pending",
                      selection_state: "not_ready",
                      factor: null,
                  }),
                  blocked,
              ]
            : [blocked];
    await page.route("**/plants/21/calibrations/", async (route) => {
        if (route.request().method() === "POST") {
            requested = true;
            holding = true;
            return route.fulfill({ status: 202, json: current()[0] });
        }
        if (holding) {
            reloading();
            await gate;
        }
        return route.fulfill({ json: paged(current()) });
    });
    await page.route("**/plants/21/calibrations/40/", (route) =>
        route.fulfill({ json: blocked }),
    );
    await openCalibration(page);
    await page
        .getByRole("row", { name: /#40/ })
        .getByRole("button", { name: "View details" })
        .click();
    await page
        .getByRole("button", {
            name: "Request a new calibration for 2026-01-01 – 2026-01-10",
        })
        .click();
    await reload;
    await expect(
        page.getByText(
            "A new calibration was requested. It appears in the list when it starts.",
        ),
    ).toBeVisible();
    const working = page.getByRole("button", { name: "Working…" });
    await expect(working).toHaveCount(2);
    for (const button of await working.all())
        await expect(button).toBeDisabled();
    await expect(
        page.getByRole("button", { name: /^Download calibration/ }),
    ).toHaveCount(0);
    holding = false;
    release();
    await expect(page.getByRole("row", { name: /#41/ })).toContainText(
        "Queued",
    );
    const download = page.waitForEvent("download");
    await page
        .getByRole("button", { name: "Download calibration CSV" })
        .click();
    const records = Papa.parse<Record<string, string>>(
        await readFile(await (await download).path(), "utf8"),
        { header: true, skipEmptyLines: true },
    ).data;
    expect(records.map((record) => record.id)).toEqual(["41", "40"]);
});

test("after a reset the opened run shows and exports its reloaded selection", async ({
    page,
}) => {
    await mockApi(page);
    let reset = false;
    const current = () =>
        run(40, {
            selection_state: reset ? "candidate" : "active",
            activation_blocker: reset ? "incumbent_changed" : null,
        });
    await routeRuns(page, () => [current()]);
    await page.route("**/plants/21/calibration/reset/", (route) => {
        reset = true;
        return route.fulfill({ json: {} });
    });
    await page.route("**/plants/21/calibration/automation/", (route) =>
        route.fulfill({
            json: automation({
                current_calibration: inForce(reset ? null : 40),
            }),
        }),
    );
    await openCalibration(page);
    await page
        .getByRole("row", { name: /#40/ })
        .getByRole("button", { name: "View details" })
        .click();
    const detail = page.locator(".calibration-detail");
    await expect(detail.locator(".tags")).toContainText("In use");
    await page.getByRole("button", { name: "Reset to baseline" }).click();
    await page.getByRole("button", { name: "Reset to baseline" }).click();
    await expect(page.getByRole("row", { name: /#40/ })).toContainText(
        "Calculated",
    );
    await expect(detail.locator(".tags")).toContainText("Calculated");
    await expect(
        detail.getByText(
            "This calibration cannot be activated: the calibration in use changed.",
        ),
    ).toBeVisible();
    const download = page.waitForEvent("download");
    await detail
        .getByRole("button", { name: "Download calibration details CSV" })
        .click();
    const [record] = Papa.parse<Record<string, string>>(
        await readFile(await (await download).path(), "utf8"),
        { header: true, skipEmptyLines: true },
    ).data;
    expect(record).toMatchObject({
        selection_state: "candidate",
        activation_blocker: "incumbent_changed",
    });
});

const resetOffers = [
    {
        name: "a calibration in force",
        fields: { current_calibration: inForce(40) },
        offered: true,
    },
    {
        name: "automatic calibration on",
        fields: {
            enabled: true,
            authorized: true,
            current_calibration: inForce(null),
        },
        offered: true,
    },
    {
        name: "the baseline in force and automation off",
        fields: { current_calibration: inForce(null) },
        offered: false,
    },
    {
        name: "nothing selected yet",
        fields: { current_calibration: null },
        offered: false,
    },
    {
        name: "a configuration that is not current",
        fields: {
            enabled: true,
            authorized: true,
            configuration_current: false,
            current_calibration: inForce(40),
        },
        offered: false,
    },
] as const;

for (const offer of resetOffers)
    test(`a reset is ${offer.offered ? "offered" : "not offered"} with ${offer.name}`, async ({
        page,
    }) => {
        await mockApi(page);
        await routeRuns(page, () => [run(40)]);
        await routeAutomation(page, offer.fields);
        await openCalibration(page);
        await expect(
            page
                .getByRole("region", { name: "Calibration in force" })
                .getByRole("heading", { name: "Calibration in force" }),
        ).toBeVisible();
        await expect(
            page.getByRole("button", { name: "Reset to baseline" }),
        ).toHaveCount(offer.offered ? 1 : 0);
    });

test("a reset refused because the configuration changed meanwhile says so", async ({
    page,
}) => {
    await mockApi(page);
    await routeRuns(page, () => [run(40, { selection_state: "active" })]);
    await routeAutomation(page, { current_calibration: inForce(40) });
    await page.route("**/plants/21/calibration/reset/", (route) =>
        route.fulfill({
            status: 409,
            json: errorBody(
                "configuration_changed",
                "The plant configuration is no longer current.",
            ),
        }),
    );
    await openCalibration(page);
    await page.getByRole("button", { name: "Reset to baseline" }).click();
    await page.getByRole("button", { name: "Reset to baseline" }).click();
    await expect(
        page.getByText("The plant configuration is no longer current.", {
            exact: true,
        }),
    ).toBeVisible();
    await expect(
        page.getByText(
            "Calibration selection updated. Refresh the forecast to see the current result.",
        ),
    ).toHaveCount(0);
});

for (const failing of ["activation", "reload"] as const)
    test(`the calibration export comes back when the ${failing} fails`, async ({
        page,
    }) => {
        await mockApi(page);
        let activated = false;
        await page.route("**/plants/21/calibrations/", (route) =>
            activated && failing === "reload"
                ? route.fulfill({
                      status: 503,
                      json: errorBody("busy", "Runs reload failed"),
                  })
                : route.fulfill({ json: paged([run(40)]) }),
        );
        await page.route("**/plants/21/calibrations/40/", (route) =>
            route.fulfill({ json: run(40) }),
        );
        await page.route("**/plants/21/calibrations/40/activate/", (route) => {
            activated = true;
            return failing === "activation"
                ? route.fulfill({
                      status: 503,
                      json: errorBody("busy", "Activation failed"),
                  })
                : route.fulfill({ json: {} });
        });
        await openCalibration(page);
        await activate(page, 40);
        await expect(
            page.getByText(
                failing === "activation"
                    ? "Activation failed"
                    : "Runs reload failed",
            ),
        ).toBeVisible();
        await expect(
            page.getByRole("button", { name: "Download calibration CSV" }),
        ).toBeEnabled();
        await expect(
            page.getByRole("button", { name: "Working…" }),
        ).toHaveCount(0);
    });

test("superseded runs are labelled in Turkish", async ({ page, baseURL }) => {
    await mockApi(page);
    await page
        .context()
        .addCookies([
            { name: "pyracast_language", value: "tr", url: baseURL! },
        ]);
    await page.route("**/plants/21/calibrations/", (route) =>
        route.fulfill({ json: paged([{ ...run(40), status: "superseded" }]) }),
    );
    await page.goto("/app/");
    await page
        .getByRole("button", { name: /East array|Antalya solar park/ })
        .click();
    await page
        .getByRole("button", { name: "Kalibrasyon", exact: true })
        .click();
    const row = page.getByRole("row", { name: /#40/ });
    await expect(row).toContainText("Eskidi");
    await expect(row).not.toContainText("superseded");
});

test("refresh clears a finished action notice", async ({ page }) => {
    await mockApi(page);
    await routeRuns(page, () => [run(40)]);
    await page.route("**/plants/21/calibrations/40/activate/", (route) =>
        route.fulfill({ json: {} }),
    );
    await openCalibration(page);
    await activate(page, 40);
    const notice = page.getByText(
        "Calibration selection updated. Refresh the forecast to see the current result.",
    );
    await expect(notice).toBeVisible();
    await page.getByRole("button", { name: "Refresh", exact: true }).click();
    await expect(notice).toHaveCount(0);
});

test("the selection note names the affected forecasts and estimates", async ({
    page,
}) => {
    await mockApi(page);
    await routeRuns(page, () => [run(40)]);
    await openCalibration(page);
    await expect(
        page.getByText(
            "Issued forecasts keep their calibration. New forecasts and estimated past days use the selected calibration from its first training day. Reset also turns off automatic calibration.",
            { exact: true },
        ),
    ).toBeVisible();
});

test("detail and activation errors remain independent", async ({ page }) => {
    await mockApi(page);
    let details = 0;
    await page.route("**/plants/21/calibrations/", (route) =>
        route.fulfill({ json: paged([run(40)]) }),
    );
    await page.route("**/plants/21/calibrations/40/activate/", (route) =>
        route.fulfill({
            status: 503,
            json: errorBody("busy", "Activation failed"),
        }),
    );
    await page.route("**/plants/21/calibrations/40/", (route) =>
        ++details === 1
            ? route.fulfill({ json: run(40) })
            : route.fulfill({
                  status: 503,
                  json: errorBody("busy", "Detail failed"),
              }),
    );
    await openCalibration(page);
    await activate(page, 40);
    await expect(page.getByText("Activation failed")).toBeVisible();
    await page.getByRole("button", { name: "View details" }).click();
    await expect(page.getByText("Detail failed")).toBeVisible();
    await expect(page.getByText("Activation failed")).toBeVisible();
});

test("a release in progress leaves an activation unconfirmed instead of refused", async ({
    page,
}) => {
    await mockApi(page);
    await routeRuns(page, () => [run(40)]);
    await page.route("**/plants/21/calibrations/40/activate/", (route) =>
        route.fulfill({
            status: 503,
            headers: { "Retry-After": "60" },
            json: errorBody(
                "maintenance",
                "Pyracast is being updated. Try again in a minute; your data is safe.",
                { retry_after: 60 },
            ),
        }),
    );
    await openCalibration(page);
    await activate(page, 40);
    await expect(
        page.getByText(
            "It is not known whether it was saved. Check before you send it again.",
            { exact: true },
        ),
    ).toBeVisible();
    await expect(page.getByText(/Pyracast is being updated/)).toHaveCount(0);
});

test("automatic calibration can be enabled before a fit and disabled without resetting", async ({
    page,
}) => {
    await mockApi(page);
    let enabled = false;
    const changes: boolean[] = [];
    await routeRuns(page, () => (enabled ? [run(40)] : []));
    await page.route("**/plants/21/calibration/automation/", (route) => {
        if (route.request().method() === "PATCH") {
            expect(route.request().headers()["x-csrftoken"]).toBe(
                "signed-token",
            );
            enabled = route.request().postDataJSON().enabled;
            changes.push(enabled);
        }
        return route.fulfill({
            json: automation({
                enabled,
                authorized: enabled,
                next_check_at: enabled ? "2026-11-12T03:00:00Z" : null,
                disabled_cause: enabled ? null : "toggle",
                last_attempt: enabled
                    ? {
                          calibration_id: 40,
                          status: "skipped",
                          reason: "awaiting_new_holdout_days",
                          created_at: "2026-11-11T03:00:00Z",
                      }
                    : null,
            }),
        });
    });
    await openCalibration(page);
    await expect(
        page.getByText("Off: turned off by a workspace administrator."),
    ).toBeVisible();
    await page
        .getByRole("checkbox", { name: "Automatic calibration enabled" })
        .click();
    await expect(
        page.getByRole("checkbox", { name: "Automatic calibration enabled" }),
    ).toBeChecked();
    await expect(page.getByRole("row", { name: /#40/ })).toBeVisible();
    await expect(
        page.getByText("Waiting for seven new held-out days."),
    ).toBeVisible();
    await page
        .getByRole("checkbox", { name: "Automatic calibration enabled" })
        .click();
    await expect(
        page.getByRole("checkbox", { name: "Automatic calibration enabled" }),
    ).not.toBeChecked();
    expect(changes).toEqual([true, false]);
});

test("automatic status reloads after manual activation and reset", async ({
    page,
}) => {
    await mockApi(page);
    let enabled = false;
    await routeRuns(page, () => [run(40)]);
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
            json: automation({ enabled, authorized: enabled }),
        }),
    );
    await openCalibration(page);
    await activate(page, 40);
    await expect(
        page.getByRole("checkbox", { name: "Automatic calibration enabled" }),
    ).toBeChecked();
    await page.getByRole("button", { name: "Reset to baseline" }).click();
    await expect(
        page.getByText(
            "New forecasts and estimated past days use the uncalibrated model (factor 1). Issued forecasts keep their calibration. Automatic calibration will be turned off.",
        ),
    ).toBeVisible();
    await page.getByRole("button", { name: "Reset to baseline" }).click();
    await expect(
        page.getByRole("checkbox", { name: "Automatic calibration enabled" }),
    ).not.toBeChecked();
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
            return route.fulfill({ json: automation() });
        }
        return route.fulfill({
            json: automation({
                enabled,
                authorized: enabled,
                last_attempt: enabled
                    ? {
                          calibration_id: 40,
                          status: "skipped",
                          reason: "insufficient_current_source_validation",
                          created_at: "2026-11-11T03:00:00Z",
                      }
                    : null,
            }),
        });
    });
    await openCalibration(page);
    await expect(
        page.getByRole("checkbox", { name: "Automatic calibration enabled" }),
    ).not.toBeChecked();
    delayNext = true;
    await page.getByRole("button", { name: "Refresh", exact: true }).click();
    await observed;
    await page
        .getByRole("checkbox", { name: "Automatic calibration enabled" })
        .click();
    await expect(
        page.getByRole("checkbox", { name: "Automatic calibration enabled" }),
    ).toBeChecked();
    await expect(
        page.getByText(/Needs 30 days from this reference source/),
    ).toBeVisible();
    const oldResponse = page.waitForResponse(
        "**/plants/21/calibration/automation/",
    );
    release();
    await (await oldResponse).finished();
    await expect(
        page.getByRole("checkbox", { name: "Automatic calibration enabled" }),
    ).toBeChecked();
    await expect(
        page.getByText(/Needs 30 days from this reference source/),
    ).toBeVisible();
});

const states = [
    ["active", { tag: "In use", review: false }],
    ["candidate", { tag: "Calculated", review: true }],
    ["previous_configuration", { tag: "Outdated", review: false }],
    ["not_ready", { tag: "Not usable", review: false }],
] as const;

for (const [state, expected] of states)
    test(`a run in selection state ${state} is labelled and offers activation only as a candidate`, async ({
        page,
    }) => {
        await mockApi(page);
        await routeRuns(page, () => [run(40, { selection_state: state })]);
        await openCalibration(page);
        const row = page.getByRole("row", { name: /#40/ });
        await expect(row).toContainText(expected.tag);
        await expect(
            row.getByRole("button", { name: "Review activation" }),
        ).toHaveCount(expected.review ? 1 : 0);
    });

test("activating a candidate states its effect before it is confirmed", async ({
    page,
}) => {
    await mockApi(page);
    const activations: string[] = [];
    await routeRuns(page, () => [run(40)]);
    await page.route("**/plants/21/calibrations/40/activate/", (route) => {
        activations.push(route.request().url());
        return route.fulfill({ json: {} });
    });
    await openCalibration(page);
    await reviewActivation(page, 40);
    const confirmation = page.getByRole("region", {
        name: "Activate calibration #40",
    });
    await expect(confirmation).toContainText(
        "Factor 1.1 applies to new forecasts and to estimated past days from 2026-01-01, its first training day. Issued forecasts keep their calibration.",
    );
    await expect(confirmation).toContainText(
        "Automatic calibration will be turned on.",
    );
    expect(activations).toEqual([]);
    await confirmation.getByRole("button", { name: "Cancel" }).click();
    expect(activations).toEqual([]);
    await activate(page, 40);
    await expect(
        page.getByText(
            "Calibration selection updated. Refresh the forecast to see the current result.",
        ),
    ).toBeVisible();
    expect(activations).toHaveLength(1);
});

test("the calibration in force names its run, time and who selected it", async ({
    page,
}) => {
    await mockApi(page);
    await routeRuns(page, () => [run(40, { selection_state: "active" })]);
    await page.route("**/plants/21/calibration/automation/", (route) =>
        route.fulfill({
            json: automation({
                enabled: true,
                authorized: true,
                configuration_current: false,
                current_calibration: {
                    activation_id: 3,
                    calibration_id: 40,
                    factor: 1.1,
                    actor_id: null,
                    origin: "automatic",
                    created_at: "2026-01-12T09:00:00Z",
                    effective: false,
                },
            }),
        }),
    );
    await openCalibration(page);
    const inForce = page.getByRole("region", { name: "Calibration in force" });
    await expect(inForce).toContainText(/Calibration #40 with factor 1\.1/);
    await expect(inForce).toContainText("Selected by automatic calibration.");
    await expect(inForce).toContainText(
        "Forecasts do not apply it now because its training data changed",
    );
    await expect(
        page.getByText(
            "Paused until the plant configuration is approved again.",
        ),
    ).toBeVisible();
});

const blockers = [
    [
        "configuration_changed",
        "the technical details changed. It was made for another plant configuration",
    ],
    [
        "production_source_changed",
        "the actual production changed. The production source changed after it was trained.",
    ],
    [
        "outage_history_changed",
        "the outages changed. The outage history inside its window changed after it was trained.",
    ],
    [
        "incumbent_changed",
        "the calibration in use changed. The active calibration changed after it was compared.",
    ],
    [
        "calibration_not_activatable",
        "it cannot be used. It did not finish with a held-out evaluation that allows activation.",
    ],
] as const;

for (const [blocker, explanation] of blockers)
    test(`the ${blocker} blocker is explained and offers a new calibration instead of activation`, async ({
        page,
    }) => {
        await mockApi(page);
        const requests: unknown[] = [];
        await routeRuns(page, () => [
            run(40, {
                activation_blocker: blocker,
                ...(blocker === "configuration_changed"
                    ? { selection_state: "previous_configuration" }
                    : {}),
            }),
        ]);
        await page.route("**/plants/21/calibrations/", (route) => {
            if (route.request().method() !== "POST") return route.fallback();
            requests.push(route.request().postDataJSON());
            return route.fulfill({ status: 202, json: run(41) });
        });
        await openCalibration(page);
        await page
            .getByRole("row", { name: /#40/ })
            .getByRole("button", { name: "View details" })
            .click();
        await expect(
            page.getByText(
                `This calibration cannot be activated: ${explanation}`,
            ),
        ).toBeVisible();
        await expect(
            page.getByRole("button", { name: "Review activation" }),
        ).toHaveCount(blocker === "configuration_changed" ? 0 : 1);
        await page
            .getByRole("button", {
                name: "Request a new calibration for 2026-01-01 – 2026-01-10",
            })
            .click();
        await expect(
            page.getByText(
                "A new calibration was requested. It appears in the list when it starts.",
            ),
        ).toBeVisible();
        expect(requests).toEqual([
            {
                configuration_id: 31,
                start_date: "2026-01-01",
                end_date: "2026-01-10",
            },
        ]);
    });

test("a blocked run offers no new request while another run is open", async ({
    page,
}) => {
    await mockApi(page);
    await routeRuns(page, () => [
        run(41, { status: "running", selection_state: "not_ready" }),
        run(40, { activation_blocker: "incumbent_changed" }),
    ]);
    await openCalibration(page);
    await page
        .getByRole("row", { name: /#40/ })
        .getByRole("button", { name: "View details" })
        .click();
    await expect(
        page.getByText(
            "A calibration is already pending or running; request another one when it has finished.",
        ),
    ).toBeVisible();
    await expect(
        page.getByRole("button", { name: /Request a new calibration/ }),
    ).toHaveCount(0);
});

test("a blocked run whose window is not 7 to 90 days long says why it cannot be requested again", async ({
    page,
}) => {
    await mockApi(page);
    await routeRuns(page, () => [
        run(40, {
            activation_blocker: "incumbent_changed",
            window_end: "2026-01-03",
        }),
    ]);
    await openCalibration(page);
    await page
        .getByRole("row", { name: /#40/ })
        .getByRole("button", { name: "View details" })
        .click();
    await expect(
        page.getByText(
            "A new calibration needs a window of 7 to 90 days; this window cannot be requested again.",
        ),
    ).toBeVisible();
    await expect(page.getByText(/already pending or running/)).toHaveCount(0);
    await expect(
        page.getByRole("button", { name: /Request a new calibration/ }),
    ).toHaveCount(0);
});

const plantDays = [
    { end: "2026-01-10", offered: false },
    { end: "2026-01-09", offered: true },
] as const;

for (const day of plantDays)
    test(`a blocked run ending ${day.end} ${day.offered ? "can" : "cannot yet"} be requested again just after plant midnight`, async ({
        page,
    }) => {
        await page.clock.setFixedTime(new Date("2026-01-09T22:30:00Z"));
        await mockApi(page);
        await routeRuns(page, () => [
            run(40, {
                activation_blocker: "incumbent_changed",
                window_end: day.end,
            }),
        ]);
        await openCalibration(page);
        await page
            .getByRole("row", { name: /#40/ })
            .getByRole("button", { name: "View details" })
            .click();
        await expect(
            page.getByText(/This calibration cannot be activated/),
        ).toBeVisible();
        await expect(
            page.getByRole("button", { name: /Request a new calibration/ }),
        ).toHaveCount(day.offered ? 1 : 0);
        await expect(
            page.getByText(
                "Request a new calibration for this window once its last day has ended in plant time.",
            ),
        ).toHaveCount(day.offered ? 0 : 1);
        await expect(page.getByText(/already pending or running/)).toHaveCount(
            0,
        );
    });

test("a blocked run states no request reason until the calibration status is loaded", async ({
    page,
}) => {
    await mockApi(page);
    let release!: () => void;
    const gate = new Promise<void>((resolve) => {
        release = resolve;
    });
    await routeRuns(page, () => [
        run(40, { activation_blocker: "incumbent_changed" }),
    ]);
    await page.route("**/plants/21/calibration/automation/", async (route) => {
        await gate;
        return route.fulfill({ json: automation() });
    });
    await openCalibration(page);
    await page
        .getByRole("row", { name: /#40/ })
        .getByRole("button", { name: "View details" })
        .click();
    await expect(
        page.getByText(/This calibration cannot be activated/),
    ).toBeVisible();
    await expect(page.getByText(/already pending or running/)).toHaveCount(0);
    await expect(
        page.getByRole("button", { name: /Request a new calibration/ }),
    ).toHaveCount(0);
    release();
    await expect(
        page.getByRole("button", {
            name: "Request a new calibration for 2026-01-01 – 2026-01-10",
        }),
    ).toBeVisible();
});
