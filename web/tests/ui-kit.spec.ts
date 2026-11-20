import AxeBuilder from "@axe-core/playwright";
import { expect, test, type Page } from "./support/test";
import { errorBody } from "./support/errors";
import { mockApi, user } from "./support/workspace";

async function scan(page: Page, selector: string) {
    const { violations } = await new AxeBuilder({ page })
        .include(selector)
        .analyze();
    expect(violations).toEqual([]);
}

test("view navigation names its group and marks the current view", async ({
    page,
}) => {
    await mockApi(page);
    await page.goto("/app/");
    await page.getByRole("button", { name: /East array/ }).click();
    const sections = page.getByRole("navigation", { name: "Plant sections" });
    await expect(
        sections.getByRole("button", { name: "Forecast", exact: true }),
    ).toHaveAttribute("aria-current", "page");
    await expect(sections.locator("[aria-current]")).toHaveCount(1);
    const views = page.getByRole("navigation", { name: "Forecast views" });
    await expect(
        views.getByRole("button", { name: "Generation", exact: true }),
    ).toHaveAttribute("aria-current", "page");
    await views
        .getByRole("button", { name: "Irradiance", exact: true })
        .click();
    await expect(
        views.getByRole("button", { name: "Irradiance", exact: true }),
    ).toHaveAttribute("aria-current", "page");
    await expect(views.locator("[aria-current]")).toHaveCount(1);
    for (const button of await page
        .locator(".tabs button, .subnav button")
        .all())
        await expect(button).toHaveAttribute("type", "button");
    await scan(page, ".tabs");
    await scan(page, ".subnav");
});

test("labelled controls are reachable by their visible labels", async ({
    page,
}) => {
    await mockApi(page);
    await page.goto("/app/");
    await page.getByRole("button", { name: /East array/ }).click();
    await expect(
        page.getByLabel("Forecast date", { exact: true }),
    ).toHaveAttribute("type", "date");
    await expect(page.getByLabel("Resolution")).toHaveValue("60");
    await scan(page, ".forecast-controls");
    await page.getByRole("button", { name: "Production", exact: true }).click();
    await expect(page.getByLabel("Day", { exact: true })).toHaveAttribute(
        "type",
        "date",
    );
    const fullDay = page.getByRole("checkbox", {
        name: "Show full day",
        exact: true,
    });
    await expect(fullDay).not.toBeChecked();
    await fullDay.check();
    await expect(fullDay).toBeChecked();
    await scan(page, ".production-controls");
});

test("a confirmation names its question, focuses the safe choice and returns focus", async ({
    page,
}) => {
    const viewer = { id: 2, email: "viewer@example.test", role: "viewer" };
    await mockApi(page);
    await page.route("**/api/tenants/1/members/", (route) =>
        route.fulfill({
            json: {
                members: [
                    { id: user.id, email: user.email, role: "admin" },
                    viewer,
                ],
            },
        }),
    );
    await page.route("**/api/tenants/*/invitations/", (route) =>
        route.fulfill({ json: { invitations: [] } }),
    );
    await page.goto("/app/");
    await page.getByRole("button", { name: "Members", exact: true }).click();
    await page
        .getByRole("button", {
            name: `Remove from workspace: ${viewer.email}`,
            exact: true,
        })
        .click();
    const confirmation = page.getByRole("region", {
        name: "Confirm member removal",
    });
    await expect(confirmation).toContainText(viewer.email);
    await expect(
        confirmation.getByRole("button", { name: "Cancel", exact: true }),
    ).toBeFocused();
    await scan(page, ".member-list + section");
    await confirmation
        .getByRole("button", { name: "Cancel", exact: true })
        .click();
    await expect(confirmation).toHaveCount(0);
    await expect(
        page.getByRole("heading", { name: "Members", level: 2 }),
    ).toBeFocused();
});

test("a confirmation moved to another member focuses its safe choice again", async ({
    page,
}) => {
    const first = { id: 2, email: "first@example.test", role: "viewer" };
    const second = { id: 3, email: "second@example.test", role: "viewer" };
    await mockApi(page);
    await page.route("**/api/tenants/1/members/", (route) =>
        route.fulfill({
            json: {
                members: [
                    { id: user.id, email: user.email, role: "admin" },
                    first,
                    second,
                ],
            },
        }),
    );
    await page.route("**/api/tenants/*/invitations/", (route) =>
        route.fulfill({ json: { invitations: [] } }),
    );
    await page.goto("/app/");
    await page.getByRole("button", { name: "Members", exact: true }).click();
    for (const viewer of [first, second])
        await page
            .getByRole("button", {
                name: `Remove from workspace: ${viewer.email}`,
                exact: true,
            })
            .click();
    const confirmation = page.getByRole("region", {
        name: "Confirm member removal",
    });
    await expect(confirmation).toContainText(second.email);
    await expect(
        confirmation.getByRole("button", { name: "Cancel", exact: true }),
    ).toBeFocused();
});

