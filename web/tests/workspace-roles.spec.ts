import { test, expect } from "./support/test";
import { mockApi, paged, user, tenantRow } from "./support/workspace";
import { allauthErrorBody, errorBody } from "./support/errors";

test("Turkish workspace roles and invitation labels preserve readable characters", async ({
    page,
}) => {
    await mockApi(page);
    let role = "admin";
    await page.route("**/api/session/", (route) =>
        route.fulfill({
            json: { user, language: "tr", csrf_token: "signed-token" },
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
        page.getByText("Üye · değişiklikler onay gerektirir", { exact: true }),
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
        route.fulfill({ json: paged([]) }),
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
        page.getByRole("button", { name: /Move to recycle bin/ }),
    ).toHaveCount(0);
    await expect(page.locator(".readonly-form input").first()).toBeDisabled();
    await page
        .getByRole("button", { name: "Email reminders", exact: true })
        .click();
    await expect(page.locator(".readonly-form input").first()).toBeDisabled();
    await expect(page.locator(".readonly-form button:visible")).toHaveCount(0);
    await page.getByRole("button", { name: "Outages", exact: true }).click();
    await expect(
        page.getByRole("button", { name: "Submit outage", exact: true }),
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
        page.getByRole("button", { name: "Send verification" }),
    ).toBeVisible();
    expect(writes).toEqual([]);
});

test("workspace administrator invites a viewer and sees invitation errors", async ({
    page,
}, testInfo) => {
    await mockApi(page);
    await page.setViewportSize({ width: 390, height: 844 });
    let email = "";
    await page.route("**/api/tenants/1/members/", (route) =>
        route.fulfill({
            json: {
                members: [
                    { id: 1, email: "owner@example.test", role: "admin" },
                ],
            },
        }),
    );
    await page.route("**/api/tenants/1/invitations/", async (route) => {
        if (route.request().method() === "POST") {
            expect(route.request().headers()["x-csrftoken"]).toBe(
                "signed-token",
            );
            if (email)
                return route.fulfill({
                    status: 400,
                    json: errorBody(
                        "invalid_request",
                        "An invitation is already pending.",
                    ),
                });
            email = route.request().postDataJSON().email;
            return route.fulfill({
                status: 201,
                json: { id: 4, email, expires_at: "2026-12-01T00:00:00Z" },
            });
        }
        return route.fulfill({
            json: {
                invitations: email
                    ? [{ id: 4, email, expires_at: "2026-12-01T00:00:00Z" }]
                    : [],
            },
        });
    });
    await page.route("**/api/tenants/1/invitations/4/", async (route) => {
        expect(route.request().method()).toBe("DELETE");
        expect(route.request().headers()["x-csrftoken"]).toBe("signed-token");
        email = "";
        await route.fulfill({ status: 204 });
    });
    await page.goto("/app/");
    await page.getByRole("button", { name: "Members", exact: true }).click();
    await expect(page.getByText("owner@example.test")).toBeVisible();
    await page
        .getByLabel("Email address", { exact: true })
        .fill("viewer@example.test");
    await page
        .getByRole("button", { name: "Send invitation", exact: true })
        .click();
    await expect(
        page.getByText("viewer@example.test", { exact: true }),
    ).toBeVisible();
    await page.screenshot({
        path: testInfo.outputPath("members-mobile.png"),
        fullPage: true,
    });
    await page
        .getByRole("button", { name: "Send invitation", exact: true })
        .click();
    await expect(page.locator(".form-errors")).toContainText(
        "An invitation is already pending.",
    );
    expect(
        await page.evaluate(
            () => document.documentElement.scrollWidth <= window.innerWidth,
        ),
    ).toBe(true);
    await page
        .getByRole("button", { name: "Cancel invitation", exact: true })
        .click();
    await expect(page.getByText("No pending invitations.")).toBeVisible();
});

test("invitation signup preserves token without creating another workspace", async ({
    page,
}) => {
    await mockApi(page, { anonymous: true });
    page.on("request", (request) => {
        expect(request.url()).not.toContain("invite-secret");
        expect(request.headers()["referer"] ?? "").not.toContain(
            "invite-secret",
        );
    });
    let payload: unknown;
    let verified = false;
    await page.route("**/api/session/", (route) =>
        route.fulfill({
            json: {
                user: verified ? user : null,
                csrf_token: verified ? "signed-token" : "anonymous-token",
            },
        }),
    );
    await page.route("**/api/auth/browser/v1/auth/session", (route) =>
        route.fulfill({
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
        verified = true;
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
    await mockApi(page);
    let signouts = 0;
    let leaving = false;
    page.on("framenavigated", (frame) => {
        if (frame === page.mainFrame()) leaving = false;
    });
    await page.route("**/api/session/", (route) =>
        leaving
            ? route.fulfill({
                  status: 503,
                  json: errorBody("busy", "Try again."),
              })
            : route.fallback(),
    );
    await page.route("**/api/auth/browser/v1/auth/session", (route) => {
        if (route.request().method() === "DELETE") {
            expect(route.request().headers()["x-csrftoken"]).toBe(
                "signed-token",
            );
            signouts++;
            leaving = true;
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
    await page
        .getByLabel("Email", { exact: true })
        .fill("invited@example.test");
    await page.getByLabel("Password", { exact: true }).fill("fixture-password");
    await page
        .locator("form")
        .getByRole("button", { name: "Sign in", exact: true })
        .click();
    await expect(
        page.getByRole("button", { name: "Accept invitation", exact: true }),
    ).toBeVisible();
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
        .getByRole("button", { name: "Cancel invitation", exact: true })
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
        page.getByRole("button", { name: "Cancel invitation", exact: true }),
    ).toHaveCount(0);
});
