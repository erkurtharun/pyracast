import { test, expect, type Page } from "./support/test";
import { accessibilityViolations } from "./support/accessibility";
import { answer, mockApi, paged, tenantRow, user } from "./support/workspace";
import { nativeProductionHistory } from "./support/native-production";
import { errorBody } from "./support/errors";

const reading = {
    energy_mwh: 1,
    quality: "valid",
    source: "meter",
    source_kind: "manual",
    provisional: false,
    aggregated: false,
};
function proposal(id = 1) {
    return {
        id,
        kind: "production",
        status: "pending",
        plant_id: 21,
        region_id: 11,
        plant_name: "East array",
        configuration_id: 31,
        timezone: "Europe/Istanbul",
        item_count: 1,
        period_start: "2026-11-09T08:00:00+03:00",
        period_end: "2026-11-09T08:30:00+03:00",
        incident_action: null,
        incident_title: null,
        proposed_by: { id: 2, name: "Member" },
        created_at: "2026-11-09T12:00:00Z",
        reason: "Verified meter reading",
        reviewed_by: null,
        closed_at: null,
        decision_reason: "",
        former_member: false,
        can_review: true,
        can_withdraw: false,
        unread: false,
        conflict: null,
        review_fingerprint: "context-1",
        incident: null,
        production_items: [
            {
                id: 1,
                starts_at: "2026-11-09T08:00:00+03:00",
                ends_at: "2026-11-09T08:30:00+03:00",
                action: "set",
                expected_revision_id: null,
                baseline_energy_mwh: 1,
                baseline_source_kind: "manual",
                current: reading,
                automatic: reading,
                proposed: { ...reading, energy_mwh: 0.75 },
                energy_mwh: 0.75,
                quality: "valid",
                source: "meter",
                applied_observation_id: null,
            },
        ],
    };
}

async function member(page: Page, language = "en") {
    await page.clock.setFixedTime(new Date("2026-11-10T12:00:00Z"));
    await mockApi(page);
    await page.route("**/api/session/", (route) =>
        route.fulfill({ json: { user, language, csrf_token: "signed-token" } }),
    );
    await page.route("**/api/tenants/", (route) =>
        route.fulfill({
            json: paged([
                {
                    id: 1,
                    name: "Workspace",
                    role: "viewer",
                    pending_review_count: null,
                    unread_outcome_count: 0,
                },
            ]),
        }),
    );
    await page.route("**/change-requests/?*", (route) =>
        route.fulfill({ json: paged([]) }),
    );
}

async function openPlant(page: Page, tab: string) {
    await page.goto("/app/");
    await page
        .getByRole("button", { name: /East array|Antalya solar park/ })
        .click();
    await page.getByRole("button", { name: tab, exact: true }).click();
}

function draft(page: Page) {
    return page.getByRole("region", {
        name: "Correction request",
        exact: true,
    });
}

async function stage(page: Page, start: string, energy: string) {
    await page
        .getByRole("button", { name: `Suggest correction for ${start}` })
        .click();
    const editor = page.locator(".production-row-editor");
    await editor.getByRole("textbox").first().fill(energy);
    await editor
        .getByRole("button", { name: "Add to request", exact: true })
        .click();
}

test("the correction reason is named by its label and described by the approval help", async ({
    page,
}) => {
    await member(page);
    await page.route("**/api/**/production/?*", (route) => {
        const date = new URL(route.request().url()).searchParams.get(
            "start_date",
        )!;
        return route.fulfill({
            json: nativeProductionHistory(date, "Europe/Istanbul", [
                {
                    starts_at: `${date}T08:00:00+03:00`,
                    ends_at: `${date}T08:30:00+03:00`,
                    effective: reading,
                    can_edit: false,
                    can_propose: true,
                    can_propose_revert: false,
                    revision_id: null,
                    observation: null,
                },
            ]),
        });
    });
    await openPlant(page, "Production");
    await page.getByRole("button", { name: /^Suggest correction for/ }).click();
    await expect(
        page
            .locator(".production-row-editor")
            .getByRole("textbox", { name: "Reason for correction" }),
    ).toHaveCount(0);
    await page.getByRole("button", { name: "Add to request" }).click();
    const reason = draft(page).getByRole("textbox", {
        name: "Reason for correction",
        exact: true,
    });
    await expect(reason).toHaveAccessibleDescription(
        "A workspace administrator reviews this submission before it changes live data.",
    );
});

