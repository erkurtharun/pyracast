import { readFile } from "node:fs/promises";
import Papa from "papaparse";
import { test, expect, type Page } from "./support/test";
import {
    answer,
    mockApi,
    paged,
    tenantRow,
    type Mock,
} from "./support/workspace";
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
        .getByRole("row", { name: new RegExp(`^${id}\\s`) })
        .getByRole("button", { name: "Use this calibration" })
        .click();
}

async function activate(page: Page, id: number) {
    await reviewActivation(page, id);
    await page.getByRole("button", { name: "Use", exact: true }).click();
}

const listDownload = (page: Page) =>
    page
        .locator(".section-heading", {
            has: page.getByRole("heading", {
                level: 2,
                name: "Calibrations",
                exact: true,
            }),
        })
        .getByRole("button", { name: "Download CSV" });

async function openCalibration(page: Page) {
    await page.goto("/app/");
    await page
        .getByRole("button", { name: /East array|Antalya solar park/ })
        .click();
    await page
        .getByRole("button", { name: "Calibration", exact: true })
        .click();
    await expect(
        page.getByRole("heading", { name: "Calibrations", exact: true }),
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
            .getByRole("row", { name: /^40\s/ })
            .getByRole("button", { name: "View details" })
            .click();
        await observed;
        await page
            .getByRole("row", { name: /^41\s/ })
            .getByRole("button", { name: "View details" })
            .click();
        await expect(
            page.getByRole("heading", { name: "Calibration No. 41" }),
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
            page.getByRole("heading", { name: "Calibration No. 41" }),
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
    await expect(page.getByRole("row", { name: /^41\s/ })).toBeVisible();
});

const selectionChanges = [
    {
        kind: "an activation",
        before: "candidate",
        after: "active",
        inUse: true,
        exports: 2,
        endpoint: "**/plants/21/calibrations/40/activate/",
        act: (page: Page) => activate(page, 40),
    },
    {
        kind: "a reset",
        before: "active",
        after: "candidate",
        inUse: false,
        exports: 1,
        endpoint: "**/plants/21/calibration/reset/",
        act: async (page: Page) => {
            await page
                .getByRole("button", {
                    name: "Use forecasts without calibration",
                })
                .click();
            await page
                .getByRole("button", {
                    name: "Use forecasts without calibration",
                })
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
        await expect(page.getByText("Saved.", { exact: true })).toBeVisible();
        const working = page.getByRole("button", { name: "Working…" });
        await expect(working).toHaveCount(change.exports);
        for (const button of await working.all())
            await expect(button).toBeDisabled();
        await expect(
            page.getByRole("button", { name: "Download CSV", exact: true }),
        ).toHaveCount(0);
        holding = false;
        release();
        const row = page.getByRole("row", { name: /^40\s/ });
        if (change.inUse) await expect(row).toContainText("In use");
        else await expect(row).not.toContainText("In use");
        const download = page.waitForEvent("download");
        await listDownload(page).click();
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
        .getByRole("row", { name: /^40\s/ })
        .getByRole("button", { name: "View details" })
        .click();
    await page
        .getByRole("button", {
            name: "Calculate new calibration for 2026-01-01 – 2026-01-10",
        })
        .click();
    await reload;
    await expect(
        page.getByText(
            "The new calibration will appear in the list when its calculation starts.",
        ),
    ).toBeVisible();
    const working = page.getByRole("button", { name: "Working…" });
    await expect(working).toHaveCount(2);
    for (const button of await working.all())
        await expect(button).toBeDisabled();
    await expect(
        page.getByRole("button", { name: "Download CSV", exact: true }),
    ).toHaveCount(0);
    holding = false;
    release();
    await expect(page.getByRole("row", { name: /^41\s/ })).toContainText(
        "Waiting",
    );
    const download = page.waitForEvent("download");
    await listDownload(page).click();
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
        .getByRole("row", { name: /^40\s/ })
        .getByRole("button", { name: "View details" })
        .click();
    const detail = page.locator(".calibration-detail");
    await expect(detail.locator(".tags")).toContainText("In use");
    await page
        .getByRole("button", { name: "Use forecasts without calibration" })
        .click();
    await page
        .getByRole("button", { name: "Use forecasts without calibration" })
        .click();
    await expect(
        detail.getByText(
            "This calibration cannot be used. The calibration in use changed after it was calculated.",
        ),
    ).toBeVisible();
    await expect(page.getByRole("row", { name: /^40\s/ })).not.toContainText(
        "In use",
    );
    await expect(detail.locator(".tags")).not.toContainText("In use");
    const download = page.waitForEvent("download");
    await detail.getByRole("button", { name: "Download CSV" }).click();
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
                .getByRole("region", { name: "Calibration in use" })
                .getByRole("heading", { name: "Calibration in use" }),
        ).toBeVisible();
        await expect(
            page.getByRole("button", {
                name: "Use forecasts without calibration",
            }),
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
                "The plant's technical details changed. Reload the page and try again.",
            ),
        }),
    );
    await openCalibration(page);
    await page
        .getByRole("button", { name: "Use forecasts without calibration" })
        .click();
    await page
        .getByRole("button", { name: "Use forecasts without calibration" })
        .click();
    await expect(
        page.getByText(
            "The plant's technical details changed. Reload the page and try again.",
            {
                exact: true,
            },
        ),
    ).toBeVisible();
    await expect(page.getByText("Saved.", { exact: true })).toHaveCount(0);
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
        await expect(listDownload(page)).toBeEnabled();
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
    const row = page.getByRole("row", { name: /^40\s/ });
    await expect(row).toContainText("Yerini yenisi aldı");
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
    const notice = page.getByText("Saved.", { exact: true });
    await expect(notice).toBeVisible();
    await page.getByRole("button", { name: "Refresh", exact: true }).click();
    await expect(notice).toHaveCount(0);
});

