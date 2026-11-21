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

test("a failed read after another tab's change is not shown again while a read after a later change runs", async ({
    context,
    page,
}) => {
    let unavailable = false;
    let holding = false;
    let release: () => void = () => {};
    const held = new Promise<void>((resolve) => {
        release = resolve;
    });
    await mockApi(page);
    await page.route("**/api/session/", async (route) => {
        if (holding) await held;
        return unavailable
            ? route.fulfill({
                  status: 503,
                  json: errorBody(
                      "server_error",
                      "The session could not be read.",
                  ),
              })
            : route.fulfill({
                  json: {
                      user,
                      csrf_token: "signed-token",
                      language: "en",
                      expires_at: null,
                      session_tag: "tag-a",
                  },
              });
    });
    // Another document of the origin that only writes the observed tag, as a tab's read does.
    const other = await context.newPage();
    await other.route("**/blank", (route) =>
        route.fulfill({ contentType: "text/html", body: "<html></html>" }),
    );
    const observe = (tag: string) =>
        other.evaluate(
            (value) =>
                window.localStorage.setItem("pyracast.sessionTag", value),
            tag,
        );
    await page.goto("/app/w/");
    await expect(
        page.getByRole("heading", { name: "Plants", exact: true }),
    ).toBeVisible();
    await other.goto("/blank");
    unavailable = true;
    await observe("tag-b");
    await expect(
        page.getByText("The session could not be read."),
    ).toBeVisible();
    unavailable = false;
    await observe("tag-a");
    await expect(
        page.getByRole("heading", { name: "Plants", exact: true }),
    ).toBeVisible();
    holding = true;
    await observe("tag-b");
    await expect(
        page.getByRole("progressbar", { name: "Loading…" }),
    ).toBeVisible();
    await expect(page.getByText("The session could not be read.")).toHaveCount(
        0,
    );
    release();
    await expect(
        page.getByRole("heading", { name: "Plants", exact: true }),
    ).toBeVisible();
});