test("member submits a native production correction with a stable retry key and no live write", async ({
    page,
}, testInfo) => {
    await member(page);
    await page.setViewportSize({ width: 320, height: 844 });
    const submissions: Record<string, unknown>[] = [];
    let canRevert = false;
    const direct: string[] = [];
    page.on("request", (request) => {
        if (
            request.method() !== "GET" &&
            request.url().includes("production/changes/")
        )
            direct.push(request.url());
    });
    await page.route("**/api/**/production/?*", (route) => {
        const date = new URL(route.request().url()).searchParams.get(
            "start_date",
        )!;
        return route.fulfill({
            json: nativeProductionHistory(date, "Europe/Istanbul", [
                {
                    starts_at: `${date}T08:00:00+03:00`,
                    ends_at: `${date}T08:30:00+03:00`,
                    effective: reading,
                    can_edit: false,
                    can_propose: true,
                    can_propose_revert: canRevert,
                    revision_id: canRevert ? 4 : null,
                    observation: canRevert
                        ? {
                              id: 4,
                              configuration_id: 31,
                              starts_at: `${date}T08:00:00+03:00`,
                              ends_at: `${date}T08:30:00+03:00`,
                              energy_mwh: 1,
                              quality: "valid",
                              source: "meter",
                              recorded_by_id: 2,
                              created_at: `${date}T09:00:00Z`,
                              supersedes_id: null,
                              is_reverted: false,
                          }
                        : null,
                },
            ]),
        });
    });
    await page.route("**/change-requests/production/", (route) => {
        expect(route.request().headers()["x-csrftoken"]).toBe("signed-token");
        submissions.push(route.request().postDataJSON());
        return submissions.length === 1
            ? route.fulfill({
                  status: 503,
                  json: errorBody(
                      "busy",
                      "The workspace is busy. Try again in a moment.",
                      { retry_after: 1 },
                  ),
              })
            : route.fulfill({ status: 201, json: proposal() });
    });
    await openPlant(page, "Production");
    await stage(page, "08:00", "0,75");
    const staged = draft(page);
    await staged.getByLabel("Reason for correction").fill("Checked meter");
    await page.evaluate(() => window.scrollTo(0, 0));
    await page.screenshot({
        path: testInfo.outputPath("production-proposal-320.png"),
        fullPage: true,
    });
    expect(
        await page.evaluate(() => document.documentElement.scrollWidth),
    ).toBeLessThanOrEqual(320);
    await staged
        .getByRole("button", { name: "Send for approval", exact: true })
        .click();
    await expect(
        staged.getByText("The workspace is busy. Try again in a moment."),
    ).toBeVisible();
    await expect(staged.getByLabel("Reason for correction")).toHaveValue(
        "Checked meter",
    );
    await staged
        .getByRole("button", { name: "Send for approval", exact: true })
        .click();
    await expect(page.getByText(/^Sent for approval\./)).toBeVisible();
    await expect(staged).toHaveCount(0);
    expect(submissions).toHaveLength(2);
    expect(submissions[0]?.idempotency_key).toEqual(expect.any(String));
    expect(submissions[1]?.idempotency_key).toEqual(expect.any(String));
    expect(submissions[0]?.idempotency_key).toBe(
        submissions[1]?.idempotency_key,
    );
    expect(submissions[1]).toMatchObject({
        reason: "Checked meter",
        unit: "MWh",
        changes: [
            {
                energy: 0.75,
                starts_at: expect.stringContaining("T08:00:00"),
                ends_at: expect.stringContaining("T08:30:00"),
                expected_revision_id: null,
            },
        ],
    });
    expect(direct).toEqual([]);
    await expect(
        page.getByRole("button", { name: "Import CSV", exact: true }),
    ).toHaveCount(0);
    canRevert = true;
    await openPlant(page, "Production");
    await page.getByRole("button", { name: /^Suggest correction for/ }).click();
    await page
        .getByRole("button", { name: /^Request provider reading for/ })
        .click();
    await draft(page)
        .getByLabel("Reason for correction")
        .fill("Restore the automatic reading");
    await draft(page)
        .getByRole("button", { name: "Send for approval", exact: true })
        .click();
    await expect(page.getByText(/^Sent for approval\./)).toBeVisible();
    expect(submissions[2]).toMatchObject({
        reason: "Restore the automatic reading",
        changes: [{ action: "revert", expected_revision_id: 4 }],
    });
    expect(
        (submissions[2]?.changes as Record<string, unknown>[] | undefined)?.[0],
    ).not.toHaveProperty("energy");
    expect(submissions[2]?.idempotency_key).not.toBe(
        submissions[1]?.idempotency_key,
    );
    expect(direct).toEqual([]);
});

test("a server error shows its reference", async ({ page }) => {
    await member(page);
    await page.route("**/api/**/production/?*", (route) => {
        const date = new URL(route.request().url()).searchParams.get(
            "start_date",
        )!;
        return route.fulfill({
            json: nativeProductionHistory(date, "Europe/Istanbul", [
                {
                    starts_at: `${date}T08:00:00+03:00`,
                    ends_at: `${date}T08:30:00+03:00`,
                    effective: reading,
                    can_edit: false,
                    can_propose: true,
                    can_propose_revert: false,
                    revision_id: null,
                    observation: null,
                },
            ]),
        });
    });
    await page.route("**/change-requests/production/", (route) =>
        route.fulfill({
            status: 500,
            json: errorBody(
                "server_error",
                "The server could not complete this request.",
                { request_id: "0123456789abcdef0123456789abcdef" },
            ),
        }),
    );
    await openPlant(page, "Production");
    await stage(page, "08:00", "0,75");
    const staged = draft(page);
    await staged.getByLabel("Reason for correction").fill("Checked meter");
    await staged
        .getByRole("button", { name: "Send for approval", exact: true })
        .click();
    await expect(
        staged.getByText(
            "The server could not complete this request. Reference: 0123456789abcdef0123456789abcdef",
            { exact: true },
        ),
    ).toBeVisible();
});

test("member outage reports remain proposals and use explicit action envelopes", async ({
    page,
}, testInfo) => {
    await member(page);
    await page.setViewportSize({ width: 320, height: 844 });
    const submissions: Record<string, unknown>[] = [];
    const outage = {
        id: 4,
        revision: 2,
        kind: "inverter",
        title: "Inverter offline",
        notes: "",
        starts_at: "2026-11-09T08:00:00Z",
        ends_at: null,
        available_dc_mw: null,
        available_inverter_mw: null,
        available_export_mw: null,
        status: "active",
        declared_configuration_id: 31,
    };
    await page.route("**/api/**/outages/", (route) => {
        expect(route.request().method()).toBe("GET");
        return route.fulfill({ json: paged([outage]) });
    });
    await page.route("**/change-requests/incidents/", (route) => {
        submissions.push(route.request().postDataJSON());
        return route.fulfill({ status: 201, json: proposal() });
    });
    await openPlant(page, "Outages");
    await page
        .getByRole("button", { name: "Submit outage", exact: true })
        .click();
    const editor = page.getByRole("region", {
        name: "Report outage",
        exact: true,
    });
    await editor.getByLabel("Incident title").fill("New outage");
    await editor.getByLabel("Capacity impact").selectOption("unknown");
    await page.evaluate(() => window.scrollTo(0, 0));
    await page.screenshot({
        path: testInfo.outputPath("outage-proposal-320.png"),
        fullPage: true,
    });
    expect(
        await page.evaluate(() => document.documentElement.scrollWidth),
    ).toBeLessThanOrEqual(320);
    await editor.getByRole("button", { name: "Send for approval" }).click();
    await expect
        .poll(() => submissions[0])
        .toMatchObject({
            action: "create",
            values: {
                title: "New outage",
                available_dc_mw: null,
                available_inverter_mw: null,
                available_export_mw: null,
            },
        });
    await page
        .getByRole("button", { name: "Suggest changes", exact: true })
        .click();
    await page.getByLabel("Incident title").fill("Corrected title");
    await page
        .getByRole("button", { name: "Send for approval", exact: true })
        .click();
    await expect
        .poll(() => submissions[1])
        .toMatchObject({
            action: "update",
            incident_id: 4,
            expected_revision: 2,
            values: { title: "Corrected title" },
        });
    await page
        .getByRole("button", { name: "Submit resolution", exact: true })
        .click();
    await page
        .getByRole("button", { name: "Send for approval", exact: true })
        .click();
    await expect
        .poll(() => submissions[2])
        .toMatchObject({
            action: "resolve",
            incident_id: 4,
            expected_revision: 2,
            ends_at: expect.any(String),
        });
    await page.getByText("Request cancellation", { exact: true }).click();
    await page.getByRole("checkbox").check();
    await page
        .getByRole("button", { name: "Send for approval", exact: true })
        .click();
    await expect
        .poll(() => submissions[3])
        .toMatchObject({
            action: "cancel",
            incident_id: 4,
            expected_revision: 2,
        });
    await expect(
        page.getByRole("heading", { name: "Inverter offline" }),
    ).toBeVisible();
    expect(new Set(submissions.map((item) => item.idempotency_key)).size).toBe(
        4,
    );
});

