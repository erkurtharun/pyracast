import { expect, test, type Page } from "./support/test";
import { mockApi } from "./support/workspace";
import {
    changedNotice,
    expiredNotice,
    firstAccount,
    focusWindow,
    isMarked,
    markDocument,
    markedTenantReads,
    navigationType,
    secondUser,
    serveSession,
    tagObserver,
    type Account,
} from "./support/session";
import { nativeProductionHistory } from "./support/native-production";

const address = "/app/w/1/#keep";

function plants(page: Page) {
    return page.getByRole("heading", { name: "Plants", exact: true });
}

function welcome(page: Page) {
    return page.getByRole("heading", { name: "Welcome back" });
}

async function expectFreshDocument(page: Page, loadedWith?: string) {
    expect(await isMarked(page)).toBe(false);
    expect(await navigationType(page)).toBe("reload");
    const shown = new URL(page.url());
    expect(shown.pathname + shown.hash).toBe(address);
    expect(
        (await markedTenantReads(page)).filter((tag) => tag !== loadedWith),
    ).toEqual([]);
}

async function signedInDocument(page: Page, account: () => Account) {
    await mockApi(page);
    await serveSession(page, account);
    await page.goto(address);
    await expect(plants(page)).toBeVisible();
    await markDocument(page);
}

test("an expired session ends in a fresh document that tells once that the session expired", async ({
    page,
}) => {
    let account: Account = firstAccount;
    await signedInDocument(page, () => account);
    account = null;
    await focusWindow(page);
    await expect(welcome(page)).toBeVisible();
    await expect(page.getByText(expiredNotice)).toBeVisible();
    await expectFreshDocument(page, "tag-a");
    await page.reload();
    await expect(welcome(page)).toBeVisible();
    await expect(page.getByText(expiredNotice)).toHaveCount(0);
});

test("a sign-out here ends in a fresh document without a notice", async ({
    page,
}) => {
    let account: Account = firstAccount;
    await signedInDocument(page, () => account);
    await page.route("**/api/auth/browser/v1/auth/session", (route) => {
        if (route.request().method() !== "DELETE") return route.fallback();
        account = null;
        return route.fallback();
    });
    await page.getByRole("button", { name: "Sign out", exact: true }).click();
    await expect(welcome(page)).toBeVisible();
    await expectFreshDocument(page, "tag-a");
    await expect(page.getByText(expiredNotice)).toHaveCount(0);
    await expect(page.getByText(changedNotice)).toHaveCount(0);
});

test("a sign-out in another window ends in a fresh document", async ({
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
    }
    await page.goto(address);
    await expect(plants(page)).toBeVisible();
    await markDocument(page);
    await other.goto("/app/w/");
    await other.getByRole("button", { name: "Sign out", exact: true }).click();
    await expect(welcome(other)).toBeVisible();
    await expect(welcome(page)).toBeVisible();
    await expectFreshDocument(page, "tag-a");
});

for (const [change, next] of [
    ["another user's session", { user: secondUser, tag: "tag-b" }],
    [
        "a new session of the same user",
        { user: firstAccount.user, tag: "tag-a2" },
    ],
] as const) {
    test(`${change} ends in a fresh document that tells once that the session changed`, async ({
        page,
    }) => {
        let account: Account = firstAccount;
        await signedInDocument(page, () => account);
        const observe = await tagObserver(page);
        account = next;
        await observe(next.tag);
        await expect(page.getByText(changedNotice)).toBeVisible();
        await expect(plants(page)).toBeVisible();
        await expectFreshDocument(page, "tag-a");
        await page.reload();
        await expect(plants(page)).toBeVisible();
        await expect(page.getByText(changedNotice)).toHaveCount(0);
    });
}

test("a sign-in completed on the sign-in screen ends in a fresh document", async ({
    page,
}) => {
    let account: Account = null;
    await mockApi(page, { anonymous: true });
    await serveSession(page, () => account);
    await page.route("**/api/auth/browser/v1/auth/login", (route) => {
        account = firstAccount;
        return route.fallback();
    });
    await page.goto(address);
    await expect(welcome(page)).toBeVisible();
    await markDocument(page);
    await page
        .getByLabel("Email", { exact: true })
        .fill(firstAccount.user.email);
    await page
        .getByLabel("Password", { exact: true })
        .fill("Test-only-cobalt-79!");
    await page.locator("form").getByRole("button", { name: "Sign in" }).click();
    await expect(plants(page)).toBeVisible();
    await expectFreshDocument(page);
    await expect(page.getByText(changedNotice)).toHaveCount(0);
});