async function membersPage(
    page: Page,
    invite: () => { status?: number; json: unknown },
    { members = () => ({ status: 200 }) } = {} as {
        members?: () => { status: number };
    },
) {
    await mockApi(page);
    await page.route("**/api/tenants/1/members/", (route) => {
        const { status } = members();
        return status === 200
            ? route.fulfill({
                  json: {
                      members: [
                          { id: user.id, email: user.email, role: "admin" },
                      ],
                  },
              })
            : route.fulfill({
                  status,
                  json: errorBody("busy", "Members are unavailable."),
              });
    });
    await page.route("**/api/tenants/*/invitations/", (route) =>
        route.request().method() === "POST"
            ? route.fulfill(invite())
            : route.fulfill({ json: { invitations: [] } }),
    );
    await page.goto("/app/");
    await page.getByRole("button", { name: "Members", exact: true }).click();
    await expect(page.getByText(user.email, { exact: true })).toBeVisible();
}

test("a failed form keeps the general message and names each field error by its label", async ({
    page,
}) => {
    await membersPage(page, () => ({
        status: 400,
        json: errorBody("invalid_request", "Check the invitation.", {
            fields: {
                email: "Enter a valid email address.",
                tenant: "This workspace cannot invite.",
                role: "",
            },
        }),
    }));
    const email = page.getByLabel("Email address", { exact: true });
    await email.fill("new@example.test");
    await page
        .getByRole("button", { name: "Send invitation", exact: true })
        .click();
    const summary = page.locator(".form-errors");
    await expect(summary).toBeFocused();
    await expect(summary.locator("p")).toHaveText("Check the invitation.");
    await expect(summary.getByRole("listitem")).toHaveText([
        "Email address: Enter a valid email address.",
        "This workspace cannot invite.",
    ]);
    await expect(
        summary.getByRole("listitem").nth(1).locator("button"),
    ).toHaveCount(0);
    await summary
        .getByRole("button", {
            name: "Email address: Enter a valid email address.",
            exact: true,
        })
        .click();
    await expect(email).toBeFocused();
    await expect(email).toHaveAttribute("aria-invalid", "true");
    await expect(email).toHaveAccessibleDescription(
        "Enter a valid email address.",
    );
    await scan(page, ".form-errors");
});

test("the form status region is mounted before it speaks and clears on the next change", async ({
    page,
}) => {
    await membersPage(page, () => ({
        status: 201,
        json: {
            id: 7,
            email: "new@example.test",
            role: "viewer",
            delivery_status: "sent",
        },
    }));
    const email = page.getByLabel("Email address", { exact: true });
    const form = page.locator("form").filter({ has: email });
    const status = form.locator("[aria-live='polite']");
    await expect(status).toHaveCount(1);
    await expect(status).toHaveText("");
    await email.fill("new@example.test");
    await page
        .getByRole("button", { name: "Send invitation", exact: true })
        .click();
    await expect(status).toHaveText("Invitation sent.");
    await email.fill("other@example.test");
    await expect(status).toHaveText("");
});

test("a select reports its field error like every other control", async ({
    page,
}) => {
    await mockApi(page);
    await page.route("**/api/tenants/1/regions/11/plants/21/", (route) =>
        route.request().method() === "PUT"
            ? route.fulfill({
                  status: 400,
                  json: errorBody("invalid_request", "Check the plant.", {
                      fields: {
                          measurement_basis:
                              "Choose where production is metered.",
                      },
                  }),
              })
            : route.fallback(),
    );
    await page.goto("/app/");
    await page.getByRole("button", { name: /East array/ }).click();
    await page.getByRole("button", { name: "Settings", exact: true }).click();
    await page
        .getByRole("button", { name: "Save configuration", exact: true })
        .click();
    const basis = page.getByLabel("Measurement point", { exact: true });
    await expect(basis).toHaveAttribute("aria-invalid", "true");
    await expect(basis).toHaveAccessibleDescription(
        "Choose where production is metered.",
    );
    await expect(page.locator(".form-errors").getByRole("listitem")).toHaveText(
        ["Measurement point: Choose where production is metered."],
    );
});

test("the member list keeps its rows with a retry when a refresh fails", async ({
    page,
}) => {
    let membersFail = false;
    await membersPage(
        page,
        () => {
            membersFail = true;
            return {
                status: 201,
                json: {
                    id: 7,
                    email: "new@example.test",
                    role: "viewer",
                    delivery_status: "sent",
                },
            };
        },
        { members: () => ({ status: membersFail ? 503 : 200 }) },
    );
    await page
        .getByLabel("Email address", { exact: true })
        .fill("new@example.test");
    await page
        .getByRole("button", { name: "Send invitation", exact: true })
        .click();
    const members = page.locator("section").filter({
        has: page.getByRole("heading", { name: "Members", level: 2 }),
    });
    await expect(members.getByRole("alert")).toContainText("Could not refresh");
    await expect(members.getByText(user.email, { exact: true })).toBeVisible();
    membersFail = false;
    await members
        .getByRole("button", { name: "Try again", exact: true })
        .click();
    await expect(members.getByRole("alert")).toHaveCount(0);
});