test("mobile admin reviews current context, refreshes a conflict and approves explicitly", async ({
    page,
}, testInfo) => {
    await mockApi(page);
    await page.setViewportSize({ width: 320, height: 844 });
    let item = {
        ...proposal(),
        former_member: true,
        production_items: [
            {
                ...proposal().production_items[0],
                current: null,
                provider_intervals: [
                    {
                        starts_at: "2026-11-09T08:00:00+03:00",
                        ends_at: "2026-11-09T09:00:00+03:00",
                        energy_mwh: 1,
                        quality: "valid",
                        source_kind: "settlement_injection",
                    },
                ],
                warnings: [
                    {
                        code: "provider_hour_incomplete",
                        starts_at: "2026-11-09T08:00:00+03:00",
                        ends_at: "2026-11-09T09:00:00+03:00",
                    },
                ],
            },
        ],
    };
    let failed = false;
    let count = 1;
    await page.route("**/api/tenants/", (route) =>
        route.fulfill({
            json: paged([
                {
                    id: 1,
                    name: "Workspace",
                    role: "admin",
                    pending_review_count: count,
                    unread_outcome_count: 0,
                },
            ]),
        }),
    );
    await page.route("**/change-requests/?*", (route) => {
        const scope = new URL(route.request().url()).searchParams.get("scope");
        return route.fulfill({
            json: paged(
                scope === "pending" && item.status !== "pending" ? [] : [item],
            ),
        });
    });
    await page.route("**/change-requests/1/", (route) =>
        route.fulfill({ json: item }),
    );
    await page.route("**/change-requests/1/approve/", (route) => {
        expect(route.request().postDataJSON()).toEqual({
            review_fingerprint: failed ? "context-2" : "context-1",
        });
        if (!failed) {
            failed = true;
            item = { ...item, review_fingerprint: "context-2" };
            return route.fulfill({
                status: 409,
                json: errorBody(
                    "review_context_changed",
                    "Review context changed.",
                ),
            });
        }
        item = { ...item, status: "approved", can_review: false };
        count = 0;
        return route.fulfill({ json: item });
    });
    await page.goto("/app/");
    await page
        .getByRole("button", { name: "Inbox, 1 update", exact: true })
        .click();
    await page
        .getByRole("button", { name: "Review details", exact: true })
        .click();
    await expect(page.getByText(/Former member/)).toBeVisible();
    await expect(
        page.locator(".proposal-energy").filter({ hasText: "0.75 MWh" }),
    ).toBeVisible();
    const detail = page.locator(".inbox-detail");
    await expect(
        detail.getByText(/The remaining portion will be missing/),
    ).toBeVisible();
    await detail
        .getByText("Automatic source intervals", { exact: true })
        .click();
    await expect(detail.locator("details li")).toHaveText(
        /09:00 AM GMT\+3: 1 MWh/,
    );
    await expect(detail.getByText("0.5 MWh", { exact: true })).toHaveCount(0);
    await detail
        .getByRole("button", { name: "Approve and apply", exact: true })
        .click();
    await expect(
        detail.getByRole("button", { name: "Approve and apply" }),
    ).toBeDisabled();
    await page
        .getByRole("button", { name: "Refresh list", exact: true })
        .click();
    await expect(
        detail.getByRole("button", { name: "Approve and apply" }),
    ).toBeDisabled();
    await detail
        .getByRole("button", { name: "Refresh details", exact: true })
        .click();
    await expect(
        detail.getByRole("button", { name: "Approve and apply" }),
    ).toBeEnabled();
    await expect(
        page.getByRole("button", { name: "Inbox, 1 update", exact: true }),
    ).toBeVisible();
    await page.evaluate(() => window.scrollTo(0, 0));
    await page.screenshot({
        path: testInfo.outputPath("inbox-admin-320.png"),
        fullPage: true,
    });
    expect(await accessibilityViolations(page, "main")).toEqual([]);
    expect(
        await page.evaluate(() => document.documentElement.scrollWidth),
    ).toBeLessThanOrEqual(320);
    await detail.getByRole("button", { name: "Approve and apply" }).click();
    await expect(
        page.getByRole("button", { name: "Inbox", exact: true }),
    ).toBeVisible();
    await expect(page.getByText("Approved", { exact: true })).toBeVisible();
    await expect(page.locator(".inbox-card")).toHaveCount(0);
    await expect(
        page.getByText("Approved", { exact: true }).locator(".."),
    ).toBeFocused();
    await page.getByRole("button", { name: "History", exact: true }).click();
    await page
        .getByRole("button", { name: "Review details", exact: true })
        .click();
    await expect(
        page.getByRole("heading", { name: "At decision", exact: true }),
    ).toBeVisible();
});

