import { test, expect } from "./support/test";
import { secondAccount, secondUser } from "./support/session";
import {
    answer,
    firstAccount,
    mockApi,
    paged,
    sessionIdentity,
    tenantRow,
} from "./support/workspace";
import { allauthErrorBody, errorBody } from "./support/errors";
import type { components } from "../src/lib/schema";

test("Turkish workspace roles and invitation labels preserve readable characters", async ({
    page,
}) => {
    const account = await mockApi(page);
    let role = "admin";
    await page.route("**/api/session/", (route) =>
        route.fulfill({
            json: { ...sessionIdentity(account.current), language: "tr" },
        }),
    );
    await page.route("**/api/tenants/", (route) =>
        route.fulfill({
            json: paged([{ id: 1, name: "Sunfield Energy", role }]),
        }),
    );
    await page.route("**/api/tenants/1/members/", (route) =>
        route.fulfill({
            json: {
                members: [
                    { id: 2, email: "viewer@example.test", role: "viewer" },
                ],
            },
        }),
    );
    await page.route("**/api/tenants/1/invitations/", (route) =>
        route.fulfill({ json: { invitations: [] } }),
    );
    await page.goto("/app/");
    await page.getByRole("button", { name: "Üyeler", exact: true }).click();
    await expect(
        page.getByRole("heading", {
            name: "Üyeler",
            exact: true,
            level: 1,
        }),
    ).toBeVisible();
    await expect(
        page.getByRole("heading", { name: "Üye davet et", exact: true }),
    ).toBeVisible();
    await expect(
        page.getByRole("button", { name: "Davet gönder", exact: true }),
    ).toBeVisible();
    await expect(page.getByText("Üye", { exact: true })).toBeVisible();
    role = "viewer";
    await page.reload();
    await expect(
        page.locator(".workspace-readonly", { hasText: /^Üye$/ }),
    ).toBeVisible();
    await expect(
        page.getByRole("button", {
            name: "Üyeler",
            exact: true,
        }),
    ).toHaveCount(0);
});

test("viewer reads shared settings without write controls or requests", async ({
    page,
}) => {
    await mockApi(page);
    await page.route("**/api/tenants/", (route) =>
        route.fulfill({
            json: paged([
                tenantRow({ id: 1, name: "Sunfield Energy", role: "viewer" }),
            ]),
        }),
    );
    await page.route("**/api/**/outages/", (route) =>
        route.fulfill({
            json: {
                ...paged([]),
                can_propose: true,
                proposal_unavailable_reason: null,
            },
        }),
    );
    await page.route("**/api/**/calibration/automation/", (route) =>
        route.fulfill({
            json: answer("CalibrationAutomationOutput", {
                enabled: true,
                authorized: true,
                configuration_id: 31,
                configuration_current: true,
                next_check_at: null,
                disabled_cause: null,
                current_calibration: {
                    activation_id: 3,
                    calibration_id: 40,
                    factor: 1.1,
                    actor_id: 1,
                    origin: "manual",
                    created_at: "2026-01-12T09:00:00Z",
                    effective: true,
                },
                last_attempt: null,
            }),
        }),
    );
    const writes: string[] = [];
    page.on("request", (request) => {
        if (
            request.url().includes("/api/tenants/") &&
            request.method() !== "GET"
        )
            writes.push(request.url());
    });
    await page.goto("/app/");
    await expect(page.getByRole("heading", { name: "Plants" })).toBeVisible();
    await expect(
        page.getByRole("button", { name: "Add plant", exact: true }),
    ).toHaveCount(0);
    await expect(page.getByRole("button", { name: "Members" })).toHaveCount(0);
    await expect(
        page.getByRole("button", { name: "Recycle bin", exact: true }),
    ).toHaveCount(0);
    await page
        .getByRole("button", { name: /East array|Antalya solar park/ })
        .click();
    await page.getByRole("button", { name: "Settings", exact: true }).click();
    await expect(
        page.getByRole("heading", { name: "Technical details" }),
    ).toBeVisible();
    await expect(
        page.getByRole("button", { name: "Save configuration" }),
    ).toHaveCount(0);
    await expect(
        page.getByRole("button", { name: "Delete plant", exact: true }),
    ).toHaveCount(0);
    await expect(page.locator(".readonly-form input").first()).toBeDisabled();
    await page
        .getByRole("button", { name: "Email reminders", exact: true })
        .click();
    await expect(page.locator(".readonly-form input").first()).toBeDisabled();
    await expect(page.locator(".readonly-form button:visible")).toHaveCount(0);
    await page.getByRole("button", { name: "Outages", exact: true }).click();
    await expect(
        page.getByRole("button", { name: "Report outage", exact: true }),
    ).toBeVisible();
    await page
        .getByRole("button", { name: "Calibration", exact: true })
        .click();
    await expect(
        page.getByRole("button", { name: "Reset to baseline" }),
    ).toHaveCount(0);
    await expect(
        page.getByRole("checkbox", { name: "Automatic calibration enabled" }),
    ).toBeDisabled();
    await page.getByRole("button", { name: "Account", exact: true }).click();
    await page.getByText("Change email", { exact: true }).click();
    await expect(
        page.getByRole("button", { name: "Send email code" }),
    ).toBeVisible();
    expect(writes).toEqual([]);
});

