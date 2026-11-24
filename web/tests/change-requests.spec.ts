import { test, expect, type Page } from "./support/test";
import { accessibilityViolations } from "./support/accessibility";
import {
    answer,
    mockApi,
    paged,
    sessionIdentity,
    tenantRow,
    type Mock,
} from "./support/workspace";
import type { components } from "../src/lib/schema";
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
    const account = await mockApi(page);
    await page.route("**/api/session/", (route) =>
        route.fulfill({
            json: { ...sessionIdentity(account.current), language },
        }),
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

type OutageRow = components["schemas"]["IncidentListOutput"];

function outagePage(results: Mock<OutageRow>[], reason: string | null = null) {
    return answer("IncidentPage", {
        ...paged(results),
        can_propose: reason === null,
        proposal_unavailable_reason: reason,
    });
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

test("member submits a native production correction, sent again after a refusal under its own key, and no live write", async ({
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
    expect(submissions[0]?.idempotency_key).not.toBe(
        submissions[1]?.idempotency_key,
    );
    expect({ ...submissions[1], idempotency_key: null }).toEqual({
        ...submissions[0],
        idempotency_key: null,
    });
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
    const outage: OutageRow = {
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
        created_at: "2026-11-09T08:10:00Z",
        updated_at: "2026-11-09T08:10:00Z",
        pending_request_id: null,
    };
    await page.route("**/api/**/outages/", (route) => {
        expect(route.request().method()).toBe("GET");
        return route.fulfill({ json: outagePage([outage]) });
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

test("Turkish member can withdraw and an opened outcome is marked read at 320px", async ({
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
    await expect(
        page.getByRole("button", { name: "Gelen kutusu", exact: true }),
    ).toBeVisible();
    expect(seen).toBe(1);
});

function decidedOwnRequest() {
    return {
        ...proposal(2),
        proposed_by: { id: 1, name: "solar.user" },
        status: "rejected",
        decision_reason: "Meter reading differs",
        can_review: false,
        can_withdraw: false,
        unread: true,
    };
}

async function memberWithOutcome(page: Page, seen: () => number) {
    await member(page);
    let outcome = decidedOwnRequest();
    let unread = 1;
    await page.route("**/api/tenants/", (route) =>
        route.fulfill({
            json: paged([
                tenantRow(
                    { id: 1, name: "Workspace", role: "viewer" },
                    { unread_outcome_count: unread },
                ),
            ]),
        }),
    );
    await page.route("**/change-requests/?*", (route) =>
        route.fulfill({ json: paged([outcome]) }),
    );
    await page.route("**/change-requests/2/", (route) =>
        route.fulfill({ json: outcome }),
    );
    await page.route("**/change-requests/2/seen/", async (route) => {
        expect(route.request().method()).toBe("POST");
        const status = seen();
        if (status === 200) {
            outcome = { ...outcome, unread: false };
            unread = 0;
            return route.fulfill({ json: outcome });
        }
        return route.fulfill({
            status,
            json: errorBody("server_error", "Server error."),
        });
    });
}

test("opening one's own decided request marks it read once", async ({
    page,
}) => {
    let posts = 0;
    await memberWithOutcome(page, () => {
        posts++;
        return 200;
    });
    await page.goto("/app/");
    await page
        .getByRole("button", { name: "Inbox, 1 update", exact: true })
        .click();
    const card = page.locator(".inbox-card").filter({ hasText: "Rejected" });
    await card.getByRole("button", { name: "Review details" }).click();

    await expect(
        page.getByRole("button", { name: "Inbox", exact: true }),
    ).toBeVisible();
    await expect(card.getByText("Meter reading differs")).toBeVisible();
    await expect(
        card.getByRole("heading", { name: "Review details" }),
    ).toBeFocused();
    await expect(card.getByText("Unread outcome")).toHaveCount(0);
    await card
        .getByRole("button", { name: "Close details", exact: true })
        .click();
    await card.getByRole("button", { name: "Review details" }).click();
    await expect(card.getByText("Meter reading differs")).toBeVisible();
    expect(posts).toBe(1);
});

test("a failed read receipt keeps the decided request readable", async ({
    page,
}) => {
    let posts = 0;
    await memberWithOutcome(page, () => {
        posts++;
        return 500;
    });
    await page.goto("/app/w/1/inbox/");
    const card = page.locator(".inbox-card").filter({ hasText: "Rejected" });
    await card.getByRole("button", { name: "Review details" }).click();

    await expect(card.getByText("Meter reading differs")).toBeVisible();
    await expect(
        card.getByRole("button", { name: "Mark as read", exact: true }),
    ).toBeVisible();
    await expect(
        page.getByRole("button", { name: "Inbox, 1 update", exact: true }),
    ).toBeVisible();
    expect(posts).toBe(1);
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
    await expect(
        page.getByRole("status").filter({
            hasText: /^Removed 09:30.10:00 from the request\.$/,
        }),
    ).toBeVisible();
    await staged
        .getByRole("button", { name: /^Edit staged correction for 08:30/ })
        .click();
    const editor = page.locator(".production-row-editor");
    await expect(editor.getByRole("textbox").first()).toHaveValue("0.6");
    await editor.getByRole("textbox").first().fill("0,65");
    await editor
        .getByRole("button", { name: "Update in request", exact: true })
        .click();
    await expect(
        page.getByRole("status").filter({
            hasText: /^Updated 08:30.09:00 in the request\.$/,
        }),
    ).toBeVisible();
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
        return route.fulfill(answers[submissions.length - 1]);
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
    ).toHaveCount(0);
    await expect(
        rows.nth(1).getByRole("button", { name: /^Suggest correction for/ }),
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

test("a waiting marker opened in a new tab leaves the page where it is", async ({
    page,
}) => {
    await member(page);
    await serveDays(page, () => [{ pending_request_id: 7 }, {}]);
    await openPlant(page, "Production");
    const marker = page.getByRole("link", {
        name: /^Waiting for approval: view the request for 08:00/,
    });
    await expect(marker).toBeVisible();
    const before = page.url();
    const opened = page.context().waitForEvent("page");
    await marker.click({ modifiers: ["ControlOrMeta"] });
    const tab = await opened;
    await tab.waitForURL(/\/app\/w\/1\/inbox\/\?request=7$/, {
        waitUntil: "commit",
    });
    await tab.close();
    expect(page.url()).toBe(before);
    await expect(
        page.getByRole("group", { name: "Production intervals" }),
    ).toBeVisible();
});

function proposalControls(page: Page, title: string) {
    const card = page.locator("article").filter({
        has: page.getByRole("heading", { name: title }),
    });
    return [
        card.getByRole("button", { name: "Suggest changes", exact: true }),
        card.getByRole("button", { name: "Submit resolution", exact: true }),
        card.getByText("Request cancellation", { exact: true }),
    ] as const;
}

test("a member sees which outages wait for approval", async ({ page }) => {
    await member(page);
    await page.route("**/api/**/outages/", (route) =>
        route.fulfill({
            json: outagePage([
                { ...activeOutage(), pending_request_id: 9 },
                { ...activeOutage(), id: 5, title: "Export limited" },
            ]),
        }),
    );
    await openPlant(page, "Outages");
    await expect(
        page.getByRole("link", {
            name: "Waiting for approval: view the request for Inverter offline",
        }),
    ).toHaveAttribute("href", "/app/w/1/inbox/?request=9");
    for (const control of proposalControls(page, "Inverter offline"))
        await expect(control).toHaveCount(0);
    for (const control of proposalControls(page, "Export limited"))
        await expect(control).toBeVisible();
});

function activeOutage(): OutageRow {
    return {
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
        created_at: "2026-11-09T08:10:00Z",
        updated_at: "2026-11-09T08:10:00Z",
        pending_request_id: null,
    };
}

for (const [reason, explanation] of [
    [
        "configuration_unavailable",
        "Outages can be reported once the plant configuration is approved.",
    ],
    [
        "pending_request_limit",
        "Too many of your requests for this plant await approval. Withdraw one or wait for a decision before proposing another outage or a change to one.",
    ],
] as const) {
    test(`a member sees why a new outage cannot be submitted: ${reason}`, async ({
        page,
    }) => {
        await member(page);
        await page.route("**/api/**/outages/", (route) =>
            route.fulfill({ json: outagePage([activeOutage()], reason) }),
        );
        await openPlant(page, "Outages");
        await expect(page.getByText(explanation)).toBeVisible();
        await expect(
            page.getByRole("button", { name: "Submit outage", exact: true }),
        ).toHaveCount(0);
        await expect(
            page.getByRole("heading", { name: "Inverter offline" }),
        ).toBeVisible();
        const [change, resolution, cancellation] = proposalControls(
            page,
            "Inverter offline",
        );
        if (reason === "pending_request_limit")
            for (const control of [change, resolution, cancellation])
                await expect(control).toHaveCount(0);
        else await expect(resolution).toBeVisible();
    });
}

test("the sent notice of an outage proposal clears on the next action", async ({
    page,
}) => {
    await member(page);
    await page.route("**/api/**/outages/", (route) =>
        route.fulfill({ json: outagePage([activeOutage()]) }),
    );
    await page.route("**/change-requests/incidents/", (route) =>
        route.fulfill({ status: 201, json: proposal() }),
    );
    await openPlant(page, "Outages");
    await page
        .getByRole("button", { name: "Suggest changes", exact: true })
        .click();
    await page.getByLabel("Incident title").fill("Corrected title");
    await page
        .getByRole("button", { name: "Send for approval", exact: true })
        .click();
    const sent = page.getByText(/^Sent for approval\./);
    await expect(sent).toBeVisible();

    await page
        .getByRole("button", { name: "Submit resolution", exact: true })
        .click();
    await expect(sent).toHaveCount(0);
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

async function serveDays(
    page: Page,
    rows: () => Parameters<typeof dayRows>[1],
) {
    await page.route("**/api/**/production/?*", (route) => {
        const date = new URL(route.request().url()).searchParams.get(
            "start_date",
        )!;
        return route.fulfill({ json: dayRows(date, rows()) });
    });
}

function showDay(page: Page, date: string) {
    return page.evaluate((date) => {
        const url = new URL(window.location.href);
        url.searchParams.set("date", date);
        window.history.pushState(null, "", url);
    }, date);
}

// A full page load drops an unsent draft, so leaving the page is confirmed first.
function leavingIsConfirmed(page: Page) {
    return page.evaluate(() => {
        const leaving = new Event("beforeunload", { cancelable: true });
        window.dispatchEvent(leaving);
        return leaving.defaultPrevented;
    });
}

test("a staged interval that comes to wait for the member's own request is named and cannot be edited", async ({
    page,
}) => {
    await member(page);
    let pending: number | null = null;
    await serveDays(page, () => [{ pending_request_id: pending }, {}]);
    await openPlant(page, "Production");
    await stage(page, "08:00", "0,5");
    pending = 7;
    await page.locator(".production-refresh").click();

    const rows = page.locator(".production-table tbody tr");
    await expect(
        rows.nth(0).getByRole("link", {
            name: /^Waiting for approval: view the request for 08:00/,
        }),
    ).toBeVisible();
    await expect(
        rows.nth(0).getByRole("button", { name: /^Suggest correction for/ }),
    ).toHaveCount(0);
    const item = draft(page).getByRole("listitem");
    await expect(item).toContainText(
        "A request of yours for this interval already waits for approval. Remove it here, or withdraw that request first.",
    );
    await expect(
        item.getByRole("button", { name: /^Edit staged correction for 08:00/ }),
    ).toBeDisabled();
});

test("editing a staged interval outside daylight shows its row and discarding closes the editor", async ({
    page,
}) => {
    await member(page);
    await serveDays(page, () => [{ effective: null }, {}, {}]);
    await openPlant(page, "Production");
    const fullDay = page.getByRole("checkbox", { name: "Show full day" });
    const night = page.getByRole("button", {
        name: "Suggest correction for 08:00",
    });
    await expect(night).toHaveCount(0);
    await fullDay.check();
    await stage(page, "08:00", "0,4");
    await fullDay.uncheck();
    await expect(night).toHaveCount(0);
    const staged = draft(page);
    const edit = staged.getByRole("button", {
        name: /^Edit staged correction for 08:00/,
    });

    await edit.click();
    const editor = page.locator(".production-row-editor");
    await expect(editor.getByRole("textbox").first()).toHaveValue("0.4");
    await expect(fullDay).toBeChecked();
    await editor.getByRole("button", { name: "Cancel", exact: true }).click();
    await expect(night).toBeFocused();

    await edit.click();
    await expect(editor).toHaveCount(1);
    await staged
        .getByRole("button", { name: "Discard request", exact: true })
        .click();
    await expect(page.getByText("Correction request discarded.")).toBeVisible();
    await expect(editor).toHaveCount(0);
    await expect(fullDay).toBeEnabled();
    await expect(night).toBeEnabled();
    await expect(
        page.getByRole("button", { name: "Previous day" }),
    ).toBeEnabled();
});

test("a reason of only spaces is refused at its field and a refused reason is shown there", async ({
    page,
}) => {
    await member(page);
    const submissions: unknown[] = [];
    await serveDays(page, () => [{}, {}]);
    await page.route("**/change-requests/production/", (route) => {
        submissions.push(route.request().postDataJSON());
        return route.fulfill({
            status: 400,
            json: errorBody(
                "invalid_request",
                "Check the highlighted values and try again.",
                {
                    fields: {
                        reason: "This field may not be blank.",
                        changes: "Submit 1 to 100 production intervals.",
                    },
                },
            ),
        });
    });
    await openPlant(page, "Production");
    await stage(page, "08:00", "0,5");
    const staged = draft(page);
    const reason = staged.getByLabel("Reason for correction");
    const send = staged.getByRole("button", {
        name: "Send for approval",
        exact: true,
    });

    await reason.fill("   ");
    await send.click();
    await expect(reason).toBeFocused();
    await expect(reason).toHaveAttribute("aria-invalid", "true");
    await expect(reason).toHaveAccessibleDescription(
        /Enter a reason for this correction\./,
    );
    expect(submissions).toHaveLength(0);

    await reason.fill("Meter read on site");
    await expect(reason).not.toHaveAttribute("aria-invalid", "true");
    await send.click();
    await expect(staged.getByRole("alert")).toContainText(
        "Submit 1 to 100 production intervals.",
    );
    await expect(reason).toHaveAttribute("aria-invalid", "true");
    await expect(reason).toHaveAccessibleDescription(
        /This field may not be blank\./,
    );
    expect(submissions).toHaveLength(1);
});

test("a reason of only spaces is refused at its field in Turkish", async ({
    page,
}) => {
    await member(page, "tr");
    const submissions: unknown[] = [];
    await serveDays(page, () => [{}, {}]);
    await page.route("**/change-requests/production/", (route) => {
        submissions.push(route.request().postDataJSON());
        return route.fulfill({ status: 201, json: proposal() });
    });
    await openPlant(page, "Üretim");
    await page
        .getByRole("button", {
            name: /^08:00.08:30 aralığı için düzeltme öner/,
        })
        .click();
    const editor = page.locator(".production-row-editor");
    await editor.getByRole("textbox").first().fill("0,5");
    await editor
        .getByRole("button", { name: "Talebe ekle", exact: true })
        .click();
    const staged = page.getByRole("region", {
        name: "Düzeltme talebi",
        exact: true,
    });
    const reason = staged.getByLabel("Düzeltme gerekçesi");

    await reason.fill("  ");
    await staged
        .getByRole("button", { name: "Onaya gönder", exact: true })
        .click();
    await expect(reason).toBeFocused();
    await expect(reason).toHaveAttribute("aria-invalid", "true");
    await expect(reason).toHaveAccessibleDescription(
        /Bu düzeltme için gerekçe girin\./,
    );
    expect(submissions).toHaveLength(0);
});

test("a draft stays with its day when Back shows another day", async ({
    page,
}) => {
    await member(page);
    await serveDays(page, () => [{}, {}]);
    await openPlant(page, "Production");
    const day = page.getByLabel("Day", { exact: true });
    await expect(day).toHaveValue("2026-11-09");
    await showDay(page, "2026-11-08");
    await expect(day).toHaveValue("2026-11-08");
    await stage(page, "08:00", "0,5");
    const staged = draft(page);
    const send = staged.getByRole("button", {
        name: "Send for approval",
        exact: true,
    });

    await page.goBack();
    await expect(day).toHaveValue("2026-11-09");
    await expect(staged).toContainText(
        "This request is for 2026-11-08. Return to that day to send it, or discard it.",
    );
    await expect(staged.getByRole("listitem")).toHaveCount(1);
    await expect(send).toBeDisabled();
    await expect(
        page.getByRole("button", { name: "Suggest correction for 08:00" }),
    ).toBeDisabled();

    await staged
        .getByRole("button", { name: "Return to 2026-11-08", exact: true })
        .click();
    await expect(day).toHaveValue("2026-11-08");
    await expect(staged).not.toContainText("This request is for");
    await expect(staged.getByRole("listitem")).toHaveCount(1);
    await expect(send).toBeEnabled();
});

test("a draft stays with its day when a link shows another day", async ({
    page,
}) => {
    await member(page);
    await serveDays(page, () => [{}, {}]);
    await openPlant(page, "Production");
    const day = page.getByLabel("Day", { exact: true });
    await stage(page, "08:00", "0,5");
    const staged = draft(page);
    await staged.getByLabel("Reason for correction").fill("Meter read on site");
    expect(await leavingIsConfirmed(page)).toBe(true);

    await showDay(page, "2026-11-07");
    await expect(day).toHaveValue("2026-11-07");
    await expect(staged).toContainText(
        "This request is for 2026-11-09. Return to that day to send it, or discard it.",
    );
    await expect(
        staged.getByRole("button", { name: "Send for approval", exact: true }),
    ).toBeDisabled();
    await expect(
        page.getByRole("button", { name: "Suggest correction for 08:00" }),
    ).toBeDisabled();

    await staged
        .getByRole("button", { name: "Discard request", exact: true })
        .click();
    await expect(page.getByText("Correction request discarded.")).toBeVisible();
    await expect(staged).toHaveCount(0);
    await expect(day).toHaveValue("2026-11-07");
    await expect(
        page.getByRole("button", { name: "Previous day" }),
    ).toBeEnabled();
    expect(await leavingIsConfirmed(page)).toBe(false);
    await stage(page, "08:00", "0,6");
    await expect(draft(page).getByLabel("Reason for correction")).toHaveValue(
        "",
    );
});

// A member whose clock runs, so reads become stale and polls and focus reads happen.
async function memberOnClock(
    page: Page,
    role: () => "viewer" | "admin" = () => "viewer",
) {
    await page.clock.install({ time: new Date("2026-11-10T12:00:00Z") });
    const account = await mockApi(page);
    await page.route("**/api/session/", (route) =>
        route.fulfill({
            json: { ...sessionIdentity(account.current), language: "en" },
        }),
    );
    await page.route("**/api/tenants/", (route) =>
        route.fulfill({
            json: paged([
                tenantRow({ id: 1, name: "Workspace", role: role() }),
            ]),
        }),
    );
    await page.route("**/change-requests/?*", (route) =>
        route.fulfill({ json: paged([]) }),
    );
}

async function readOnFocus(page: Page) {
    await page.clock.runFor(5_000);
    await page.evaluate(() =>
        document.dispatchEvent(
            new Event("visibilitychange", { bubbles: true }),
        ),
    );
}

test("a member promoted while a draft is open can choose another day", async ({
    page,
}) => {
    let role: "viewer" | "admin" = "viewer";
    await memberOnClock(page, () => role);
    await serveDays(page, () => [{}, {}]);
    await openPlant(page, "Production");
    await stage(page, "08:00", "0,5");
    const previous = page.getByRole("button", { name: "Previous day" });
    await expect(previous).toBeDisabled();

    role = "admin";
    await page.clock.runFor(120_001);
    await expect(draft(page)).toHaveCount(0);
    await expect(
        page.getByRole("status").filter({
            hasText:
                "You can now edit production directly, so the correction request you were preparing was discarded.",
        }),
    ).toHaveCount(1);
    await expect(previous).toBeEnabled();
    await previous.click();
    await expect(page.getByLabel("Day", { exact: true })).toHaveValue(
        "2026-11-08",
    );
});

test("the notice of a promotion that discarded a draft goes with the next access change and a second such promotion is announced again", async ({
    page,
}) => {
    const promoted =
        "You can now edit production directly, so the correction request you were preparing was discarded.";
    let role: "viewer" | "admin" = "viewer";
    await memberOnClock(page, () => role);
    await serveDays(page, () => [{}, {}]);
    await openPlant(page, "Production");
    await stage(page, "08:00", "0,5");
    role = "admin";
    await page.clock.runFor(120_001);
    const notice = page.getByRole("status").filter({ hasText: promoted });
    await expect(notice).toHaveCount(1);

    role = "viewer";
    await page.clock.runFor(120_001);
    await expect(
        page.getByRole("button", { name: "Suggest correction for 08:00" }),
    ).toBeVisible();
    await expect(notice).toHaveCount(0);

    await stage(page, "08:00", "0,5");
    role = "admin";
    await page.clock.runFor(120_001);
    await expect(draft(page)).toHaveCount(0);
    await expect(notice).toHaveCount(1);
});

test("the manual mark as read recovers a failed read receipt", async ({
    page,
}) => {
    let posts = 0;
    await memberWithOutcome(page, () => (++posts === 1 ? 500 : 200));
    await page.goto("/app/w/1/inbox/");
    const card = page.locator(".inbox-card").filter({ hasText: "Rejected" });
    await card.getByRole("button", { name: "Review details" }).click();
    await expect.poll(() => posts).toBe(1);

    const mark = card.getByRole("button", {
        name: "Mark as read",
        exact: true,
    });
    await mark.click();
    await expect(mark).toHaveCount(0);
    await expect(
        page.getByRole("button", { name: "Inbox", exact: true }),
    ).toBeVisible();
    expect(posts).toBe(2);
});

test("a draft holding an interval that comes to wait for the member's own request is sent only once that interval is removed", async ({
    page,
}) => {
    await member(page);
    let pending: number | null = null;
    const submissions: Record<string, unknown>[] = [];
    await serveDays(page, () => [{ pending_request_id: pending }, {}]);
    await page.route("**/change-requests/production/", (route) => {
        submissions.push(route.request().postDataJSON());
        return route.fulfill({ status: 201, json: proposal() });
    });
    await openPlant(page, "Production");
    await stage(page, "08:00", "0,5");
    await stage(page, "08:30", "0,6");
    const staged = draft(page);
    await staged.getByLabel("Reason for correction").fill("Meter read on site");
    pending = 7;
    await page.locator(".production-refresh").click();

    const send = staged.getByRole("button", {
        name: "Send for approval",
        exact: true,
    });
    await expect(staged.getByRole("listitem").nth(0)).toContainText(
        "A request of yours for this interval already waits for approval.",
    );
    await expect(send).toBeDisabled();
    await expect(send).toHaveAccessibleDescription(
        "Intervals that already wait for a request of yours cannot be sent again. Remove them to send this request.",
    );
    await staged
        .getByRole("button", { name: /^Remove staged correction for 08:00/ })
        .click();
    await expect(send).toBeEnabled();
    await send.click();
    await expect(page.getByText(/^Sent for approval\./)).toBeVisible();
    expect(submissions).toHaveLength(1);
    expect(submissions[0]).toMatchObject({
        changes: [{ starts_at: expect.stringContaining("T08:30:00") }],
    });
});

test("while a request is being sent nothing can be staged, edited or removed", async ({
    page,
}) => {
    await member(page);
    await serveDays(page, () => [{}, {}]);
    let answer: () => void = () => undefined;
    const answered = new Promise<void>((resolve) => {
        answer = resolve;
    });
    const submissions: unknown[] = [];
    await page.route("**/change-requests/production/", async (route) => {
        submissions.push(route.request().postDataJSON());
        await answered;
        await route.fulfill({
            status: 400,
            json: errorBody("invalid_request", "The request is invalid.", {
                fields: {
                    "changes.0.energy":
                        "Ensure this value is less than 1000000.",
                },
            }),
        });
    });
    await openPlant(page, "Production");
    await stage(page, "08:00", "5");
    const staged = draft(page);
    await staged.getByLabel("Reason for correction").fill("Meter read on site");
    await staged
        .getByRole("button", { name: "Send for approval", exact: true })
        .click();

    const other = page.getByRole("button", {
        name: "Suggest correction for 08:30",
    });
    await expect(
        staged.getByRole("button", { name: "Sending…", exact: true }),
    ).toBeDisabled();
    await expect(other).toBeDisabled();
    for (const name of [
        /^Edit staged correction for 08:00/,
        /^Remove staged correction for 08:00/,
        /^Discard request$/,
    ])
        await expect(staged.getByRole("button", { name })).toBeDisabled();
    await expect(staged.getByLabel("Reason for correction")).toBeDisabled();

    answer();
    await expect(staged.getByRole("listitem").nth(0)).toContainText(
        "Ensure this value is less than 1000000.",
    );
    await expect(other).toBeEnabled();
    expect(submissions).toHaveLength(1);
});

test("an open editor whose interval comes to wait for the member's own request says so and can only be closed", async ({
    page,
}) => {
    await memberOnClock(page);
    let pending: number | null = null;
    await serveDays(page, () => [{ pending_request_id: pending }, {}]);
    await openPlant(page, "Production");
    await page
        .getByRole("button", { name: "Suggest correction for 08:00" })
        .click();
    const editor = page.locator(".production-row-editor");
    await expect(
        editor.getByRole("button", { name: "Add to request", exact: true }),
    ).toBeVisible();

    pending = 7;
    await readOnFocus(page);
    await expect(editor.getByRole("alert")).toHaveText(
        "A request of yours for this interval now waits for approval. Your attempted value is preserved below. Cancel to choose another interval.",
    );
    await expect(
        editor.getByRole("button", { name: "Add to request", exact: true }),
    ).toHaveCount(0);
    await editor.getByRole("button", { name: "Cancel", exact: true }).click();
    await expect(editor).toHaveCount(0);
    await expect(
        page.getByRole("link", {
            name: /^Waiting for approval: view the request for 08:00/,
        }),
    ).toBeVisible();
    await expect(
        page.getByRole("group", { name: "Production intervals" }),
    ).toBeFocused();
});

test("an open outage editor cannot send once its outage waits for the member's own request or the limit is reached", async ({
    page,
}) => {
    await memberOnClock(page);
    let held: number | null = null;
    let reason: string | null = null;
    const submissions: unknown[] = [];
    await page.route("**/api/**/outages/", (route) =>
        route.fulfill({
            json: outagePage(
                [
                    { ...activeOutage(), pending_request_id: held },
                    { ...activeOutage(), id: 5, title: "Export limited" },
                ],
                reason,
            ),
        }),
    );
    await page.route("**/change-requests/incidents/", (route) => {
        submissions.push(route.request().postDataJSON());
        return route.fulfill({ status: 201, json: proposal() });
    });
    await openPlant(page, "Outages");
    const inverter = proposalControls(page, "Inverter offline");
    await inverter[0].click();
    const editor = page.getByRole("region", { name: "Edit outage" });
    await expect(editor.getByLabel("Incident title")).toHaveValue(
        "Inverter offline",
    );
    for (const control of inverter) await expect(control).toHaveCount(0);
    for (const control of proposalControls(page, "Export limited"))
        await expect(control).toBeVisible();

    held = 9;
    await readOnFocus(page);
    await expect(editor.getByRole("status")).toHaveText(
        "A request of yours for this outage now waits for approval. Withdraw it before proposing another change.",
    );
    await expect(
        editor.getByRole("button", { name: "Send for approval", exact: true }),
    ).toHaveCount(0);
    await editor
        .getByRole("button", { name: "Close editor", exact: true })
        .click();
    await expect(editor).toHaveCount(0);

    held = null;
    await readOnFocus(page);
    await proposalControls(page, "Export limited")[0].click();
    await expect(editor.getByLabel("Incident title")).toHaveValue(
        "Export limited",
    );
    reason = "pending_request_limit";
    await readOnFocus(page);
    await expect(editor.getByRole("status")).toHaveText(
        "Too many of your requests for this plant await approval. Withdraw one or wait for a decision before proposing another outage or a change to one.",
    );
    await expect(
        editor.getByRole("button", { name: "Send for approval", exact: true }),
    ).toHaveCount(0);
    await editor
        .getByRole("button", { name: "Close editor", exact: true })
        .click();
    await expect(editor).toHaveCount(0);
    expect(submissions).toHaveLength(0);
});

test("an editor closes when a link shows a day whose read fails, and refresh reads that day again", async ({
    page,
}) => {
    await member(page);
    let failing = true;
    await page.route("**/api/**/production/?*", (route) => {
        const date = new URL(route.request().url()).searchParams.get(
            "start_date",
        )!;
        return date === "2026-11-07" && failing
            ? route.fulfill({
                  status: 503,
                  json: errorBody("busy", "Production is busy."),
              })
            : route.fulfill({ json: dayRows(date, [{}, {}]) });
    });
    await openPlant(page, "Production");
    await page
        .getByRole("button", { name: "Suggest correction for 08:00" })
        .click();
    const editor = page.locator(".production-row-editor");
    await expect(editor).toHaveCount(1);

    await showDay(page, "2026-11-07");
    await expect(page.getByLabel("Day", { exact: true })).toHaveValue(
        "2026-11-07",
    );
    const failure = page.getByRole("main").getByRole("alert");
    await expect(failure.locator("p")).toHaveText("Production is busy.");
    await expect(failure.getByRole("button")).toHaveText("Try again");
    await expect(editor).toHaveCount(0);
    const refresh = page.locator(".production-refresh");
    await expect(refresh).toBeEnabled();
    failing = false;
    await refresh.click();
    await expect(
        page.getByRole("button", { name: "Suggest correction for 08:00" }),
    ).toBeEnabled();
    await expect(editor).toHaveCount(0);
});