test("Turkish member can withdraw and explicitly mark an outcome read at 320px", async ({
    page,
}, testInfo) => {
    await member(page, "tr");
    await page.setViewportSize({ width: 320, height: 844 });
    let item = {
        ...proposal(),
        proposed_by: { id: 1, name: "solar.user" },
        can_review: false,
        can_withdraw: true,
        unread: false,
    };
    let seen = 0;
    let unread = 1;
    let outcome = {
        ...proposal(2),
        proposed_by: { id: 1, name: "solar.user" },
        status: "approved",
        can_review: false,
        can_withdraw: false,
        unread: true,
    };
    await page.route("**/api/tenants/", (route) =>
        route.fulfill({
            json: paged([
                {
                    id: 1,
                    name: "Workspace",
                    role: "viewer",
                    pending_review_count: null,
                    unread_outcome_count: unread,
                },
            ]),
        }),
    );
    await page.route("**/change-requests/?*", (route) => {
        expect(new URL(route.request().url()).searchParams.get("scope")).toBe(
            "mine",
        );
        return route.fulfill({ json: paged([item, outcome]) });
    });
    await page.route("**/change-requests/1/", (route) =>
        route.fulfill({ json: item }),
    );
    await page.route("**/change-requests/2/", (route) =>
        route.fulfill({ json: outcome }),
    );
    await page.route("**/change-requests/1/withdraw/", (route) => {
        item = {
            ...item,
            status: "withdrawn",
            can_withdraw: false,
            unread: false,
        };
        return route.fulfill({ json: item });
    });
    await page.route("**/change-requests/2/seen/", (route) => {
        seen++;
        outcome = { ...outcome, unread: false };
        unread = 0;
        return route.fulfill({ json: outcome });
    });
    await page.goto("/app/");
    await page
        .getByRole("button", {
            name: "Gelen kutusu, 1 güncelleme",
            exact: true,
        })
        .click();
    await page
        .getByRole("button", { name: "Ayrıntıları incele", exact: true })
        .first()
        .click();
    await expect(
        page.getByRole("button", { name: "Onayla ve uygula" }),
    ).toHaveCount(0);
    await expect(
        page.getByRole("button", { name: "Listeyi yenile", exact: true }),
    ).toBeVisible();
    await expect(
        page.getByRole("button", { name: "Ayrıntıları yenile", exact: true }),
    ).toBeVisible();
    await page
        .getByRole("button", { name: "Başvuruyu geri çek", exact: true })
        .click();
    await expect(
        page.getByRole("status").filter({ hasText: /^Geri çekildi$/ }),
    ).toBeVisible();
    await expect(
        page.getByRole("button", {
            name: "Gelen kutusu, 1 güncelleme",
            exact: true,
        }),
    ).toBeVisible();
    expect(seen).toBe(0);
    await page.evaluate(() => window.scrollTo(0, 0));
    await page.screenshot({
        path: testInfo.outputPath("inbox-member-tr-320.png"),
        fullPage: true,
    });
    await page
        .locator(".inbox-card")
        .filter({ hasText: "Onaylandı" })
        .getByRole("button", { name: "Ayrıntıları incele", exact: true })
        .click();
    await page
        .getByRole("button", { name: "Okundu olarak işaretle", exact: true })
        .click();
    await expect(
        page.getByRole("button", { name: "Gelen kutusu", exact: true }),
    ).toBeVisible();
    expect(seen).toBe(1);
});

test("inbox loads bounded pages and requires an explicit rejection reason", async ({
    page,
}) => {
    await mockApi(page);
    const requests: string[] = [];
    let rejected = false;
    let rejection: unknown;
    const values = {
        kind: "grid",
        title: "Grid unavailable",
        notes: "",
        starts_at: "2026-11-09T08:00:00Z",
        ends_at: null,
        available_dc_mw: null,
        available_inverter_mw: 0,
        available_export_mw: 0,
    };
    const item = () => ({
        ...proposal(2),
        kind: "incident",
        production_items: [],
        status: rejected ? "rejected" : "pending",
        can_review: !rejected,
        decision_reason: rejected ? "Please verify the start time" : "",
        conflict: {
            code: "request_conflict",
            message: "The plant configuration changed. Submit a new request.",
        },
        incident: {
            action: "create",
            incident_id: null,
            expected_revision: null,
            values,
            ends_at: null,
            before: null,
            applied_revision_id: null,
        },
    });
    await page.route("**/change-requests/?*", (route) => {
        const url = new URL(route.request().url());
        const scope = url.searchParams.get("scope")!;
        const pageNumber = url.searchParams.get("page")!;
        requests.push(`${scope}:${pageNumber}`);
        const base = `/api/tenants/1/change-requests/?scope=${scope}`;
        return route.fulfill({
            json: {
                count: 51,
                next: pageNumber === "1" ? `${base}&page=2` : null,
                previous: pageNumber === "2" ? `${base}&page=1` : null,
                results:
                    pageNumber === "1"
                        ? [{ ...proposal(), plant_name: "First page plant" }]
                        : [item()],
            },
        });
    });
    await page.route("**/change-requests/2/", (route) =>
        route.fulfill({ json: item() }),
    );
    await page.route("**/change-requests/2/reject/", (route) => {
        rejection = route.request().postDataJSON();
        rejected = true;
        return route.fulfill({ json: item() });
    });
    await page.goto("/app/");
    await page.getByRole("button", { name: "Inbox", exact: true }).click();
    await expect(
        page.getByText("First page plant", { exact: true }),
    ).toBeVisible();
    expect(new Set(requests)).toEqual(new Set(["pending:1"]));
    await page.getByRole("button", { name: "Next", exact: true }).click();
    await expect(
        page.getByText("First page plant", { exact: true }),
    ).toHaveCount(0);
    await page
        .getByRole("button", { name: "Review details", exact: true })
        .click();
    await expect(
        page.getByText(
            "Approving this ongoing outage immediately affects live forecasts and reminder emails.",
        ),
    ).toBeVisible();
    const detail = page.locator(".inbox-detail");
    await expect(
        detail.getByRole("button", { name: "Approve and apply", exact: true }),
    ).toBeDisabled();
    await detail.getByRole("button", { name: "Reject", exact: true }).click();
    expect(rejection).toBeUndefined();
    await detail
        .getByLabel("Reason for rejection")
        .fill(" Please verify the start time ");
    await detail.getByRole("button", { name: "Reject", exact: true }).click();
    await expect(
        page.getByRole("status").filter({ hasText: /^Rejected$/ }),
    ).toBeVisible();
    expect(rejection).toEqual({ reason: "Please verify the start time" });
    await page.getByRole("button", { name: "History", exact: true }).click();
    await expect(
        page.getByText("First page plant", { exact: true }),
    ).toBeVisible();
    expect(requests).toContain("pending:2");
    expect(requests).toContain("history:1");
    expect(requests).not.toContain("history:2");
});

