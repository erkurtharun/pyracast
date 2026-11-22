import { expect, test, type Page } from "./support/test";
import { errorBody } from "./support/errors";
import { answer, mockApi } from "./support/workspace";
import {
    changedNotice,
    expiredNotice,
    firstAccount,
    focusWindow,
    isMarked,
    markDocument,
    pendingInvitation,
    restoreFromCache,
    secondUser,
    serveSession,
    sessionAnswer,
    type Account,
} from "./support/session";

function plants(page: Page) {
    return page.getByRole("heading", { name: "Plants", exact: true });
}

function welcome(page: Page) {
    return page.getByRole("heading", { name: "Welcome back" });
}

function signingOut(page: Page) {
    return page.getByRole("heading", { name: "Signing out" });
}

type SignOutServer = { deletes: (string | undefined)[]; failing: boolean };

async function signOutServer(page: Page, account: { current: Account }) {
    const server: SignOutServer = { deletes: [], failing: true };
    await page.route("**/api/auth/browser/v1/auth/session", (route) => {
        if (route.request().method() !== "DELETE") return route.fallback();
        server.deletes.push(route.request().headers()["x-session-tag"]);
        if (server.failing)
            return route.fulfill({
                status: 503,
                json: errorBody("busy", "Sign-out is unavailable."),
            });
        account.current = null;
        return route.fulfill({
            status: 401,
            json: { status: 401, meta: { is_authenticated: false } },
        });
    });
    return server;
}

test("a sign-out whose request fails stays signing out across history until Retry ends the shown session", async ({
    page,
}) => {
    const account: { current: Account } = { current: firstAccount };
    await mockApi(page);
    await serveSession(page, () => account.current);
    const server = await signOutServer(page, account);
    await page.goto("/app/w/1/");
    await expect(plants(page)).toBeVisible();
    await page.getByRole("button", { name: "Account", exact: true }).click();
    await expect(
        page.getByRole("heading", { name: "Account settings", exact: true }),
    ).toBeVisible();
    await markDocument(page);
    await page.getByRole("button", { name: "Sign out", exact: true }).click();
    await expect(signingOut(page)).toBeVisible();
    await expect(page.getByText("Sign-out is unavailable.")).toBeVisible();
    await page.goBack();
    await expect(page).toHaveURL("/app/w/1/");
    await expect(signingOut(page)).toBeVisible();
    await expect(plants(page)).toHaveCount(0);
    await page.goForward();
    await expect(page).toHaveURL("/app/account/");
    await expect(signingOut(page)).toBeVisible();
    await expect(
        page.getByRole("heading", { name: "Account settings", exact: true }),
    ).toHaveCount(0);
    server.failing = false;
    await page.getByRole("button", { name: "Try again" }).click();
    await expect(welcome(page)).toBeVisible();
    expect(await isMarked(page)).toBe(false);
    expect(server.deletes).toEqual(["tag-a", "tag-a"]);
});

test("a sign-out whose session read fails stays signing out until Retry confirms it", async ({
    page,
}) => {
    const account: { current: Account } = { current: firstAccount };
    let readFails = false;
    await mockApi(page);
    await page.route("**/api/session/", (route) =>
        readFails
            ? route.fulfill({
                  status: 503,
                  json: errorBody("busy", "The session could not be read."),
              })
            : route.fulfill({
                  json: {
                      user: account.current?.user ?? null,
                      csrf_token: account.current
                          ? "signed-token"
                          : "anonymous-token",
                      language: "en",
                      expires_at: null,
                      session_tag: account.current?.tag ?? null,
                  },
              }),
    );
    const server = await signOutServer(page, account);
    server.failing = false;
    await page.goto("/app/w/1/");
    await expect(plants(page)).toBeVisible();
    readFails = true;
    await page.getByRole("button", { name: "Sign out", exact: true }).click();
    await expect(signingOut(page)).toBeVisible();
    await expect(
        page.getByText("The session could not be read."),
    ).toBeVisible();
    readFails = false;
    await page.getByRole("button", { name: "Try again" }).click();
    await expect(welcome(page)).toBeVisible();
    await expect(page.getByText(expiredNotice)).toHaveCount(0);
    expect(server.deletes[0]).toBe("tag-a");
});

async function invitedTab(page: Page, account: () => Account) {
    await mockApi(page);
    await serveSession(page, account);
    await openWithInvitation(page);
}

async function openWithInvitation(page: Page) {
    await page.goto("/app/invitation/#token=invite-secret");
    await expect(
        page.getByRole("heading", { name: "Join workspace" }),
    ).toBeVisible();
    await page.goto("/app/w/1/");
    await expect(plants(page)).toBeVisible();
    expect(await pendingInvitation(page)).toBe("invite-secret");
}

async function signInLandsOnWorkspaces(page: Page) {
    await page.goto("/app/");
    await page
        .getByLabel("Email", { exact: true })
        .fill(firstAccount.user.email);
    await page
        .getByLabel("Password", { exact: true })
        .fill("Test-only-cobalt-79!");
    await page.locator("form").getByRole("button", { name: "Sign in" }).click();
    await expect(plants(page)).toBeVisible();
    await expect(page).toHaveURL("/app/w/1/");
}