test("workspace administrator invites, sends again and cancels with the server's answers", async ({
    page,
}, testInfo) => {
    await mockApi(page);
    await page.setViewportSize({ width: 390, height: 844 });
    type Invitation = components["schemas"]["InvitationOutput"];
    const sentAt = "2026-11-20T09:00:00Z";
    const validUntil = "2026-11-27T09:00:00Z";
    let rows: Invitation[] = [
        {
            id: 3,
            email: "late@example.test",
            role: "viewer",
            state: "expired",
            created_at: "2026-11-01T09:00:00Z",
            expires_at: "2026-11-08T09:00:00Z",
            delivery_status: "sent",
        },
    ];
    let next = 4;
    const issue = (email: string): Invitation => ({
        id: next++,
        email,
        role: "viewer",
        state: "pending",
        created_at: sentAt,
        expires_at: validUntil,
        delivery_status: "pending",
    });
    const invitationId = (url: string) =>
        Number(new URL(url).pathname.split("/")[5]);
    await page.route("**/api/tenants/1/members/", (route) =>
        route.fulfill({
            json: answer("WorkspaceMembersOutput", {
                members: [
                    { id: 1, email: "owner@example.test", role: "admin" },
                ],
                plants_at_recipient_limit: 0,
            }),
        }),
    );
    await page.route("**/api/tenants/1/invitations/", async (route) => {
        if (route.request().method() === "POST") {
            expect(route.request().headers()["x-csrftoken"]).toBe(
                "signed-token",
            );
            const { email } = route.request().postDataJSON();
            if (rows.some((row) => row.email === email))
                return route.fulfill({
                    status: 429,
                    headers: { "Retry-After": "60" },
                    json: errorBody(
                        "invitation_rate_limited",
                        "Too many invitations were sent. Try again later.",
                        { retry_after: 60 },
                    ),
                });
            const created = issue(email);
            rows = [created, ...rows];
            return route.fulfill({ status: 201, json: created });
        }
        return route.fulfill({
            json: answer("WorkspaceInvitationsOutput", { invitations: rows }),
        });
    });
    await page.route("**/api/tenants/1/invitations/*/resend/", (route) => {
        expect(route.request().method()).toBe("POST");
        expect(route.request().headers()["x-csrftoken"]).toBe("signed-token");
        const id = invitationId(route.request().url());
        const created = issue(rows.find((row) => row.id === id)!.email);
        rows = [created, ...rows.filter((row) => row.id !== id)];
        return route.fulfill({ status: 201, json: created });
    });
    await page.route(/\/api\/tenants\/1\/invitations\/\d+\/$/, (route) => {
        expect(route.request().method()).toBe("DELETE");
        expect(route.request().headers()["x-csrftoken"]).toBe("signed-token");
        const id = invitationId(route.request().url());
        rows = rows.filter((row) => row.id !== id);
        return route.fulfill({ status: 204 });
    });
    const row = (email: string) =>
        page.locator(".workspace-member", { hasText: email });
    await page.goto("/app/");
    await page.getByRole("button", { name: "Members", exact: true }).click();
    await expect(page.getByText("owner@example.test")).toBeVisible();
    await expect(
        row("late@example.test").getByText("Expired", { exact: true }),
    ).toBeVisible();
    await page
        .getByLabel("Email address", { exact: true })
        .fill("viewer@example.test");
    await page
        .getByRole("button", { name: "Send invitation", exact: true })
        .click();
    await expect(
        row("viewer@example.test").getByText("Pending", { exact: true }),
    ).toBeVisible();
    await page
        .getByRole("button", { name: "Send invitation", exact: true })
        .click();
    await expect(page.locator(".form-errors")).toContainText(
        "Too many invitations were sent. Try again later.",
    );
    await page
        .getByRole("button", {
            name: "Send again: late@example.test",
            exact: true,
        })
        .click();
    await expect(page.locator("[data-live-region]")).toHaveText(
        "A new invitation is being sent to late@example.test.",
    );
    await expect(
        row("late@example.test").getByText("Pending", { exact: true }),
    ).toBeVisible();
    await page.screenshot({
        path: testInfo.outputPath("members-mobile.png"),
        fullPage: true,
    });
    expect(
        await page.evaluate(
            () => document.documentElement.scrollWidth <= window.innerWidth,
        ),
    ).toBe(true);
    await page
        .getByRole("button", {
            name: "Withdraw invitation: viewer@example.test",
            exact: true,
        })
        .click();
    await expect(page.locator("[data-live-region]")).toHaveText(
        "Invitation withdrawn.",
    );
    await expect(row("viewer@example.test")).toHaveCount(0);
    await expect(row("late@example.test")).toHaveCount(1);
});