test("workspace and current role changes clear review controls and previous tenant data", async ({
    page,
}) => {
    await page.clock.install({ time: new Date("2026-11-10T12:00:00Z") });
    await mockApi(page);
    let role = "admin";
    const scopes: string[] = [];
    await page.route("**/api/tenants/", (route) =>
        route.fulfill({
            json: paged([
                {
                    id: 1,
                    name: "One",
                    role,
                    pending_review_count: role === "admin" ? 1 : null,
                    unread_outcome_count: 0,
                },
                {
                    id: 2,
                    name: "Two",
                    role: "viewer",
                    pending_review_count: null,
                    unread_outcome_count: 0,
                },
            ]),
        }),
    );
    await page.route("**/change-requests/?*", (route) => {
        const url = new URL(route.request().url());
        scopes.push(url.searchParams.get("scope")!);
        return route.fulfill({
            json: paged(
                url.pathname.includes("/tenants/2/")
                    ? []
                    : [{ ...proposal(), can_review: role === "admin" }],
            ),
        });
    });
    await page.route("**/change-requests/1/", (route) =>
        route.fulfill({
            json: { ...proposal(), can_review: role === "admin" },
        }),
    );
    await page.goto("/app/");
    await page
        .getByRole("button", { name: "Inbox, 1 update", exact: true })
        .click();
    await page
        .getByRole("button", { name: "Review details", exact: true })
        .click();
    await expect(
        page.getByRole("button", { name: "Approve and apply", exact: true }),
    ).toBeVisible();
    role = "viewer";
    await page.clock.runFor(120_001);
    await expect(
        page.getByRole("button", { name: "Approve and apply", exact: true }),
    ).toHaveCount(0);
    await expect(
        page.getByRole("button", { name: "History", exact: true }),
    ).toHaveCount(0);
    await expect.poll(() => scopes.at(-1)).toBe("mine");
    await page
        .getByRole("combobox", { name: "Workspace", exact: true })
        .selectOption("2");
    await expect(page.getByText("East array", { exact: true })).toHaveCount(0);
    await expect(
        page.getByText("No submissions here.", { exact: true }),
    ).toBeVisible();
});

for (const removal of ["decision", "concurrent"] as const) {
    test(`the final pending page remains navigable after ${removal} removal`, async ({
        page,
    }) => {
        await mockApi(page);
        let finalPending = true;
        let invalidPageRequests = 0;
        const firstPage = Array.from({ length: 50 }, (_, index) => ({
            ...proposal(100 + index),
            plant_name:
                index === 0 ? "First page plant" : `Other plant ${index}`,
        }));
        await page.route("**/change-requests/?*", (route) => {
            const second =
                new URL(route.request().url()).searchParams.get("page") === "2";
            if (second && !finalPending) {
                invalidPageRequests++;
                return route.fulfill({
                    status: 404,
                    json: errorBody("not_found", "Invalid page."),
                });
            }
            return route.fulfill({
                json: {
                    count: finalPending ? 51 : 50,
                    next:
                        !second && finalPending
                            ? "/api/tenants/1/change-requests/?scope=pending&page=2"
                            : null,
                    previous: second
                        ? "/api/tenants/1/change-requests/?scope=pending&page=1"
                        : null,
                    results: second ? [proposal(2)] : firstPage,
                },
            });
        });
        await page.route("**/change-requests/2/", (route) =>
            route.fulfill({ json: proposal(2) }),
        );
        await page.route("**/change-requests/2/approve/", (route) => {
            finalPending = false;
            return route.fulfill({
                json: { ...proposal(2), status: "approved", can_review: false },
            });
        });
        await page.goto("/app/");
        await page.getByRole("button", { name: "Inbox", exact: true }).click();
        await page.getByRole("button", { name: "Next", exact: true }).click();
        await expect(page.getByText("Page 2", { exact: true })).toBeVisible();
        await expect(page.getByRole("article")).toHaveCount(1);
        if (removal === "decision") {
            await page
                .getByRole("button", { name: "Review details", exact: true })
                .click();
            await page
                .getByRole("button", { name: "Approve and apply", exact: true })
                .click();
            await expect(
                page.getByText("First page plant", { exact: true }),
            ).toBeVisible();
            await expect(
                page.getByText("Approved", { exact: true }).locator(".."),
            ).toBeFocused();
            expect(invalidPageRequests).toBe(0);
        } else {
            finalPending = false;
            await page
                .getByRole("button", { name: "Refresh list", exact: true })
                .click();
            await expect(
                page.getByText("Invalid page.", { exact: true }),
            ).toBeVisible();
            await page
                .getByRole("button", { name: "Previous", exact: true })
                .click();
            await expect(
                page.getByText("First page plant", { exact: true }),
            ).toBeVisible();
            expect(invalidPageRequests).toBe(1);
        }
    });
}