test("a sign-out here takes the tab's pending invitation with it", async ({
    page,
}) => {
    let account: Account = firstAccount;
    await invitedTab(page, () => account);
    await page.route("**/api/auth/browser/v1/auth/session", (route) => {
        if (route.request().method() === "DELETE") account = null;
        return route.fallback();
    });
    await page.route("**/api/auth/browser/v1/auth/login", (route) => {
        account = firstAccount;
        return route.fallback();
    });
    await page.getByRole("button", { name: "Sign out", exact: true }).click();
    await expect(welcome(page)).toBeVisible();
    expect(await pendingInvitation(page)).toBeNull();
    await signInLandsOnWorkspaces(page);
});

test("a sign-out in another window takes this tab's pending invitation with it", async ({
    page,
    context,
}) => {
    let account: Account = firstAccount;
    const other = await context.newPage();
    for (const tab of [page, other]) {
        await mockApi(tab);
        await serveSession(tab, () => account);
        await tab.route("**/api/auth/browser/v1/auth/session", (route) => {
            if (route.request().method() === "DELETE") account = null;
            return route.fallback();
        });
        await tab.route("**/api/auth/browser/v1/auth/login", (route) => {
            account = firstAccount;
            return route.fallback();
        });
    }
    await openWithInvitation(page);
    await other.goto("/app/w/1/");
    await other.getByRole("button", { name: "Sign out", exact: true }).click();
    await expect(welcome(other)).toBeVisible();
    await expect(welcome(page)).toBeVisible();
    expect(await pendingInvitation(page)).toBeNull();
    await signInLandsOnWorkspaces(page);
});

test("an expired session takes the tab's pending invitation with it", async ({
    page,
}) => {
    let account: Account = firstAccount;
    await invitedTab(page, () => account);
    await page.route("**/api/auth/browser/v1/auth/login", (route) => {
        account = firstAccount;
        return route.fallback();
    });
    account = null;
    await focusWindow(page);
    await expect(page.getByText(expiredNotice)).toBeVisible();
    expect(await pendingInvitation(page)).toBeNull();
    await signInLandsOnWorkspaces(page);
});

test("using another account signs out as a state of the document and ends on the invitation", async ({
    page,
}) => {
    const account: { current: Account } = { current: firstAccount };
    await mockApi(page);
    await serveSession(page, () => account.current);
    const server = await signOutServer(page, account);
    await page.goto("/app/invitation/#token=invite-secret");
    await expect(
        page.getByRole("heading", { name: "Join workspace" }),
    ).toBeVisible();
    await markDocument(page);
    await page
        .getByRole("button", { name: "Use another account", exact: true })
        .click();
    await expect(signingOut(page)).toBeVisible();
    await expect(page.getByText("Sign-out is unavailable.")).toBeVisible();
    server.failing = false;
    await page.getByRole("button", { name: "Try again" }).click();
    await expect(
        page.getByRole("region", { name: "Invitation" }),
    ).toContainText("You are invited to join Sunfield Energy.");
    await expect(
        page.locator("form").getByRole("button", { name: "Sign in" }),
    ).toBeVisible();
    await expect(page).toHaveURL("/app/invitation/");
    expect(await isMarked(page)).toBe(false);
    expect(await pendingInvitation(page)).toBe("invite-secret");
    await expect(page.getByText(expiredNotice)).toHaveCount(0);
    expect(server.deletes).toEqual(["tag-a", "tag-a"]);
});

test("leaving a pending sign-in stage keeps the pending invitation", async ({
    page,
}) => {
    await mockApi(page, { anonymous: true });
    await page.route("**/api/auth/browser/v1/auth/session", (route) =>
        route.request().method() === "GET"
            ? route.fulfill({
                  status: 401,
                  json: {
                      status: 401,
                      data: {
                          flows: [{ id: "mfa_authenticate", is_pending: true }],
                      },
                      meta: { is_authenticated: false },
                  },
              })
            : route.fallback(),
    );
    await page.goto("/app/invitation/#token=invite-secret");
    await expect(
        page.getByRole("heading", {
            name: "Two-factor authentication",
            exact: true,
        }),
    ).toBeVisible();
    await markDocument(page);
    await page
        .getByRole("button", { name: "Use a different account", exact: true })
        .click();
    await expect(welcome(page)).toBeVisible();
    await expect(
        page.getByRole("region", { name: "Invitation" }),
    ).toContainText("You are invited to join Sunfield Energy.");
    expect(await pendingInvitation(page)).toBe("invite-secret");
    expect(await isMarked(page)).toBe(true);
});

test("a sign-in with a pending invitation lands on the invitation", async ({
    page,
}) => {
    let account: Account = null;
    await mockApi(page, { anonymous: true });
    await serveSession(page, () => account);
    await page.route("**/api/auth/browser/v1/auth/login", (route) => {
        account = firstAccount;
        return route.fallback();
    });
    await page.goto("/app/invitation/#token=invite-secret");
    await page
        .getByLabel("Email", { exact: true })
        .fill(firstAccount.user.email);
    await page
        .getByLabel("Password", { exact: true })
        .fill("Test-only-cobalt-79!");
    await page.locator("form").getByRole("button", { name: "Sign in" }).click();
    await expect(
        page.getByRole("button", { name: "Accept invitation", exact: true }),
    ).toBeVisible();
    await expect(page).toHaveURL("/app/invitation/");
});