test("the effect of a change of calibration is stated once, in the dialog that makes it", async ({
    page,
}) => {
    await mockApi(page);
    await routeRuns(page, () => [run(40)]);
    await routeAutomation(page, { current_calibration: inForce(40) });
    await openCalibration(page);
    const effect = page.getByText(
        /Forecasts already made keep their calibration/,
    );
    await expect(page.getByRole("row", { name: /^40\s/ })).toBeVisible();
    await expect(effect).toHaveCount(0);
    await page
        .getByRole("button", { name: "Use forecasts without calibration" })
        .click();
    await expect(
        page.getByRole("region", {
            name: "Use forecasts without calibration?",
        }),
    ).toBeVisible();
    await expect(effect).toHaveCount(1);
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
            "The server did not confirm the change. It may have been saved; check before you send it again.",
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
        .getByRole("checkbox", { name: "Calibrate forecasts automatically" })
        .click();
    await expect(
        page.getByRole("checkbox", {
            name: "Calibrate forecasts automatically",
        }),
    ).toBeChecked();
    await expect(page.getByRole("row", { name: /^40\s/ })).toBeVisible();
    await expect(page.getByText("Waiting for 7 new test days.")).toBeVisible();
    await page
        .getByRole("checkbox", { name: "Calibrate forecasts automatically" })
        .click();
    await expect(
        page.getByRole("checkbox", {
            name: "Calibrate forecasts automatically",
        }),
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
        page.getByRole("checkbox", {
            name: "Calibrate forecasts automatically",
        }),
    ).toBeChecked();
    await page
        .getByRole("button", { name: "Use forecasts without calibration" })
        .click();
    await expect(
        page.getByText(
            "New forecasts and production calculated from satellite data are made without calibration (factor 1). Forecasts already made keep their calibration. Automatic calibration will be turned off.",
        ),
    ).toBeVisible();
    await page
        .getByRole("button", { name: "Use forecasts without calibration" })
        .click();
    await expect(
        page.getByRole("checkbox", {
            name: "Calibrate forecasts automatically",
        }),
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
        page.getByRole("checkbox", {
            name: "Calibrate forecasts automatically",
        }),
    ).not.toBeChecked();
    delayNext = true;
    await page.getByRole("button", { name: "Refresh", exact: true }).click();
    await observed;
    await page
        .getByRole("checkbox", { name: "Calibrate forecasts automatically" })
        .click();
    await expect(
        page.getByRole("checkbox", {
            name: "Calibrate forecasts automatically",
        }),
    ).toBeChecked();
    await expect(
        page.getByText(/Needs 30 days with current data/),
    ).toBeVisible();
    const oldResponse = page.waitForResponse(
        "**/plants/21/calibration/automation/",
    );
    release();
    await (await oldResponse).finished();
    await expect(
        page.getByRole("checkbox", {
            name: "Calibrate forecasts automatically",
        }),
    ).toBeChecked();
    await expect(
        page.getByText(/Needs 30 days with current data/),
    ).toBeVisible();
});