test("a poll that was already reading cannot bring a decided request back to the first page", async ({
    page,
}) => {
    await page.clock.install({ time: new Date("2026-11-10T12:00:00Z") });
    await mockApi(page);
    let decided = false;
    let holding = false;
    let release!: () => void;
    const gate = new Promise<void>((resolve) => {
        release = resolve;
    });
    let reading!: () => void;
    const held = new Promise<void>((resolve) => {
        reading = resolve;
    });
    await page.route("**/change-requests/?*", async (route) => {
        const answer = paged(decided ? [] : [proposal(1)]);
        if (holding) {
            holding = false;
            reading();
            await gate;
        }
        await route.fulfill({ json: answer }).catch(() => {});
    });
    await page.route("**/change-requests/1/", (route) =>
        route.fulfill({ json: proposal(1) }),
    );
    await page.route("**/change-requests/1/approve/", (route) => {
        decided = true;
        return route.fulfill({
            json: { ...proposal(1), status: "approved", can_review: false },
        });
    });
    await page.goto("/app/");
    await page.getByRole("button", { name: /^Inbox/ }).click();
    await page
        .getByRole("button", { name: "Review details", exact: true })
        .click();
    holding = true;
    await page.clock.runFor(30_000);
    await held;
    await page
        .getByRole("button", { name: "Approve and apply", exact: true })
        .click();
    await expect(
        page.getByRole("status").filter({ hasText: /^Approved$/ }),
    ).toBeVisible();
    release();
    await expect(
        page.getByText("No submissions here.", { exact: true }),
    ).toBeVisible();
    await expect(page.locator(".inbox-card")).toHaveCount(0);
});

test("the inbox label counts several updates in the plural", async ({
    page,
}) => {
    await mockApi(page);
    await page.route("**/api/tenants/", (route) =>
        route.fulfill({
            json: paged([
                {
                    id: 1,
                    name: "Workspace",
                    role: "admin",
                    pending_review_count: 1,
                    unread_outcome_count: 1,
                },
            ]),
        }),
    );
    await page.goto("/app/");
    await expect(
        page.getByRole("button", { name: "Inbox, 2 updates", exact: true }),
    ).toBeVisible();
});

async function routeUnavailableRows(
    page: Page,
    reasons: (string | null)[],
    role: "member" | "admin",
) {
    await page.route("**/api/**/production/?*", (route) => {
        const date = new URL(route.request().url()).searchParams.get(
            "start_date",
        );
        if (date === null) throw new Error("The production read names no day.");
        const hour = (value: number) =>
            `${date}T${String(value).padStart(2, "0")}:00:00+03:00`;
        return route.fulfill({
            json: nativeProductionHistory(
                date,
                "Europe/Istanbul",
                reasons.map((reason, index) => ({
                    starts_at: hour(8 + index),
                    ends_at: hour(9 + index),
                    effective: reading,
                    ...(role === "member"
                        ? {
                              can_edit: false,
                              edit_unavailable_reason: "workspace_readonly",
                              can_propose: reason === null,
                              proposal_unavailable_reason: reason,
                          }
                        : {
                              can_edit: reason === null,
                              edit_unavailable_reason: reason,
                          }),
                })),
            ),
        });
    });
}

test("a member sees why an interval cannot be proposed", async ({ page }) => {
    await member(page);
    await routeUnavailableRows(
        page,
        [null, "interval_not_completed"],
        "member",
    );
    await openPlant(page, "Production");
    const rows = page.locator(".production-table tbody tr");
    await expect(
        rows.nth(0).getByRole("button", { name: /^Suggest correction for/ }),
    ).toBeVisible();
    await expect(rows.nth(1)).toContainText(
        "Only completed intervals can be changed.",
    );
});

test("a member sees one notice when no interval of the day can be proposed", async ({
    page,
}) => {
    await member(page);
    await routeUnavailableRows(
        page,
        [
            "configuration_not_current",
            "observation_overlap",
            "configuration_not_current",
        ],
        "member",
    );
    await openPlant(page, "Production");
    await expect(
        page.getByText(
            "Nothing on this day can be changed. Values recorded under an earlier plant configuration cannot be changed.",
        ),
    ).toBeVisible();
    await expect(
        page.getByRole("button", { name: /^Suggest correction for/ }),
    ).toHaveCount(0);
});

test("an administrator sees why an interval cannot be edited", async ({
    page,
}) => {
    await mockApi(page);
    await page.clock.setFixedTime(new Date("2026-11-10T12:00:00Z"));
    await routeUnavailableRows(page, [null, "configuration_mismatch"], "admin");
    await openPlant(page, "Production");
    const rows = page.locator(".production-table tbody tr");
    await expect(
        rows.nth(0).getByRole("button", { name: /^Edit / }),
    ).toBeVisible();
    await expect(rows.nth(1)).toContainText(
        "Values recorded under another plant configuration cannot be changed here.",
    );
});

test("review cards show the period, item count and outage title", async ({
    page,
}) => {
    await mockApi(page);
    await page.route("**/change-requests/?*", (route) =>
        route.fulfill({
            json: paged([
                {
                    ...proposal(1),
                    item_count: 2,
                    period_start: "2026-11-09T05:00:00Z",
                    period_end: "2026-11-09T06:00:00Z",
                    incident_action: null,
                    incident_title: null,
                },
                {
                    ...proposal(2),
                    kind: "incident",
                    production_items: [],
                    item_count: 1,
                    period_start: "2026-11-09T08:00:00Z",
                    period_end: null,
                    incident_action: "create",
                    incident_title: "Grid unavailable",
                },
            ]),
        }),
    );
    await page.goto("/app/");
    await page.getByRole("button", { name: "Inbox", exact: true }).click();
    const cards = page.locator(".inbox-card");
    await expect(cards.nth(0)).toContainText("2 intervals");
    await expect(
        cards.nth(0).locator('time[datetime="2026-11-09T05:00:00Z"]'),
    ).toHaveCount(1);
    await expect(
        cards.nth(0).locator('time[datetime="2026-11-09T06:00:00Z"]'),
    ).toHaveCount(1);
    await expect(cards.nth(1)).toContainText("New outage: Grid unavailable");
    await expect(cards.nth(1)).toContainText("Ongoing");
    await expect(
        cards.nth(1).locator('time[datetime="2026-11-09T08:00:00Z"]'),
    ).toHaveCount(1);
});