test("invitation signup preserves token without creating another workspace", async ({
    page,
}) => {
    const account = await mockApi(page, { anonymous: true });
    page.on("request", (request) => {
        expect(request.url()).not.toContain("invite-secret");
        expect(request.headers()["referer"] ?? "").not.toContain(
            "invite-secret",
        );
    });
    let payload: unknown;
    await page.route("**/api/auth/browser/v1/auth/session", (route) =>
        account.current
            ? route.fallback()
            : route.fulfill({
                  status: 401,
                  json: {
                      status: 401,
                      meta: { is_authenticated: false },
                      data: {
                          flows: payload
                              ? [{ id: "verify_email", is_pending: true }]
                              : [],
                      },
                  },
              }),
    );
    await page.route("**/api/auth/browser/v1/auth/email/verify", (route) => {
        account.current = firstAccount;
        return route.fulfill({
            json: { status: 200, meta: { is_authenticated: true } },
        });
    });
    await page.route("**/api/**", (route) => {
        expect(route.request().headers()["referer"] ?? "").not.toContain(
            "invite-secret",
        );
        return route.fallback();
    });
    await page.route("**/api/auth/browser/v1/auth/signup", async (route) => {
        payload = route.request().postDataJSON();
        await route.fulfill({
            status: 401,
            json: {
                status: 401,
                data: { flows: [{ id: "verify_email", is_pending: true }] },
                meta: { is_authenticated: false },
            },
        });
    });
    await page.goto("/app/invitation/#token=invite-secret");
    await expect(page).toHaveURL("/app/invitation/");
    await page.reload();
    await page
        .getByRole("button", { name: "Create account", exact: true })
        .click();
    await expect(page.locator('input[name="company_name"]')).toHaveCount(0);
    await page.getByLabel("Email", { exact: true }).fill("viewer@example.test");
    await page
        .getByLabel("Password", { exact: true })
        .fill("Long-test-password-123!");
    await page
        .locator("form")
        .getByRole("button", { name: "Create account", exact: true })
        .click();
    await expect(page.getByLabel("Email code", { exact: true })).toBeVisible();
    expect(payload).toMatchObject({
        invitation_token: "invite-secret",
        email: "viewer@example.test",
    });
    expect(payload).not.toHaveProperty("company_name");
    await page.reload();
    await page.getByLabel("Email code", { exact: true }).fill("123456");
    await page
        .getByRole("button", { name: "Verify email", exact: true })
        .click();
    await expect(
        page.getByRole("button", { name: "Accept invitation" }),
    ).toBeVisible();
    await expect(page).toHaveURL("/app/invitation/");
});

test("signed in member explicitly accepts invitation and sees actionable errors", async ({
    page,
}) => {
    await mockApi(page);
    let accepted = false;
    await page.route("**/api/invitations/accept/", (route) => {
        expect(route.request().postDataJSON()).toEqual({
            token: "invite-secret",
        });
        expect(route.request().headers()["x-csrftoken"]).toBe("signed-token");
        if (accepted)
            return route.fulfill({ json: { tenant_id: 1, role: "viewer" } });
        return route.fulfill({
            status: 403,
            json: errorBody(
                "invitation_email_unverified",
                "Verify the invited email address before accepting.",
            ),
        });
    });
    await page.goto("/app/invitation/#token=invite-secret");
    await expect(page).toHaveURL("/app/invitation/");
    await page.getByRole("button", { name: "Accept invitation" }).click();
    await expect(page.locator(".form-errors p")).toHaveText(
        "Verify the invited email address before accepting.",
    );
    accepted = true;
    await page.getByRole("button", { name: "Accept invitation" }).click();
    await expect(page).toHaveURL("/app/w/1/");
    expect(
        await page.evaluate(() =>
            sessionStorage.getItem("pyracast.workspaceInvitation"),
        ),
    ).toBeNull();
    await expect(page.getByRole("heading", { name: "Plants" })).toBeVisible();
});

