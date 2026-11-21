import { expect, test } from "./support/test";
import { errorBody } from "./support/errors";
import { mockApi, user } from "./support/workspace";

test("a sign-out in another tab hides this tab's workspace without waiting for its focus", async ({
    context,
    page,
}) => {
    let signedIn = true;
    const other = await context.newPage();
    for (const tab of [page, other]) {
        await mockApi(tab);
        await tab.route("**/api/session/", (route) =>
            route.fulfill({
                json: {
                    user: signedIn ? user : null,
                    csrf_token: signedIn ? "signed-token" : "anonymous-token",
                    language: "en",
                    expires_at: null,
                    session_tag: signedIn ? "tag-a" : null,
                },
            }),
        );
        await tab.route("**/api/auth/browser/v1/auth/session", (route) => {
            if (route.request().method() !== "DELETE") return route.fallback();
            expect(route.request().headers()["x-session-tag"]).toBe("tag-a");
            signedIn = false;
            return route.fulfill({
                status: 401,
                json: { status: 401, meta: { is_authenticated: false } },
            });
        });
    }
    await page.goto("/app/w/");
    await expect(
        page.getByRole("heading", { name: "Plants", exact: true }),
    ).toBeVisible();
    await other.goto("/app/w/");
    await other.getByRole("button", { name: "Sign out" }).click();
    await expect(
        other.getByRole("heading", { name: "Welcome back" }),
    ).toBeVisible();
    await expect(
        page.getByRole("heading", { name: "Welcome back" }),
    ).toBeVisible();
    await expect(page.getByText("East array")).toHaveCount(0);
});

test("a failed read after another tab's sign-out offers the same retry as a failed first read", async ({
    context,
    page,
}) => {
    let signedIn = true;
    let unavailable = false;
    const other = await context.newPage();
    for (const tab of [page, other]) {
        await mockApi(tab);
        await tab.route("**/api/session/", (route) =>
            tab === page && unavailable
                ? route.fulfill({
                      status: 503,
                      json: errorBody(
                          "server_error",
                          "The session could not be read.",
                      ),
                  })
                : route.fulfill({
                      json: {
                          user: signedIn ? user : null,
                          csrf_token: signedIn
                              ? "signed-token"
                              : "anonymous-token",
                          language: "en",
                          expires_at: null,
                          session_tag: signedIn ? "tag-a" : null,
                      },
                  }),
        );
        await tab.route("**/api/auth/browser/v1/auth/session", (route) => {
            if (route.request().method() !== "DELETE") return route.fallback();
            signedIn = false;
            unavailable = true;
            return route.fulfill({
                status: 401,
                json: { status: 401, meta: { is_authenticated: false } },
            });
        });
    }
    await page.goto("/app/w/");
    await expect(
        page.getByRole("heading", { name: "Plants", exact: true }),
    ).toBeVisible();
    await other.goto("/app/w/");
    await other.getByRole("button", { name: "Sign out" }).click();
    await expect(
        other.getByRole("heading", { name: "Welcome back" }),
    ).toBeVisible();
    await expect(
        page.getByText("The session could not be read."),
    ).toBeVisible();
    await expect(page.getByText("East array")).toHaveCount(0);
    unavailable = false;
    await page.getByRole("button", { name: "Try again" }).click();
    await expect(
        page.getByRole("heading", { name: "Welcome back" }),
    ).toBeVisible();
});