test("the shell announces action outcomes through one live region that exists before it speaks", async ({
    page,
}) => {
    let invitations = [
        {
            id: 7,
            email: "new@example.test",
            role: "viewer",
            delivery_status: "sent",
        },
    ];
    await mockApi(page);
    await page.route("**/api/tenants/1/members/", (route) =>
        route.fulfill({
            json: {
                members: [{ id: user.id, email: user.email, role: "admin" }],
            },
        }),
    );
    await page.route(/\/api\/tenants\/\d+\/invitations\//, (route) => {
        if (route.request().method() === "DELETE") {
            invitations = [];
            return route.fulfill({ status: 204 });
        }
        return route.fulfill({ json: { invitations } });
    });
    await page.goto("/app/");
    const region = page.locator("[data-live-region]");
    await expect(region).toHaveCount(1);
    await expect(region).toHaveAttribute("aria-live", "polite");
    await expect(region).toHaveText("");
    await page.getByRole("button", { name: "Members", exact: true }).click();
    const cancel = page.getByRole("button", {
        name: "Cancel invitation",
        exact: true,
    });
    await expect(cancel).toHaveClass(/secondary/);
    await cancel.click();
    await expect(region).toHaveText("Invitation cancelled.");
    await expect(
        page.getByText("new@example.test", { exact: true }),
    ).toHaveCount(0);
    await expect(page.locator("[data-live-region]")).toHaveCount(1);
});

test("the shell speaks a repeated outcome again", async ({ page }) => {
    let invitations = [7, 8].map((id) => ({
        id,
        email: `invitee${id}@example.test`,
        role: "viewer",
        delivery_status: "sent",
    }));
    await mockApi(page);
    await page.route("**/api/tenants/1/members/", (route) =>
        route.fulfill({
            json: {
                members: [{ id: user.id, email: user.email, role: "admin" }],
            },
        }),
    );
    await page.route(/\/api\/tenants\/\d+\/invitations\//, (route) => {
        if (route.request().method() === "DELETE") {
            invitations = invitations.filter(
                (invitation) =>
                    !route.request().url().endsWith(`/${invitation.id}/`),
            );
            return route.fulfill({ status: 204 });
        }
        return route.fulfill({ json: { invitations } });
    });
    await page.goto("/app/");
    await page.getByRole("button", { name: "Members", exact: true }).click();
    const region = page.locator("[data-live-region]");
    const cancel = page.getByRole("button", {
        name: "Cancel invitation",
        exact: true,
    });
    await cancel.first().click();
    await expect(region).toHaveText("Invitation cancelled.");
    await expect(cancel).toHaveCount(1);
    await region.evaluate((element) => {
        (window as unknown as { spoken: Node | null }).spoken =
            element.firstChild;
    });
    await cancel.click();
    await expect(cancel).toHaveCount(0);
    await expect
        .poll(() =>
            region.evaluate(
                (element) =>
                    element.firstChild !==
                    (window as unknown as { spoken: Node | null }).spoken,
            ),
        )
        .toBe(true);
    await expect(region).toHaveText("Invitation cancelled.");
});

test("a checkbox reports its field error like every other control", async ({
    page,
}) => {
    await mockApi(page);
    await page.route("**/collection-schedule/", (route) =>
        route.request().method() === "PUT"
            ? route.fulfill({
                  status: 400,
                  json: errorBody("invalid_request", "Check the schedule.", {
                      fields: { enabled: "Approval is required first." },
                  }),
              })
            : route.fulfill({
                  json: {
                      enabled: false,
                      times: ["09:00"],
                      timezone: "Europe/Istanbul",
                      last_success_at: null,
                      next_collection_at: null,
                      history_requests_reserved_per_day: 1,
                      history_local_time: "03:00",
                      history_retry_until_local: "09:00",
                      history_retry_minutes: 30,
                      max_forecast_times: 9,
                      effective_from: "2026-01-01T00:00:00Z",
                      recent_jobs: [],
                  },
              }),
    );
    await page.goto("/app/");
    await page.getByRole("button", { name: /East array/ }).click();
    await page.getByRole("button", { name: "Settings", exact: true }).click();
    await page
        .getByRole("button", { name: "Data collection", exact: true })
        .click();
    await page
        .getByRole("button", { name: "Save collection schedule", exact: true })
        .click();
    const enabled = page.getByRole("checkbox", {
        name: "Enable data collection",
        exact: true,
    });
    await expect(enabled).toHaveAttribute("aria-invalid", "true");
    await expect(enabled).toHaveAccessibleDescription(
        "Approval is required first.",
    );
    await expect(page.locator(".form-errors").getByRole("listitem")).toHaveText(
        ["Enable data collection: Approval is required first."],
    );
    await scan(page, ".form-errors");
});