for (const [status, code, detail] of [
    [
        403,
        "invitation_email_unverified",
        "Verify the invited email address before accepting.",
    ],
    [400, "invitation_unavailable", "The invitation is invalid or expired."],
] as const) {
    test(`invitation can be dismissed after: ${detail}`, async ({
        page,
    }, testInfo) => {
        await mockApi(page);
        await page.setViewportSize({ width: 320, height: 740 });
        await page.route("**/api/invitations/accept/", (route) =>
            route.fulfill({
                status,
                json: errorBody(code, detail),
            }),
        );
        await page.goto("/app/invitation/#token=invite-secret");
        await page.getByRole("button", { name: "Accept invitation" }).click();
        await expect(page.locator(".form-errors p")).toHaveText(detail);
        expect(
            await page.evaluate(
                () => document.documentElement.scrollWidth <= window.innerWidth,
            ),
        ).toBe(true);
        await page.screenshot({
            path: testInfo.outputPath("invitation-320.png"),
            fullPage: true,
        });
        await page.getByRole("button", { name: "Cancel", exact: true }).click();
        await expect(page).toHaveURL("/app/w/1/");
        await expect(
            page.getByRole("heading", { name: "Plants" }),
        ).toBeVisible();
        expect(
            await page.evaluate(() =>
                sessionStorage.getItem("pyracast.workspaceInvitation"),
            ),
        ).toBeNull();
        await page.reload();
        await expect(
            page.getByRole("heading", { name: "Plants" }),
        ).toBeVisible();
    });
}

test("switching invited account signs out with CSRF and preserves invitation through login", async ({
    page,
}) => {
    const account = await mockApi(page);
    let signouts = 0;
    await page.route("**/api/auth/browser/v1/auth/session", (route) => {
        if (route.request().method() === "DELETE") {
            expect(route.request().headers()["x-csrftoken"]).toBe(
                "signed-token",
            );
            signouts++;
        }
        return route.fallback();
    });
    await page.goto("/app/invitation/#token=invite-secret");
    await page
        .getByRole("button", { name: "Use another account", exact: true })
        .click();
    await expect(
        page
            .locator("form")
            .getByRole("button", { name: "Sign in", exact: true }),
    ).toBeVisible();
    expect(signouts).toBe(1);
    expect(
        await page.evaluate(() =>
            sessionStorage.getItem("pyracast.workspaceInvitation"),
        ),
    ).toBe("invite-secret");
    await page.reload();
    await page.route("**/api/auth/browser/v1/auth/login", (route) => {
        account.current = secondAccount;
        return route.fallback();
    });
    await page.getByLabel("Email", { exact: true }).fill(secondUser.email);
    await page.getByLabel("Password", { exact: true }).fill("fixture-password");
    await page
        .locator("form")
        .getByRole("button", { name: "Sign in", exact: true })
        .click();
    await expect(
        page.getByRole("button", { name: "Accept invitation", exact: true }),
    ).toBeVisible();
    expect(account.current?.user).toBe(secondUser);
    await expect(page).toHaveURL("/app/invitation/");
    expect(
        await page.evaluate(() =>
            sessionStorage.getItem("pyracast.workspaceInvitation"),
        ),
    ).toBe("invite-secret");
});

test("anonymous expired invitation can be dismissed to restore company registration", async ({
    page,
}) => {
    await mockApi(page, { anonymous: true });
    await page.route("**/api/auth/browser/v1/auth/signup", (route) =>
        route.fulfill({
            status: 400,
            json: allauthErrorBody(400, "invalid_request", [
                { message: "This invitation has expired." },
            ]),
        }),
    );
    await page.goto("/app/invitation/#token=expired-secret");
    await page
        .getByRole("button", { name: "Create account", exact: true })
        .click();
    await expect(page.getByLabel("Company name")).toHaveCount(0);
    await page
        .getByLabel("Email", { exact: true })
        .fill("invited@example.test");
    await page
        .getByLabel("Password", { exact: true })
        .fill("Test-only-cobalt-79!");
    await page
        .locator("form")
        .getByRole("button", { name: "Create account", exact: true })
        .click();
    await expect(page.locator(".form-errors")).toContainText(
        "This invitation has expired.",
    );
    await page
        .getByRole("button", { name: "Withdraw invitation", exact: true })
        .click();
    await expect(page).toHaveURL("/app/w/");
    expect(
        await page.evaluate(() =>
            sessionStorage.getItem("pyracast.workspaceInvitation"),
        ),
    ).toBeNull();
    await page
        .getByRole("button", { name: "Create account", exact: true })
        .click();
    await expect(page.getByLabel("Company name")).toBeVisible();
    await expect(
        page.getByRole("button", { name: "Withdraw invitation", exact: true }),
    ).toHaveCount(0);
});
