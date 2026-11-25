import { expect, test, type Page } from "./support/test";
import {
    answer,
    firstAccount,
    mockApi,
    paged,
    type AccountState,
} from "./support/workspace";
import {
    changedNotice,
    expiredNotice,
    focusWindow,
    holdReload,
    isMarked,
    markDocument,
    markedTenantRequests,
    navigationType,
    outstandingAnswers,
    refuseReload,
    restoreFromCache,
    secondAccount,
    serveSession,
    tagObserver,
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
        (await markedTenantRequests(page)).filter((tag) => tag !== loadedWith),
    ).toEqual([]);
}

async function signedInDocument(page: Page) {
    const account = await mockApi(page);
    await serveSession(page, account);
    await page.goto(address);
    await expect(plants(page)).toBeVisible();
    await markDocument(page);
    return account;
}

test("an expired session ends in a fresh document that tells once that the session expired", async ({
    page,
}) => {
    const account = await signedInDocument(page);
    account.current = null;
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
    await signedInDocument(page);
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
    const account: AccountState = { current: firstAccount };
    const other = await context.newPage();
    for (const tab of [page, other]) {
        await mockApi(tab, { account });
        await serveSession(tab, account);
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
    ["another user's session", secondAccount],
    [
        "a new session of the same user",
        { user: firstAccount.user, tag: "tag-a2" },
    ],
] as const) {
    test(`${change} ends in a fresh document that tells once that the session changed`, async ({
        page,
    }) => {
        const account = await signedInDocument(page);
        const observe = await tagObserver(page);
        account.current = next;
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
    await serveSession(page, await mockApi(page, { anonymous: true }));
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
    const account: AccountState = { current: null };
    const other = await context.newPage();
    for (const tab of [page, other]) {
        await mockApi(tab, { account });
        await serveSession(tab, account);
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

test("without session storage an expired session ends in one fresh document without a notice", async ({
    page,
}) => {
    await page.addInitScript(() =>
        Object.defineProperty(window, "sessionStorage", {
            get() {
                throw new DOMException("Storage is blocked.", "SecurityError");
            },
        }),
    );
    const account = await mockApi(page);
    await serveSession(page, account);
    await page.goto(address);
    await expect(plants(page)).toBeVisible();
    const mark = () =>
        page.evaluate(() => Object.assign(window, { documentMarker: true }));
    await mark();
    account.current = null;
    await focusWindow(page);
    await expect(welcome(page)).toBeVisible();
    expect(await isMarked(page)).toBe(false);
    await expect(page.getByText(expiredNotice)).toHaveCount(0);
    await mark();
    const read = page.waitForResponse("**/api/session/");
    await focusWindow(page);
    await read;
    await expect(welcome(page)).toBeVisible();
    expect(await isMarked(page)).toBe(true);
});

test("a first load that is signed in stays the document it loaded as", async ({
    page,
}) => {
    await serveSession(page, await mockApi(page));
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
    await signedInDocument(page);
    const reloaded = page.waitForEvent("load");
    await restoreFromCache(page);
    await reloaded;
    await expect(plants(page)).toBeVisible();
    await expectFreshDocument(page, "tag-a");
    await expect(page.getByText(changedNotice)).toHaveCount(0);
});

async function stagedCorrection(page: Page) {
    const questions: string[] = [];
    page.on("dialog", (dialog) => {
        questions.push(dialog.type());
        void dialog.dismiss();
    });
    await page.clock.setFixedTime(new Date("2026-11-10T12:00:00Z"));
    const account = await mockApi(page);
    await serveSession(page, account);
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
    await page.getByRole("button", { name: "Edit 08:00" }).click();
    const editor = page.locator(".production-row-editor");
    await editor.getByRole("textbox").first().fill("0,5");
    await editor
        .getByRole("button", { name: "Add to change request", exact: true })
        .click();
    await expect(
        page.getByRole("region", { name: "Change request", exact: true }),
    ).toBeVisible();
    await markDocument(page);
    return { questions, account };
}

test("an expired session reloads over a staged correction without asking", async ({
    page,
}) => {
    const { questions, account } = await stagedCorrection(page);
    account.current = null;
    await focusWindow(page);
    await expect(welcome(page)).toBeVisible();
    await expect(page.getByText(expiredNotice)).toBeVisible();
    expect(await isMarked(page)).toBe(false);
    expect(questions).toEqual([]);
});

test("a restored document hides a staged correction before it reloads and does not ask", async ({
    page,
}) => {
    const { questions } = await stagedCorrection(page);
    await page.evaluate(() =>
        window.addEventListener("beforeunload", () =>
            window.sessionStorage.setItem(
                "shownAtUnload",
                document.body.innerText,
            ),
        ),
    );
    await restoreFromCache(page);
    await expect(
        page.getByRole("button", { name: "Edit 08:00" }),
    ).toBeVisible();
    expect(await isMarked(page)).toBe(false);
    expect(questions).toEqual([]);
    const shown = await page.evaluate(() =>
        window.sessionStorage.getItem("shownAtUnload"),
    );
    expect(shown).not.toBeNull();
    expect(shown).not.toContain("Change request");
    expect(shown).not.toContain("East array");
});

test("a restored document asks for a fresh load with only the loading state shown", async ({
    page,
}) => {
    await signedInDocument(page);
    const { refused } = await refuseReload(page);
    await restoreFromCache(page);
    await refused;
    await expect(
        page.getByRole("progressbar", { name: "Loading…" }),
    ).toBeVisible();
    await expect(plants(page)).toHaveCount(0);
    expect(await isMarked(page)).toBe(true);
});

const calibrationRun = answer("CalibrationOutput", {
    id: 40,
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
    selection_state: "active",
    assessment: "evaluated",
    factor: 1.1,
    result: {},
    dataset_sha256: "",
    activation_blocker: null,
    dataset_manifest: {},
});

const calibrationInForce = answer("CalibrationAutomationOutput", {
    enabled: false,
    authorized: false,
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
});

function heldAfter(holding: () => boolean) {
    let reach!: () => void;
    const reached = new Promise<void>((resolve) => (reach = resolve));
    let release!: () => void;
    const released = new Promise<void>((resolve) => (release = resolve));
    return {
        reached,
        release,
        pass: async () => {
            if (!holding()) return;
            reach();
            await released;
        },
    };
}

test("an action still running when another user's session arrives sends nothing for that session", async ({
    page,
}) => {
    const account = await mockApi(page);
    await serveSession(page, account);
    let reset = false;
    const list = heldAfter(() => reset);
    const automation = heldAfter(() => reset);
    await page.route("**/plants/21/calibrations/", async (route) => {
        await list.pass();
        await route.fulfill({ json: paged([calibrationRun]) });
    });
    await page.route("**/plants/21/calibrations/40/", (route) =>
        route.fulfill({ json: calibrationRun }),
    );
    await page.route("**/plants/21/calibration/reset/", (route) => {
        reset = true;
        return route.fulfill({ json: {} });
    });
    await page.route("**/plants/21/calibration/automation/", async (route) => {
        await automation.pass();
        await route.fulfill({ json: calibrationInForce });
    });
    await page.goto("/app/");
    await page.getByRole("button", { name: /East array/ }).click();
    await page
        .getByRole("button", { name: "Calibration", exact: true })
        .click();
    await page
        .getByRole("row", { name: /^40\s/ })
        .getByRole("button", { name: "View details" })
        .click();
    await expect(page.locator(".calibration-detail .tags")).toContainText(
        "In use",
    );
    await markDocument(page);
    const outstanding = await outstandingAnswers(page);
    const reload = await holdReload(page);
    const observe = await tagObserver(page);
    await page
        .getByRole("button", { name: "Use forecasts without calibration" })
        .click();
    await page
        .getByRole("button", { name: "Use forecasts without calibration" })
        .click();
    await Promise.all([list.reached, automation.reached]);
    account.current = secondAccount;
    await observe("tag-b");
    await reload.reached;
    const automationOnly = outstanding(1);
    list.release();
    await automationOnly;
    const none = outstanding(0);
    automation.release();
    await none;
    reload.release();
    await expect(
        page.getByRole("heading", { name: "Calibrations", exact: true }),
    ).toBeVisible();
    expect(await isMarked(page)).toBe(false);
    expect(
        (await markedTenantRequests(page)).filter((tag) => tag !== "tag-a"),
    ).toEqual([]);
});