test("a member marks every decided request read at once", async ({ page }) => {
    await mockApi(page);
    let unread = 2;
    await page.route("**/api/tenants/", (route) =>
        route.fulfill({
            json: answer(
                "PaginatedTenantOutputList",
                paged([
                    tenantRow(
                        { id: 1, name: "Sunfield Energy", role: "viewer" },
                        { unread_outcome_count: unread },
                    ),
                ]),
            ),
        }),
    );
    await page.route(
        "**/api/tenants/1/change-requests/seen/",
        async (route) => {
            expect(route.request().method()).toBe("POST");
            unread = 0;
            await route.fulfill({
                json: answer("SeenAllOutput", { marked: 2 }),
            });
        },
    );
    await page.goto("/app/w/1/inbox/");

    await page.getByRole("button", { name: "Mark all as read" }).click();

    await expect(
        page.getByRole("button", { name: "Mark all as read" }),
    ).toHaveCount(0);
});

function dayRows(
    date: string,
    rows: Partial<Parameters<typeof nativeProductionHistory>[2][number]>[],
) {
    return nativeProductionHistory(
        date,
        "Europe/Istanbul",
        rows.map((row, index) => {
            const minutes = 8 * 60 + index * 30;
            const clock = (value: number) =>
                `${String(Math.floor(value / 60)).padStart(2, "0")}:${String(value % 60).padStart(2, "0")}`;
            return {
                starts_at: `${date}T${clock(minutes)}:00+03:00`,
                ends_at: `${date}T${clock(minutes + 30)}:00+03:00`,
                effective: reading,
                can_edit: false,
                can_propose: true,
                ...row,
            };
        }),
    );
}

test("a member stages several intervals of one day and sends them as one request", async ({
    page,
}) => {
    await member(page);
    const submissions: Record<string, unknown>[] = [];
    await page.route("**/api/**/production/?*", (route) => {
        const date = new URL(route.request().url()).searchParams.get(
            "start_date",
        )!;
        return route.fulfill({
            json: dayRows(date, [
                {},
                {},
                {
                    revision_id: 4,
                    can_propose_revert: true,
                    observation: {
                        id: 4,
                        configuration_id: 31,
                        starts_at: `${date}T09:00:00+03:00`,
                        ends_at: `${date}T09:30:00+03:00`,
                        energy_mwh: 1,
                        quality: "valid",
                        source: "meter",
                        recorded_by_id: 2,
                        created_at: `${date}T10:00:00Z`,
                        supersedes_id: null,
                        is_reverted: false,
                    },
                },
                {},
            ]),
        });
    });
    await page.route("**/change-requests/production/", (route) => {
        submissions.push(route.request().postDataJSON());
        return route.fulfill({ status: 201, json: proposal() });
    });
    await openPlant(page, "Production");
    await stage(page, "08:00", "0,5");
    await expect(
        page
            .getByRole("status")
            .filter({ hasText: /^Added 08:00.08:30 to the request\.$/ }),
    ).toBeVisible();
    await expect(
        page.getByRole("button", { name: "Suggest correction for 08:00" }),
    ).toBeFocused();
    await stage(page, "08:30", "0,6");
    await page
        .getByRole("button", { name: "Suggest correction for 09:00" })
        .click();
    await page
        .getByRole("button", { name: /^Request provider reading for 09:00/ })
        .click();
    await stage(page, "09:30", "0,8");
    const staged = draft(page);
    await expect(staged.getByRole("listitem")).toHaveCount(4);
    await expect(
        page.getByRole("button", { name: "Previous day" }),
    ).toBeDisabled();
    await expect(staged).toContainText(
        "Send or discard this request before choosing another day.",
    );

    await staged
        .getByRole("button", { name: /^Remove staged correction for 09:30/ })
        .click();
    await expect(staged.getByRole("listitem")).toHaveCount(3);
    await staged
        .getByRole("button", { name: /^Edit staged correction for 08:30/ })
        .click();
    const editor = page.locator(".production-row-editor");
    await expect(editor.getByRole("textbox").first()).toHaveValue("0.6");
    await editor.getByRole("textbox").first().fill("0,65");
    await editor
        .getByRole("button", { name: "Update in request", exact: true })
        .click();
    await staged.getByLabel("Reason for correction").fill("Meter read on site");
    await staged
        .getByRole("button", { name: "Send for approval", exact: true })
        .click();

    await expect(page.getByText(/^Sent for approval\./)).toBeVisible();
    await expect(staged).toHaveCount(0);
    await expect(
        page.getByRole("button", { name: "Previous day" }),
    ).toBeEnabled();
    expect(submissions).toHaveLength(1);
    expect(submissions[0]).toMatchObject({
        reason: "Meter read on site",
        unit: "MWh",
        changes: [
            {
                action: "set",
                energy: 0.5,
                starts_at: expect.stringContaining("T08:00:00"),
                expected_revision_id: null,
            },
            {
                action: "set",
                energy: 0.65,
                starts_at: expect.stringContaining("T08:30:00"),
            },
            {
                action: "revert",
                starts_at: expect.stringContaining("T09:00:00"),
                expected_revision_id: 4,
            },
        ],
    });
});