test("a reload after a sign-out whose confirmation failed takes the tab's pending invitation with it", async ({
    page,
}) => {
    const account: { current: Account } = { current: firstAccount };
    let readFails = false;
    await mockApi(page);
    await page.route("**/api/session/", (route) =>
        readFails
            ? route.fulfill({
                  status: 503,
                  json: errorBody("busy", "The session could not be read."),
              })
            : route.fulfill({ json: sessionAnswer(account.current, null) }),
    );
    const server = await signOutServer(page, account);
    server.failing = false;
    await page.route("**/api/auth/browser/v1/auth/login", (route) => {
        account.current = firstAccount;
        return route.fallback();
    });
    await openWithInvitation(page);
    readFails = true;
    await page.getByRole("button", { name: "Sign out", exact: true }).click();
    await expect(
        page.getByText("The session could not be read."),
    ).toBeVisible();
    readFails = false;
    await page.reload();
    await expect(welcome(page)).toBeVisible();
    await expect(page.getByText(expiredNotice)).toHaveCount(0);
    expect(await pendingInvitation(page)).toBeNull();
    await signInLandsOnWorkspaces(page);
});

test("a tab restored after another user signed in drops its pending invitation and tells once that the session changed", async ({
    page,
}) => {
    let account: Account = firstAccount;
    await invitedTab(page, () => account);
    await markDocument(page);
    account = { user: secondUser, tag: "tag-b" };
    await restoreFromCache(page);
    await expect(page.getByText(changedNotice)).toBeVisible();
    expect(await isMarked(page)).toBe(false);
    expect(await pendingInvitation(page)).toBeNull();
    await page.reload();
    await expect(plants(page)).toBeVisible();
    await expect(page.getByText(changedNotice)).toHaveCount(0);
});

test("accepting an invitation and using another account exclude each other", async ({
    page,
}) => {
    const account: { current: Account } = { current: firstAccount };
    await mockApi(page);
    await serveSession(page, () => account.current);
    const server = await signOutServer(page, account);
    let reach!: () => void;
    const reached = new Promise<void>((resolve) => (reach = resolve));
    let release!: () => void;
    const released = new Promise<void>((resolve) => (release = resolve));
    await page.route("**/api/invitations/accept/", async (route) => {
        reach();
        await released;
        await route.fulfill({
            status: 503,
            json: errorBody("busy", "Accepting is unavailable."),
        });
    });
    await page.goto("/app/invitation/#token=invite-secret");
    await page
        .getByRole("button", { name: "Accept invitation", exact: true })
        .click();
    await reached;
    await expect(
        page.getByRole("button", { name: "Use another account", exact: true }),
    ).toBeDisabled();
    release();
    await expect(page.getByText("Accepting is unavailable.")).toBeVisible();
    await expect(
        page.getByRole("button", { name: "Use another account", exact: true }),
    ).toBeEnabled();
    expect(server.deletes).toEqual([]);
});

test("an acceptance answered after signing out began navigates nowhere", async ({
    page,
}) => {
    const account: { current: Account } = { current: firstAccount };
    await mockApi(page);
    await serveSession(page, () => account.current);
    const server = await signOutServer(page, account);
    let release!: () => void;
    const released = new Promise<void>((resolve) => (release = resolve));
    let respond!: () => void;
    const answered = new Promise<void>((resolve) => (respond = resolve));
    await page.route("**/api/invitations/accept/", async (route) => {
        await released;
        await route.fulfill({
            json: answer("InvitationAcceptanceOutput", {
                tenant_id: 1,
                role: "viewer",
            }),
        });
        respond();
    });
    await page.goto("/app/invitation/#token=invite-secret");
    await expect(
        page.getByRole("button", { name: "Accept invitation", exact: true }),
    ).toBeVisible();
    await markDocument(page);
    // Both presses run in one task, before a render can disable either button.
    await page.evaluate(() => {
        const button = (name: string) =>
            [...document.querySelectorAll("button")].find(
                (element) => element.textContent === name,
            )!;
        button("Accept invitation").click();
        button("Use another account").click();
    });
    await expect(signingOut(page)).toBeVisible();
    release();
    await answered;
    const retried = page.waitForResponse(
        (response) =>
            response.url().endsWith("/auth/session") &&
            response.request().method() === "DELETE",
    );
    await page.getByRole("button", { name: "Try again" }).click();
    await retried;
    await expect(page.getByText("Sign-out is unavailable.")).toBeVisible();
    expect(new URL(page.url()).pathname).toBe("/app/invitation/");
    expect(await pendingInvitation(page)).toBe("invite-secret");
    expect(await isMarked(page)).toBe(true);
    expect(server.deletes).toEqual(["tag-a", "tag-a"]);
});