for (const role of ["admin", "viewer"] as const)
    test(`automation paused because its owner left says who can turn it on again, to the ${role}`, async ({
        page,
    }) => {
        await mockApi(page);
        if (role === "viewer")
            await page.route("**/api/tenants/", (route) =>
                route.fulfill({
                    json: paged([
                        tenantRow({ id: 1, name: "Sunfield Energy", role }),
                    ]),
                }),
            );
        await routeRuns(page, () => [run(40)]);
        const changes: boolean[] = [];
        let authorized = false;
        await page.route("**/plants/21/calibration/automation/", (route) => {
            if (route.request().method() === "PATCH") {
                changes.push(route.request().postDataJSON().enabled);
                authorized = true;
            }
            return route.fulfill({
                json: automation({
                    enabled: true,
                    authorized,
                    last_attempt: authorized
                        ? null
                        : {
                              calibration_id: null,
                              status: "skipped",
                              reason: "authorization_revoked",
                              created_at: "2026-01-12T03:00:00Z",
                          },
                }),
            });
        });
        await openCalibration(page);
        await expect(
            page.locator("p", { hasText: "Last result:" }),
        ).toContainText(
            "The person who turned it on is no longer a workspace administrator.",
        );
        const member = page.getByText(
            "The workspace administrator needs to turn automatic calibration on again.",
            { exact: true },
        );
        const administrator = page.getByText(
            "Automatic calibration is paused: the person who turned it on is no longer a workspace administrator. Turn it on again to resume.",
            { exact: true },
        );
        const turnOn = page.getByRole("button", {
            name: "Turn on automatic calibration",
        });
        if (role === "viewer") {
            await expect(member).toBeVisible();
            await expect(administrator).toHaveCount(0);
            await expect(turnOn).toHaveCount(0);
            return;
        }
        await expect(administrator).toBeVisible();
        await expect(member).toHaveCount(0);
        await turnOn.click();
        await expect(administrator).toHaveCount(0);
        expect(changes).toEqual([true]);
    });

test("a preliminary calibration is marked in the list and its detail gives the factor and the reason", async ({
    page,
}) => {
    await mockApi(page);
    await routeRuns(page, () => [
        run(40, { assessment: "provisional", factor: 1.0625 }),
    ]);
    await openCalibration(page);
    const row = page.getByRole("row", { name: /^40\s/ });
    await expect(row.getByText("Preliminary", { exact: true })).toBeVisible();
    await row.getByRole("button", { name: "View details" }).click();
    const detail = page.locator(".calibration-detail");
    await expect(detail.locator(".tags")).toContainText("Preliminary");
    await expect(detail).toContainText(
        "Preliminary: based on fewer than 30 days.",
    );
    await expect(detail.locator(".stat", { hasText: "Factor" })).toContainText(
        "1.0625",
    );
});

const states = [
    ["active", { inUse: true, review: false }],
    ["candidate", { inUse: false, review: true }],
    ["previous_configuration", { inUse: false, review: false }],
    ["not_ready", { inUse: false, review: false }],
] as const;