test("a refused request keeps the draft and names the concerned interval", async ({
    page,
}) => {
    await member(page);
    let revision: number | null = null;
    const answers = [
        {
            status: 409,
            json: errorBody(
                "production_revision_changed",
                "Production changed. Reload the latest values before saving.",
            ),
        },
        {
            status: 400,
            json: errorBody("invalid_request", "The request is invalid.", {
                fields: {
                    "changes.0.energy":
                        "Ensure this value is less than 1000000.",
                },
            }),
        },
        {
            status: 409,
            json: errorBody(
                "pending_request_limit",
                "You have too many pending submissions for this plant.",
            ),
        },
        { status: 201, json: proposal() },
    ];
    const submissions: Record<string, unknown>[] = [];
    await page.route("**/api/**/production/?*", (route) => {
        const date = new URL(route.request().url()).searchParams.get(
            "start_date",
        )!;
        return route.fulfill({
            json: dayRows(date, [{}, { revision_id: revision }]),
        });
    });
    await page.route("**/change-requests/production/", (route) => {
        submissions.push(route.request().postDataJSON());
        if (submissions.length === 1) revision = 8;
        return route.fulfill(answers[submissions.length - 1]!);
    });
    await openPlant(page, "Production");
    await stage(page, "08:00", "5");
    await stage(page, "08:30", "6");
    const staged = draft(page);
    await staged.getByLabel("Reason for correction").fill("Meter read on site");
    const send = staged.getByRole("button", {
        name: "Send for approval",
        exact: true,
    });

    await send.click();
    const second = staged.getByRole("listitem").nth(1);
    await expect(second).toContainText(
        "This interval changed after you added it. Edit or remove it.",
    );
    await expect(staged.getByRole("listitem").nth(0)).not.toContainText(
        "changed after you added it",
    );
    await staged
        .getByRole("button", { name: /^Edit staged correction for 08:30/ })
        .click();
    await page
        .locator(".production-row-editor")
        .getByRole("button", { name: "Update in request", exact: true })
        .click();
    await expect(second).not.toContainText("changed after you added it");

    await send.click();
    await expect(staged.getByRole("listitem").nth(0)).toContainText(
        "Ensure this value is less than 1000000.",
    );
    await send.click();
    await expect(staged).toContainText(
        "You have too many pending submissions for this plant.",
    );
    await expect(staged.getByRole("listitem")).toHaveCount(2);
    await expect(staged.getByLabel("Reason for correction")).toHaveValue(
        "Meter read on site",
    );

    await send.click();
    await expect(page.getByText(/^Sent for approval\./)).toBeVisible();
    expect(submissions).toHaveLength(4);
    expect(submissions[1]).toMatchObject({
        changes: [{ expected_revision_id: null }, { expected_revision_id: 8 }],
    });
    expect(submissions[0]?.idempotency_key).not.toBe(
        submissions[1]?.idempotency_key,
    );
});

test("a member sees which intervals wait for approval and opens the request", async ({
    page,
}) => {
    await member(page);
    await page.route("**/api/**/production/?*", (route) => {
        const date = new URL(route.request().url()).searchParams.get(
            "start_date",
        )!;
        return route.fulfill({
            json: dayRows(date, [{ pending_request_id: 7 }, {}]),
        });
    });
    await page.route("**/api/tenants/1/change-requests/7/", (route) =>
        route.fulfill({
            json: { ...proposal(7), can_review: false, can_withdraw: true },
        }),
    );
    await openPlant(page, "Production");
    const rows = page.locator(".production-table tbody tr");
    const marker = rows.nth(0).getByRole("link", {
        name: /^Waiting for approval: view the request for 08:00/,
    });
    await expect(marker).toHaveText("Waiting for approval – view");
    await expect(marker).toHaveAttribute("href", "/app/w/1/inbox/?request=7");
    await expect(
        rows.nth(0).getByRole("button", { name: /^Suggest correction for/ }),
    ).toBeVisible();
    await expect(rows.nth(1).getByRole("link")).toHaveCount(0);

    await marker.click();
    await expect(page).toHaveURL(/\/app\/w\/1\/inbox\/\?request=7$/);
    const linked = page.getByRole("region", { name: "Linked submission" });
    await expect(
        linked.getByRole("heading", { name: "Review details" }),
    ).toBeFocused();
    await expect(
        linked.getByRole("button", { name: "Withdraw submission" }),
    ).toBeVisible();
});

test("a member sees which outages wait for approval", async ({ page }) => {
    await member(page);
    await page.route("**/api/**/outages/", (route) =>
        route.fulfill({
            json: paged([
                {
                    id: 4,
                    revision: 2,
                    kind: "inverter",
                    title: "Inverter offline",
                    notes: "",
                    starts_at: "2026-11-09T08:00:00Z",
                    ends_at: null,
                    available_dc_mw: null,
                    available_inverter_mw: null,
                    available_export_mw: null,
                    status: "active",
                    declared_configuration_id: 31,
                    pending_request_id: 9,
                },
            ]),
        }),
    );
    await openPlant(page, "Outages");
    await expect(
        page.getByRole("link", {
            name: "Waiting for approval: view the request for Inverter offline",
        }),
    ).toHaveAttribute("href", "/app/w/1/inbox/?request=9");
});

test("an administrator who edits directly sees no proposal draft", async ({
    page,
}) => {
    await mockApi(page);
    await page.clock.setFixedTime(new Date("2026-11-10T12:00:00Z"));
    const writes: Record<string, unknown>[] = [];
    await page.route("**/api/**/production/?*", (route) => {
        const date = new URL(route.request().url()).searchParams.get(
            "start_date",
        )!;
        return route.fulfill({
            json: dayRows(date, [{ can_edit: true, can_propose: false }]),
        });
    });
    await page.route("**/production/changes/", (route) => {
        writes.push(route.request().postDataJSON());
        return route.fulfill({
            json: { created: 1, existing: 0, total: 1, ids: [5] },
        });
    });
    await openPlant(page, "Production");
    await page.getByRole("button", { name: /^Edit 08:00/ }).click();
    const editor = page.locator(".production-row-editor");
    await expect(
        editor.getByRole("button", { name: "Add to request" }),
    ).toHaveCount(0);
    await editor.getByRole("textbox").first().fill("0,5");
    await editor.getByRole("button", { name: "Save", exact: true }).click();
    await expect(page.getByText("Production saved.")).toBeVisible();
    expect(writes).toHaveLength(1);
    await expect(draft(page)).toHaveCount(0);
});