test("a sign-in completed in another window ends in a fresh document", async ({
    page,
    context,
}) => {
    let account: Account = null;
    const other = await context.newPage();
    for (const tab of [page, other]) {
        await mockApi(tab, { anonymous: true });
        await serveSession(tab, () => account);
        await tab.route("**/api/auth/browser/v1/auth/login", (route) => {
            account = firstAccount;
            return route.fallback();
        });
    }
    await page.goto(address);
    await expect(welcome(page)).toBeVisible();
    await markDocument(page);
    await other.goto("/app/");
    await other
        .getByLabel("Email", { exact: true })
        .fill(firstAccount.user.email);
    await other
        .getByLabel("Password", { exact: true })
        .fill("Test-only-cobalt-79!");
    await other
        .locator("form")
        .getByRole("button", { name: "Sign in" })
        .click();
    await expect(plants(other)).toBeVisible();
    await expect(plants(page)).toBeVisible();
    await expectFreshDocument(page);
});

test("a first load that is signed in stays the document it loaded as", async ({
    page,
}) => {
    await mockApi(page);
    await serveSession(page, () => firstAccount);
    await page.goto(address);
    await expect(plants(page)).toBeVisible();
    await expect(
        page.getByRole("button", { name: /East array/ }),
    ).toBeVisible();
    expect(await navigationType(page)).toBe("navigate");
});

test("a document restored from the back-forward cache reloads", async ({
    page,
}) => {
    await signedInDocument(page, () => firstAccount);
    const reloaded = page.waitForEvent("load");
    await page.evaluate(() =>
        window.dispatchEvent(
            new PageTransitionEvent("pageshow", { persisted: true }),
        ),
    );
    await reloaded;
    await expect(plants(page)).toBeVisible();
    await expectFreshDocument(page, "tag-a");
});

test("an expired session reloads over a staged correction without asking", async ({
    page,
}) => {
    let account: Account = firstAccount;
    const questions: string[] = [];
    page.on("dialog", (dialog) => {
        questions.push(dialog.type());
        void dialog.dismiss();
    });
    await page.clock.setFixedTime(new Date("2026-11-10T12:00:00Z"));
    await mockApi(page);
    await serveSession(page, () => account);
    await page.route("**/api/tenants/", (route) =>
        route.fulfill({
            json: {
                results: [
                    {
                        id: 1,
                        name: "Workspace",
                        role: "viewer",
                        pending_review_count: null,
                        unread_outcome_count: 0,
                    },
                ],
                count: 1,
                next: null,
                previous: null,
            },
        }),
    );
    await page.route("**/change-requests/?*", (route) =>
        route.fulfill({
            json: { results: [], count: 0, next: null, previous: null },
        }),
    );
    await page.route("**/api/**/production/?*", (route) => {
        const date = new URL(route.request().url()).searchParams.get(
            "start_date",
        )!;
        return route.fulfill({
            json: nativeProductionHistory(date, "Europe/Istanbul", [
                {
                    starts_at: `${date}T08:00:00+03:00`,
                    ends_at: `${date}T08:30:00+03:00`,
                    effective: {
                        energy_mwh: 1,
                        quality: "valid",
                        source: "meter",
                        source_kind: "manual",
                        provisional: false,
                        aggregated: false,
                    },
                    can_edit: false,
                    can_propose: true,
                },
            ]),
        });
    });
    await page.goto("/app/");
    await page.getByRole("button", { name: /East array/ }).click();
    await page.getByRole("button", { name: "Production", exact: true }).click();
    await page
        .getByRole("button", { name: "Suggest correction for 08:00" })
        .click();
    const editor = page.locator(".production-row-editor");
    await editor.getByRole("textbox").first().fill("0,5");
    await editor
        .getByRole("button", { name: "Add to request", exact: true })
        .click();
    await expect(
        page.getByRole("region", { name: "Correction request", exact: true }),
    ).toBeVisible();
    await markDocument(page);
    account = null;
    await focusWindow(page);
    await expect(welcome(page)).toBeVisible();
    await expect(page.getByText(expiredNotice)).toBeVisible();
    expect(await isMarked(page)).toBe(false);
    expect(questions).toEqual([]);
});