for (const [state, expected] of states)
    test(`a run in selection state ${state} is marked in use only when selected and offers its use only as a candidate`, async ({
        page,
    }) => {
        await mockApi(page);
        await routeRuns(page, () => [run(40, { selection_state: state })]);
        await openCalibration(page);
        const row = page.getByRole("row", { name: /^40\s/ });
        await expect(row).toBeVisible();
        await expect(row.getByText("In use", { exact: true })).toHaveCount(
            expected.inUse ? 1 : 0,
        );
        await expect(row).not.toContainText(
            /Active|Candidate|Earlier configuration|Not ready/,
        );
        await expect(
            row.getByRole("button", { name: "Use this calibration" }),
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
        name: "Use calibration No. 40?",
    });
    await expect(confirmation).toContainText(
        "Factor 1.1 applies to new forecasts and to production calculated from satellite data from 2026-01-01, its first calibration day. Forecasts already made keep their calibration.",
    );
    await expect(confirmation).toContainText(
        "Automatic calibration will be turned on.",
    );
    expect(activations).toEqual([]);
    await confirmation.getByRole("button", { name: "Cancel" }).click();
    expect(activations).toEqual([]);
    await activate(page, 40);
    await expect(page.getByText("Saved.", { exact: true })).toBeVisible();
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
    const inForce = page.getByRole("region", { name: "Calibration in use" });
    await expect(inForce).toContainText(/Calibration No\. 40, factor 1\.1\./);
    await expect(inForce).toContainText("Chosen by automatic calibration.");
    await expect(inForce).toContainText(
        "Not applied: the data of its calibration days changed.",
    );
    await expect(
        page.getByText(
            "Automatic calibration is paused: this plant has no current technical details.",
        ),
    ).toBeVisible();
});

const blockers = [
    [
        "configuration_changed",
        "It was calculated with other technical details, or the plant has no current technical details.",
    ],
    [
        "production_source_changed",
        "The source of actual production changed after it was calculated.",
    ],
    [
        "outage_history_changed",
        "Outages in its period changed after it was calculated.",
    ],
    [
        "incumbent_changed",
        "The calibration in use changed after it was calculated.",
    ],
    [
        "calibration_not_activatable",
        "Its calculation did not end with a result that can be used.",
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
            .getByRole("row", { name: /^40\s/ })
            .getByRole("button", { name: "View details" })
            .click();
        await expect(
            page.getByText(`This calibration cannot be used. ${explanation}`),
        ).toBeVisible();
        await expect(
            page.getByRole("button", { name: "Use this calibration" }),
        ).toHaveCount(blocker === "configuration_changed" ? 0 : 1);
        await page
            .getByRole("button", {
                name: "Calculate new calibration for 2026-01-01 – 2026-01-10",
            })
            .click();
        await expect(
            page.getByText(
                "The new calibration will appear in the list when its calculation starts.",
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
        .getByRole("row", { name: /^40\s/ })
        .getByRole("button", { name: "View details" })
        .click();
    await expect(
        page.getByText(
            "A calibration is queued or being calculated. Calculate a new one when it has finished.",
        ),
    ).toBeVisible();
    await expect(
        page.getByRole("button", { name: /Calculate new calibration/ }),
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
        .getByRole("row", { name: /^40\s/ })
        .getByRole("button", { name: "View details" })
        .click();
    await expect(
        page.getByText(
            "A new calibration needs a period of 7 to 90 days; this period cannot be calculated again.",
        ),
    ).toBeVisible();
    await expect(page.getByText(/queued or being calculated/)).toHaveCount(0);
    await expect(
        page.getByRole("button", { name: /Calculate new calibration/ }),
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
            .getByRole("row", { name: /^40\s/ })
            .getByRole("button", { name: "View details" })
            .click();
        await expect(
            page.getByText(/This calibration cannot be used/),
        ).toBeVisible();
        await expect(
            page.getByRole("button", { name: /Calculate new calibration/ }),
        ).toHaveCount(day.offered ? 1 : 0);
        await expect(
            page.getByText(
                "Calculate a new calibration for this period once its last day has ended in plant time.",
            ),
        ).toHaveCount(day.offered ? 0 : 1);
        await expect(page.getByText(/queued or being calculated/)).toHaveCount(
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
        .getByRole("row", { name: /^40\s/ })
        .getByRole("button", { name: "View details" })
        .click();
    await expect(
        page.getByText(/This calibration cannot be used/),
    ).toBeVisible();
    await expect(page.getByText(/queued or being calculated/)).toHaveCount(0);
    await expect(
        page.getByRole("button", { name: /Calculate new calibration/ }),
    ).toHaveCount(0);
    release();
    await expect(
        page.getByRole("button", {
            name: "Calculate new calibration for 2026-01-01 – 2026-01-10",
        }),
    ).toBeVisible();
});
