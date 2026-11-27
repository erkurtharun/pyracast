import { expect, instant, test, type Page } from "./support/test";
import {
    firstAccount,
    mockApi,
    paged,
    tenantRow,
    type Account,
    type AccountState,
} from "./support/workspace";
import {
    focusWindow,
    idleSeconds,
    isMarked,
    markDocument,
    openInvitations,
    outstandingSessionReads,
    refuseReload,
    secondAccount,
    serveSession,
    sessionAnswer,
    tagObserver,
} from "./support/session";
import { errorBody } from "./support/errors";
import { nativeProductionHistory } from "./support/native-production";
import {
    anEndedMark,
    blockDeletion,
    callStore,
    countWrites,
    dayMs,
    endedMark,
    heldDays,
    holderOf,
    holdCopy,
    holdHolderWrite,
    loadStore,
    offMark,
    onBlankPage,
    readCopy,
    refusalCases,
    refuseClears,
    seedCopy,
    watchStoreChunk,
    writeStore,
    type Holder,
} from "./support/saved-copy";

const idleLimit = idleSeconds * 1000;
const tick = 30_000;
const signOutPath = "**/api/auth/browser/v1/auth/session";
const unchecked =
    "The figures kept on this device could not be checked. Try again.";
const unreachable =
    "Pyracast could not be reached. Check your connection and try again.";
const untouched = {
    version: 1,
    holder: holderOf(),
    plants: 1,
    days: heldDays.length,
    flag: "1",
    marks: [],
};
const absent = { plants: 0, days: 0, flag: null, marks: [] };
const cleared = { version: 1, plants: 0, days: 0, flag: "1", marks: [] };

function plants(page: Page) {
    return page.getByRole("heading", { name: "Plants", exact: true });
}

function welcome(page: Page) {
    return page.getByRole("heading", { name: "Welcome back" });
}

function uncheckedNotice(page: Page) {
    return page.getByText(unchecked, { exact: true });
}

function tryAgain(page: Page) {
    return page.getByRole("button", { name: "Try again" });
}

async function expectUnchecked(page: Page) {
    await expect(uncheckedNotice(page)).toBeVisible();
    await expect(tryAgain(page)).toHaveCount(1);
    await expect(plants(page)).toHaveCount(0);
}

async function signedIn(page: Page, account?: AccountState) {
    const state = await mockApi(page, account && { account });
    await serveSession(page, state);
    return state;
}

async function signOut(page: Page) {
    await page.getByRole("button", { name: "Sign out", exact: true }).click();
}

async function submitSignIn(page: Page, account: AccountState, next: Account) {
    await page.route("**/api/auth/browser/v1/auth/login", (route) => {
        account.current = next;
        return route.fulfill({
            json: { status: 200, meta: { is_authenticated: true } },
        });
    });
    await page.getByLabel("Email", { exact: true }).fill("first@example.test");
    await page
        .getByLabel("Password", { exact: true })
        .fill("Test-only-cobalt-79!");
    await page.locator("form").getByRole("button", { name: "Sign in" }).click();
}

async function signInAs(page: Page, account: AccountState, next: Account) {
    await submitSignIn(page, account, next);
    await expect(plants(page)).toBeVisible();
}

async function anonymousStart(page: Page) {
    const account = await signedIn(page, { current: null });
    await page.goto("/app/", { waitUntil: "commit" });
    await expect(welcome(page)).toBeVisible();
    return account;
}

async function holdNextRead(page: Page) {
    let reach!: () => void;
    const reached = new Promise<void>((resolve) => (reach = resolve));
    let release!: () => void;
    const released = new Promise<void>((resolve) => (release = resolve));
    await page.route(
        "**/api/session/",
        async (route) => {
            reach();
            await released;
            await route.fallback();
        },
        { times: 1 },
    );
    return { reached, release };
}

// The server keeps the language a change sets; a change that `signsIn` leaves that account signed in.
async function serveLanguage(
    page: Page,
    account: AccountState,
    { held, signsIn }: { held?: Promise<void>; signsIn?: Account } = {},
) {
    let language = "en";
    const answer = () => ({
        ...sessionAnswer(account.current, null),
        language,
    });
    await page.route("**/api/session/", (route) =>
        route.fulfill({ json: answer() }),
    );
    await page.route("**/api/session/language/", async (route) => {
        await held;
        ({ language } = route.request().postDataJSON() as { language: string });
        if (signsIn) account.current = signsIn;
        await route.fulfill({ json: answer() });
    });
}

const signedOutHere = anEndedMark(firstAccount.tag);

function abortSignOut(page: Page) {
    return page.route(signOutPath, (route) =>
        route.request().method() === "DELETE"
            ? route.abort()
            : route.fallback(),
    );
}

function answerLanguage(page: Page, account: Account, held?: Promise<void>) {
    return page.route("**/api/session/language/", async (route) => {
        await held;
        await route.fulfill({
            json: { ...sessionAnswer(account, null), language: "tr" },
        });
    });
}

function countRequests(page: Page, pathname: string) {
    let count = 0;
    page.on("request", (request) => {
        if (new URL(request.url()).pathname === pathname) count += 1;
    });
    return () => count;
}

function observedTag(page: Page) {
    return page.evaluate(() =>
        window.localStorage.getItem("pyracast.sessionTag"),
    );
}

function documentLanguage(page: Page) {
    return page.evaluate(() => document.documentElement.lang);
}

async function pauseClock(page: Page) {
    const now = await page.evaluate(() => Date.now());
    await page.clock.pauseAt(now + 1_000);
    return now + 1_000;
}

async function confirmedAt(page: Page) {
    return (await readCopy(page)).holder?.confirmedAt ?? 0;
}

// The fixture's clock starts at the shared instant, so a confirmation renewed by this run lies near it.
async function renewed(page: Page) {
    return (
        Math.abs((await confirmedAt(page)) - instant.getTime()) < 60 * 60_000
    );
}

async function expectHeldFigures(page: Page) {
    expect(await readCopy(page)).toMatchObject({ plants: 1, days: 2 });
}

// The clock pauses before the sign-in is submitted, so its answer is admitted at the returned instant.
async function heldSignIn(page: Page, next: Account) {
    const writes = await countWrites(page);
    const account = await anonymousStart(page);
    await markDocument(page);
    const reads = await outstandingSessionReads(page);
    const release = await holdCopy(page);
    const admitted = await pauseClock(page);
    const answered = reads(0);
    await submitSignIn(page, account, next);
    await answered;
    await expect.poll(writes).toBe(1);
    return { release, admitted };
}

test("without a copy a signed-in start, another user's sign-in and a language change load no store, and a sign-out records only its session's end", async ({
    page,
}) => {
    const account = await signedIn(page);
    const chunk = await watchStoreChunk(page);
    await answerLanguage(page, secondAccount);
    await page.goto("/app/w/1/");
    await expect(plants(page)).toBeVisible();
    expect(chunk.requested()).toBe(false);
    await signOut(page);
    await expect(welcome(page)).toBeVisible();
    const ended = {
        version: 1,
        plants: 0,
        days: 0,
        flag: null,
        marks: [signedOutHere],
    };
    expect(await readCopy(page)).toEqual(ended);
    const later = await watchStoreChunk(page);
    await signInAs(page, account, secondAccount);
    await page.getByLabel("Language", { exact: true }).selectOption("tr");
    await expect(page.locator("html")).toHaveAttribute("lang", "tr");
    expect(later.requested()).toBe(false);
    expect(await readCopy(page)).toEqual(ended);
});

test("a flag without a database creates none at a signed-in start, and a sign-out creates it with only its session's end", async ({
    page,
}) => {
    await onBlankPage(page);
    await page.evaluate(() =>
        window.localStorage.setItem("pyracast.saved", "1"),
    );
    await signedIn(page);
    await page.goto("/app/w/1/");
    await expect(plants(page)).toBeVisible();
    expect(await readCopy(page)).toEqual({ ...absent, flag: "1" });
    await signOut(page);
    await expect(welcome(page)).toBeVisible();
    expect(await readCopy(page)).toEqual({
        ...cleared,
        marks: [signedOutHere],
    });
});

test("a start as the holder with the stored tag keeps every figure and renews the confirmation and the language", async ({
    page,
}) => {
    await seedCopy(page, { holder: holderOf({ language: "tr" }) });
    await expectHeldFigures(page);
    await signedIn(page);
    await page.goto("/app/w/1/");
    await expect(plants(page)).toBeVisible();
    await expect.poll(() => renewed(page)).toBe(true);
    expect(await readCopy(page)).toMatchObject({
        holder: { session: "tag-a", active: true, language: "en" },
        plants: 1,
        days: 2,
        flag: "1",
    });
});

test("a start with a new tag activates a paused copy", async ({ page }) => {
    await seedCopy(page, {
        holder: holderOf({ session: "tag-old", active: false }),
        plants: [],
        days: [],
    });
    await signedIn(page);
    await page.goto("/app/w/1/");
    await expect(plants(page)).toBeVisible();
    await expect.poll(() => renewed(page)).toBe(true);
    expect((await readCopy(page)).holder).toMatchObject({
        session: "tag-a",
        active: true,
    });
});

test("a holder whose removal token is written in capitals stays this person's copy and keeps its token", async ({
    page,
}) => {
    const holder = holderOf({ removal: "0123456789ABCDEF0123456789ABCDEF" });
    await seedCopy(page, { holder });
    await expectHeldFigures(page);
    await signedIn(page);
    await page.goto("/app/w/1/");
    await expect(plants(page)).toBeVisible();
    await expect.poll(() => renewed(page)).toBe(true);
    expect(await readCopy(page)).toMatchObject({
        holder: { removal: holder.removal, session: "tag-a", active: true },
        plants: 1,
        days: 2,
        flag: "1",
    });
});

test("a sign-out whose request is lost removes every figure, a second window of that session keeps the copy paused and the next sign-in activates it", async ({
    page,
    context,
}) => {
    await seedCopy(page);
    await expectHeldFigures(page);
    const account: AccountState = { current: firstAccount };
    await signedIn(page, account);
    await page.goto("/app/w/1/");
    await expect(plants(page)).toBeVisible();
    await expect.poll(() => renewed(page)).toBe(true);
    await abortSignOut(page);
    await signOut(page);
    await expect(tryAgain(page)).toBeVisible();
    expect(await readCopy(page)).toMatchObject({
        holder: { session: "tag-a", active: false },
        plants: 0,
        days: 0,
        marks: [signedOutHere],
    });
    const before = await confirmedAt(page);
    await page.clock.fastForward(60_000);
    const other = await context.newPage();
    await signedIn(other, account);
    await other.goto("/app/w/1/");
    await expect(plants(other)).toBeVisible();
    await expect
        .poll(() => confirmedAt(page))
        .toBeGreaterThanOrEqual(before + 60_000);
    expect((await readCopy(page)).holder).toMatchObject({
        session: "tag-a",
        active: false,
    });
    await page.unroute(signOutPath);
    await tryAgain(page).click();
    await expect(welcome(page)).toBeVisible();
    await signInAs(page, account, { ...firstAccount, tag: "tag-c" });
    await expect
        .poll(async () => (await readCopy(page)).holder?.session)
        .toBe("tag-c");
    expect((await readCopy(page)).holder?.active).toBe(true);
});

test("a sign-out whose store code loads late sends its request at two seconds and records the session's end once the code loads, before the page reloads", async ({
    page,
}) => {
    await seedCopy(page, { flagged: false });
    await signedIn(page);
    const chunk = await watchStoreChunk(page, true);
    await page.goto("/app/w/1/");
    await expect(plants(page)).toBeVisible();
    expect(chunk.requested()).toBe(false);
    await page.evaluate(() =>
        window.localStorage.setItem("pyracast.saved", "1"),
    );
    await expectHeldFigures(page);
    const deletes: string[] = [];
    page.on("request", (request) => {
        if (request.method() === "DELETE") deletes.push(request.url());
    });
    await pauseClock(page);
    await signOut(page);
    await chunk.reached;
    await page.clock.runFor(1_999);
    expect(deletes).toEqual([]);
    const sent = page.waitForRequest(
        (request) => request.method() === "DELETE",
    );
    await page.clock.runFor(1);
    expect(new URL((await sent).url()).pathname).toBe(
        "/api/auth/browser/v1/auth/session",
    );
    await expectHeldFigures(page);
    const reloaded = page.waitForEvent("load");
    chunk.release();
    await reloaded;
    await page.clock.resume();
    await expect(welcome(page)).toBeVisible();
    expect(deletes).toHaveLength(1);
    expect(await readCopy(page)).toMatchObject({
        holder: { active: false },
        plants: 0,
        days: 0,
        marks: [signedOutHere],
    });
});

test("a sign-out whose store code loads late leaves a copy that another session holds when its record lands", async ({
    page,
    context,
}) => {
    await seedCopy(page, { flagged: false });
    const account = await signedIn(page);
    const chunk = await watchStoreChunk(page, true);
    await page.goto("/app/w/1/");
    await expect(plants(page)).toBeVisible();
    await page.evaluate(() =>
        window.localStorage.setItem("pyracast.saved", "1"),
    );
    await expectHeldFigures(page);
    await abortSignOut(page);
    await pauseClock(page);
    await signOut(page);
    await chunk.reached;
    await page.clock.runFor(2_000);
    await expect(tryAgain(page)).toBeVisible();
    await expectHeldFigures(page);
    const newer = holderOf({ session: "tag-c" });
    await seedCopy(await context.newPage(), { holder: newer });
    account.current = { ...firstAccount, tag: "tag-c" };
    const other = await context.newPage();
    await signedIn(other, account);
    await other.goto("/app/w/1/");
    await expect
        .poll(() => readCopy(other))
        .toMatchObject({
            holder: { session: "tag-c", active: true },
            plants: 1,
            days: 2,
            marks: [],
        });
    chunk.release();
    await expect
        .poll(() => readCopy(other))
        .toMatchObject({
            holder: { session: "tag-c", active: true },
            plants: 1,
            days: 2,
            marks: [signedOutHere],
        });
});

test("a sign-out removal that runs after another session activated the copy leaves that holder and its figures", async ({
    page,
}) => {
    await seedCopy(page);
    await signedIn(page);
    const writes = await countWrites(page);
    await page.goto("/app/w/1/");
    await expect(plants(page)).toBeVisible();
    await expect.poll(() => renewed(page)).toBe(true);
    await expectHeldFigures(page);
    await abortSignOut(page);
    const release = await holdCopy(page);
    await pauseClock(page);
    await signOut(page);
    await expect.poll(writes).toBe(2);
    await page.clock.runFor(2_000);
    await expect(tryAgain(page)).toBeVisible();
    const newer = holderOf({
        session: "tag-c",
        confirmedAt: instant.getTime(),
    });
    await release(newer);
    await expect
        .poll(() => readCopy(page))
        .toEqual({ ...untouched, holder: newer, marks: [signedOutHere] });
});

test("a start of a session whose end is recorded keeps its copy paused, and a new session activates it", async ({
    page,
}) => {
    const ended = endedMark("tag-a");
    await seedCopy(page, {
        holder: holderOf({ active: false }),
        plants: [],
        days: [],
        marks: [ended],
    });
    const account = await signedIn(page);
    await page.goto("/app/w/1/");
    await expect(plants(page)).toBeVisible();
    await expect.poll(() => renewed(page)).toBe(true);
    expect(await readCopy(page)).toMatchObject({
        holder: { session: "tag-a", active: false },
        marks: [ended],
    });
    await signOut(page);
    await expect(welcome(page)).toBeVisible();
    await signInAs(page, account, { ...firstAccount, tag: "tag-c" });
    await expect
        .poll(async () => (await readCopy(page)).holder)
        .toMatchObject({ session: "tag-c", active: true });
});

test("another person's start removes the holder and the figures and keeps this device's marks and the flag", async ({
    page,
}) => {
    const marks = [endedMark("tag-old"), offMark()];
    await seedCopy(page, { marks });
    await expectHeldFigures(page);
    await signedIn(page, { current: secondAccount });
    await page.goto("/app/w/1/");
    await expect(plants(page)).toBeVisible();
    await expect.poll(() => readCopy(page)).toEqual({ ...cleared, marks });
});

for (const { name, marks, shown } of refusalCases(1, "tag-a"))
    test(`the store ${shown ? "shows and refreshes" : "neither shows nor refreshes"} an active copy ${name}`, async ({
        page,
    }) => {
        await seedCopy(page, { marks });
        await loadStore(page, true);
        const owner = { user: 1, session: "tag-a" };
        expect(await writeStore(page, "copyState", owner)).toMatchObject({
            state: shown ? "on" : "paused",
        });
        expect(Boolean(await writeStore(page, "held", owner))).toBe(shown);
        await expectHeldFigures(page);
    });

test("a workspace refusal that arrives while the saved-figures notice is shown reads no session before Try again", async ({
    page,
}) => {
    await seedCopy(page);
    await expectHeldFigures(page);
    const allowClears = await refuseClears(page);
    const account = await signedIn(page);
    let release!: () => void;
    const released = new Promise<void>((resolve) => (release = resolve));
    let held = false;
    await page.route("**/api/tenants/1/sites/", async (route) => {
        if (held) return route.fallback();
        held = true;
        await released;
        await route.fulfill({
            status: 409,
            json: errorBody("session_changed", "Sign in again."),
        });
    });
    const sent = page.waitForRequest("**/api/tenants/1/sites/");
    const sites = page.waitForResponse("**/api/tenants/1/sites/");
    await page.goto("/app/w/1/");
    await sent;
    account.current = secondAccount;
    await focusWindow(page);
    await expectUnchecked(page);
    await allowClears();
    const reads = countRequests(page, "/api/session/");
    release();
    await sites;
    await page.evaluate(() => 0);
    await expect(uncheckedNotice(page)).toBeVisible();
    expect(reads()).toBe(0);
    await tryAgain(page).click();
    await expect(plants(page)).toBeVisible();
    expect(await observedTag(page)).toBe(secondAccount.tag);
});

test("a signed-in answer held while another window signs in and out again, leaving the same observed tag, is not shown and sends no workspace request", async ({
    page,
    context,
}) => {
    await onBlankPage(page);
    await page.evaluate(() =>
        window.localStorage.setItem("pyracast.sessionTag", ""),
    );
    const account = await signedIn(page, { current: secondAccount });
    let reach!: () => void;
    const reached = new Promise<void>((resolve) => (reach = resolve));
    let release!: () => void;
    const released = new Promise<void>((resolve) => (release = resolve));
    await page.route(
        "**/api/session/",
        async (route) => {
            reach();
            await released;
            await route.fulfill({ json: sessionAnswer(firstAccount, null) });
        },
        { times: 1 },
    );
    await page.goto("/app/w/1/", { waitUntil: "commit" });
    await reached;
    const other = await context.newPage();
    await signedIn(other, account);
    await other.goto("/app/w/1/");
    await expect(plants(other)).toBeVisible();
    await signOut(other);
    await expect(welcome(other)).toBeVisible();
    expect(await observedTag(page)).toBe("");
    const workspaceReads = countRequests(page, "/api/tenants/");
    release();
    await expect(welcome(page)).toBeVisible();
    expect(workspaceReads()).toBe(0);
});

test("another user's copy whose clear is refused keeps a signed-in start from showing the session until a later try clears it", async ({
    page,
}) => {
    await seedCopy(page);
    await expectHeldFigures(page);
    const allowClears = await refuseClears(page);
    await signedIn(page, { current: secondAccount });
    const workspaceReads = countRequests(page, "/api/tenants/");
    await page.goto("/app/w/1/");
    await expectUnchecked(page);
    expect(await observedTag(page)).toBeNull();
    expect(workspaceReads()).toBe(0);
    expect(await readCopy(page)).toEqual(untouched);
    await allowClears();
    await tryAgain(page).click();
    await expect(plants(page)).toBeVisible();
    expect(await observedTag(page)).toBe(secondAccount.tag);
    await expect.poll(() => readCopy(page)).toEqual(cleared);
});

test("another person's sign-in over a copy whose clear is refused shows the saved-figures notice instead of their screens until a later try clears it", async ({
    page,
}) => {
    await seedCopy(page);
    await expectHeldFigures(page);
    const allowClears = await refuseClears(page);
    const account = await anonymousStart(page);
    await markDocument(page);
    await submitSignIn(page, account, secondAccount);
    await expectUnchecked(page);
    expect(await observedTag(page)).toBe("");
    expect(await isMarked(page)).toBe(true);
    expect(await readCopy(page)).toEqual(untouched);
    await allowClears();
    await tryAgain(page).click();
    await expect(plants(page)).toBeVisible();
    await expect.poll(() => readCopy(page)).toEqual(cleared);
});

async function refusedSignIn(page: Page) {
    await seedCopy(page);
    await expectHeldFigures(page);
    const allowClears = await refuseClears(page);
    const account = await anonymousStart(page);
    await submitSignIn(page, account, secondAccount);
    await expectUnchecked(page);
    await allowClears();
}

test("Try again keeps the saved-figures notice instead of the sign-in form until its session is handed over", async ({
    page,
}) => {
    await refusedSignIn(page);
    const read = await holdNextRead(page);
    await tryAgain(page).click();
    await read.reached;
    await expect(uncheckedNotice(page)).toBeVisible();
    expect(await welcome(page).count()).toBe(0);
    read.release();
    await expect(plants(page)).toBeVisible();
    await expect.poll(() => readCopy(page)).toEqual(cleared);
});

test("a Try again whose session read fails shows that failure and Try again instead of the sign-in form", async ({
    page,
}) => {
    await refusedSignIn(page);
    await page.route("**/api/session/", (route) => route.abort(), {
        times: 1,
    });
    await tryAgain(page).click();
    await expect(page.getByText(unreachable, { exact: true })).toBeVisible();
    await expect(tryAgain(page)).toHaveCount(1);
    expect(await welcome(page).count()).toBe(0);
    await tryAgain(page).click();
    await expect(plants(page)).toBeVisible();
});

test("while the saved-figures notice is shown neither focus nor the idle check reads the session", async ({
    page,
}) => {
    await seedCopy(page);
    await expectHeldFigures(page);
    const allowClears = await refuseClears(page);
    const account = await signedIn(page);
    const reads = countRequests(page, "/api/session/");
    await page.goto("/app/w/1/");
    await expect(plants(page)).toBeVisible();
    account.current = secondAccount;
    await focusWindow(page);
    await expectUnchecked(page);
    await allowClears();
    const before = reads();
    await pauseClock(page);
    await focusWindow(page);
    await page.clock.runFor(idleLimit + tick);
    await page.clock.resume();
    await expect(uncheckedNotice(page)).toBeVisible();
    expect(reads()).toBe(before);
    await tryAgain(page).click();
    await expect(plants(page)).toBeVisible();
    expect(await observedTag(page)).toBe(secondAccount.tag);
});

test("another person's sign-in shows their screens only after the previous holder's figures are cleared", async ({
    page,
}) => {
    await seedCopy(page);
    await expectHeldFigures(page);
    const { release } = await heldSignIn(page, secondAccount);
    expect(await observedTag(page)).toBe("");
    expect(await isMarked(page)).toBe(true);
    await release();
    await page.clock.resume();
    await expect(plants(page)).toBeVisible();
    expect(await readCopy(page)).toEqual(cleared);
});

test("a sign-in whose clear waits two seconds from its answer is not shown, and the late work leaves the holder it then finds", async ({
    page,
}) => {
    await seedCopy(page);
    await expectHeldFigures(page);
    const { release, admitted } = await heldSignIn(page, secondAccount);
    await page.clock.runFor(1_999);
    expect(await uncheckedNotice(page).count()).toBe(0);
    await page.clock.runFor(1);
    await expectUnchecked(page);
    expect(await observedTag(page)).toBe("");
    expect(await isMarked(page)).toBe(true);
    const newer = holderOf({
        user: secondAccount.user.id,
        session: "tag-b-newer",
        confirmedAt: admitted + 2_000,
    });
    await release(newer);
    await page.clock.resume();
    expect(await readCopy(page)).toEqual({ ...untouched, holder: newer });
    await tryAgain(page).click();
    await expect(plants(page)).toBeVisible();
    expect(await readCopy(page)).toMatchObject({
        holder: { user: secondAccount.user.id, session: "tag-b", active: true },
        plants: 1,
        days: 2,
    });
});

test("a sign-in whose saved-figures code does not load within two seconds of its answer is not shown", async ({
    page,
}) => {
    await seedCopy(page);
    await expectHeldFigures(page);
    const chunk = await watchStoreChunk(page, true);
    const account = await anonymousStart(page);
    expect(chunk.requested()).toBe(false);
    await markDocument(page);
    const reads = await outstandingSessionReads(page);
    await pauseClock(page);
    const answered = reads(0);
    await submitSignIn(page, account, secondAccount);
    await answered;
    await chunk.reached;
    await page.clock.runFor(1_999);
    expect(await uncheckedNotice(page).count()).toBe(0);
    await page.clock.runFor(1);
    await expectUnchecked(page);
    expect(await observedTag(page)).toBe("");
    expect(await isMarked(page)).toBe(true);
    chunk.release();
    await page.clock.resume();
    await tryAgain(page).click();
    await expect(plants(page)).toBeVisible();
    await expect.poll(() => readCopy(page)).toEqual(cleared);
});

test("an adoption that reads the holder just before two seconds and still has its writes open then is undone", async ({
    page,
}) => {
    const holder = holderOf({ session: "tag-old" });
    await seedCopy(page, { holder });
    await expectHeldFigures(page);
    const writes = await countWrites(page);
    const write = await holdHolderWrite(page);
    await signedIn(page);
    const release = await holdCopy(page);
    await pauseClock(page);
    await page.goto("/app/w/1/", { waitUntil: "commit" });
    await expect.poll(writes).toBe(1);
    await page.clock.runFor(1_999);
    await write.arm();
    await release();
    await expect.poll(write.held).toBe(true);
    await page.clock.runFor(1);
    await expectUnchecked(page);
    expect(await observedTag(page)).toBeNull();
    await write.release();
    await page.clock.resume();
    expect(await readCopy(page)).toEqual({ ...untouched, holder });
});

test("a language answer that brings a signed-in session is shown only after the copy is handed over", async ({
    page,
}) => {
    await seedCopy(page);
    await expectHeldFigures(page);
    const writes = await countWrites(page);
    await signedIn(page, { current: null });
    let answer!: () => void;
    await answerLanguage(
        page,
        firstAccount,
        new Promise<void>((resolve) => (answer = resolve)),
    );
    const requested = page.waitForRequest("**/api/session/language/");
    await page.goto("/app/?language=tr");
    await requested;
    await markDocument(page);
    const release = await holdCopy(page);
    await pauseClock(page);
    answer();
    await expect.poll(writes).toBe(1);
    expect(await observedTag(page)).toBe("");
    expect(await documentLanguage(page)).toBe("en");
    expect(new URL(page.url()).search).toBe("?language=tr");
    expect(await isMarked(page)).toBe(true);
    const { refused } = await refuseReload(page);
    await release();
    await refused;
    expect(await observedTag(page)).toBe(firstAccount.tag);
});

test("a language answer that brings a signed-in session over another user's copy whose clear is refused is not shown", async ({
    page,
}) => {
    const holder = holderOf({ user: secondAccount.user.id, session: "tag-b" });
    await seedCopy(page, { holder });
    await expectHeldFigures(page);
    await refuseClears(page);
    await signedIn(page, { current: null });
    let answer!: () => void;
    await answerLanguage(
        page,
        firstAccount,
        new Promise<void>((resolve) => (answer = resolve)),
    );
    const requested = page.waitForRequest("**/api/session/language/");
    await page.goto("/app/?language=tr");
    await requested;
    await markDocument(page);
    answer();
    await expectUnchecked(page);
    expect(await observedTag(page)).toBe("");
    expect(await documentLanguage(page)).toBe("en");
    expect(new URL(page.url()).search).toBe("?language=tr");
    expect(await isMarked(page)).toBe(true);
    expect(await readCopy(page)).toEqual({ ...untouched, holder });
});

test("a language chosen on the screens is applied only after the copy is handed over", async ({
    page,
}) => {
    await seedCopy(page);
    await expectHeldFigures(page);
    const writes = await countWrites(page);
    await signedIn(page);
    await answerLanguage(page, firstAccount);
    await page.goto("/app/w/1/");
    await expect(plants(page)).toBeVisible();
    await expect.poll(writes).toBe(1);
    const release = await holdCopy(page);
    await pauseClock(page);
    await page.getByLabel("Language", { exact: true }).selectOption("tr");
    await expect.poll(writes).toBe(2);
    expect(await documentLanguage(page)).toBe("en");
    await expect(plants(page)).toBeVisible();
    await release();
    await expect(page.locator("html")).toHaveAttribute("lang", "tr");
});

test("a language answer whose hand-over another window's session change overtakes reads the session and shows it in the chosen language", async ({
    page,
}) => {
    await seedCopy(page);
    await expectHeldFigures(page);
    const writes = await countWrites(page);
    const account = await signedIn(page, { current: null });
    let answer!: () => void;
    await serveLanguage(page, account, {
        held: new Promise<void>((resolve) => (answer = resolve)),
        signsIn: firstAccount,
    });
    const requested = page.waitForRequest("**/api/session/language/");
    await page.goto("/app/?language=tr");
    await requested;
    const release = await holdCopy(page);
    const observer = await tagObserver(page);
    await pauseClock(page);
    answer();
    await expect.poll(writes).toBe(1);
    await observer("tag-z");
    const reloaded = page.waitForEvent("load");
    await release();
    await page.clock.resume();
    await reloaded;
    await expect(page.locator("html")).toHaveAttribute("lang", "tr");
    await expect.poll(() => new URL(page.url()).search).toBe("");
    expect(await observedTag(page)).toBe(firstAccount.tag);
});

test("a language chosen on the screens while a workspace change is sent is read back and shown as saved", async ({
    page,
}) => {
    await seedCopy(page);
    await expectHeldFigures(page);
    const writes = await countWrites(page);
    const account = await signedIn(page);
    await serveLanguage(page, account);
    await page.goto("/app/w/1/");
    await expect(plants(page)).toBeVisible();
    await expect.poll(writes).toBe(1);
    const resend = await openInvitations(page);
    await expect(resend).toBeVisible();
    const release = await holdCopy(page);
    await pauseClock(page);
    await page.getByLabel("Language", { exact: true }).selectOption("tr");
    await expect.poll(writes).toBe(2);
    const sent = page.waitForRequest("**/invitations/3/resend/");
    await resend.click();
    await sent;
    await release();
    await expect(page.locator("html")).toHaveAttribute("lang", "tr");
    await expect(page.getByLabel("Dil", { exact: true })).toHaveValue("tr");
});

test("an adoption held in one window does nothing once another window's sign-out has paused the copy", async ({
    page,
    context,
}) => {
    await seedCopy(page, { holder: holderOf({ session: "tag-old" }) });
    await expectHeldFigures(page);
    const account: AccountState = { current: firstAccount };
    await signedIn(page, account);
    const chunk = await watchStoreChunk(page, true);
    const answered = page.waitForResponse("**/api/session/");
    await pauseClock(page);
    await page.goto("/app/w/1/", { waitUntil: "commit" });
    await refuseReload(page);
    await answered;
    await chunk.reached;
    account.current = { ...firstAccount, tag: "tag-b" };
    const other = await context.newPage();
    await signedIn(other, account);
    await other.goto("/app/w/1/");
    await expect(
        other.getByRole("button", { name: "Sign out", exact: true }),
    ).toBeVisible();
    await signOut(other);
    await expect(welcome(other)).toBeVisible();
    expect(await readCopy(other)).toMatchObject({
        holder: { session: "tag-b", active: false },
        plants: 0,
        days: 0,
    });
    chunk.release();
    await expect(welcome(page)).toBeVisible();
    expect(await readCopy(page)).toMatchObject({
        holder: { session: "tag-b", active: false },
        plants: 0,
        days: 0,
    });
});

for (const [name, holder] of [
    [
        "whose fields are mistyped",
        {
            ...holderOf(),
            active: "false",
            confirmedAt: String(instant.getTime() - dayMs),
        } as unknown as Holder,
    ],
    ["without a session tag", holderOf({ session: "" })],
] as const)
    test(`a holder ${name} is removed at a start instead of renewed`, async ({
        page,
    }) => {
        await seedCopy(page, { holder });
        await expectHeldFigures(page);
        await signedIn(page);
        await page.goto("/app/w/1/");
        await expect(plants(page)).toBeVisible();
        await expect.poll(() => readCopy(page)).toEqual(cleared);
    });

for (const [when, confirmed] of [
    ["91 days before the clock", instant.getTime() - 91 * dayMs],
    ["two days ahead of the clock", instant.getTime() + 2 * dayMs],
] as const) {
    test(`a copy confirmed ${when} is emptied when its confirmation is renewed`, async ({
        page,
    }) => {
        await seedCopy(page, { holder: holderOf({ confirmedAt: confirmed }) });
        await expectHeldFigures(page);
        await signedIn(page);
        await page.goto("/app/w/1/");
        await expect(plants(page)).toBeVisible();
        await expect.poll(() => renewed(page)).toBe(true);
        expect(await readCopy(page)).toMatchObject({
            holder: { session: "tag-a", active: true },
            plants: 0,
            days: 0,
        });
    });
}

for (const [when, offset, kept] of [
    ["exactly 90 days before", -90 * dayMs, 1],
    ["a day and a millisecond after", dayMs + 1, 0],
] as const) {
    test(`a copy confirmed ${when} the answer arrived is judged at that arrival although its transaction runs a second later`, async ({
        page,
    }) => {
        await seedCopy(page);
        await expectHeldFigures(page);
        const { release, admitted } = await heldSignIn(page, firstAccount);
        await page.clock.runFor(1_000);
        const { refused } = await refuseReload(page);
        await release(holderOf({ confirmedAt: admitted + offset }));
        await refused;
        expect(await readCopy(page)).toMatchObject({
            holder: { confirmedAt: admitted, active: true },
            plants: kept,
            days: kept * 2,
        });
    });
}

test("a database of a newer format keeps a signed-in start from showing the session and is left as it is", async ({
    page,
}) => {
    const holder = holderOf();
    await seedCopy(page, { holder, version: 2 });
    await signedIn(page);
    await page.goto("/app/w/1/");
    await expectUnchecked(page);
    expect(await readCopy(page)).toEqual({ ...untouched, version: 2 });
});

test("a browser that refuses the copy's database while its flag can be read does not show a signed-in session", async ({
    page,
}) => {
    await onBlankPage(page);
    await page.evaluate(() =>
        window.localStorage.setItem("pyracast.saved", "1"),
    );
    await page.addInitScript(() => {
        IDBFactory.prototype.open = () => {
            throw new DOMException("Storage is blocked.", "SecurityError");
        };
    });
    await signedIn(page);
    await page.goto("/app/w/1/");
    await expectUnchecked(page);
    expect(await observedTag(page)).toBeNull();
});

test("a browser whose flag cannot be read and whose copy answers its open with an error does not show a signed-in session", async ({
    page,
}) => {
    await seedCopy(page, { version: 2 });
    await page.addInitScript(() => {
        Object.defineProperty(window, "localStorage", {
            get() {
                throw new DOMException("Storage is blocked.", "SecurityError");
            },
        });
    });
    await signedIn(page);
    await page.goto("/app/w/1/");
    await expectUnchecked(page);
});

test("a browser that refuses storage outright, the copy's flag included, signs in and out as before", async ({
    page,
}) => {
    await page.addInitScript(() => {
        Object.defineProperty(window, "localStorage", {
            get() {
                throw new DOMException("Storage is blocked.", "SecurityError");
            },
        });
        IDBFactory.prototype.open = () => {
            throw new DOMException("Storage is blocked.", "SecurityError");
        };
    });
    const account = await signedIn(page);
    await page.goto("/app/w/1/");
    await expect(plants(page)).toBeVisible();
    await signOut(page);
    await expect(welcome(page)).toBeVisible();
    await signInAs(page, account, firstAccount);
    await signOut(page);
    await expect(welcome(page)).toBeVisible();
});

test("the withdrawn build's removal deletes a held copy with its marks only after its figures were cleared", async ({
    page,
}) => {
    await seedCopy(page, { marks: [offMark()] });
    const allowClears = await refuseClears(page);
    const remove = async () => (await callStore(page, "removeCopy"))();
    await loadStore(page, false);
    expect((await remove()).deleted).toBe(0);
    await expectHeldFigures(page);
    await allowClears();
    expect((await remove()).deleted).toBe(1);
    expect(await readCopy(page)).toEqual({ ...absent, flag: "1" });
    expect((await remove()).deleted).toBe(2);
});

test("the store answers within its limit while another window keeps the copy from being deleted, and reports the deletion once it succeeds", async ({
    page,
}) => {
    await seedCopy(page);
    await expectHeldFigures(page);
    const deletion = await blockDeletion(page);
    await loadStore(page, false);
    await pauseClock(page);
    const started = await callStore(page, "removeCopy");
    await expect.poll(deletion.asked).toBe(true);
    await page.clock.runFor(2_000);
    expect((await started()).deleted).toBe(0);
    const state = await callStore(page, "copyState");
    await page.clock.runFor(2_000);
    expect(await state()).toEqual({
        answer: { state: "unavailable", plants: 0, off: false },
        deleted: 0,
    });
    await deletion.release();
    await expect.poll(async () => (await state()).deleted).toBe(1);
    expect((await readCopy(page)).version).toBeUndefined();
});

async function stagedCorrection(page: Page) {
    const account = await signedIn(page);
    await page.route("**/api/tenants/", (route) =>
        route.fulfill({
            json: paged([
                tenantRow({ id: 1, name: "Workspace", role: "viewer" }),
            ]),
        }),
    );
    await page.route("**/change-requests/?*", (route) =>
        route.fulfill({ json: paged([]) }),
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
    return account;
}

test("a covered window with saved figures offers them and asks before it leaves a staged correction", async ({
    page,
}) => {
    await seedCopy(page);
    await stagedCorrection(page);
    await markDocument(page);
    await page.route("**/api/**", (route) => route.abort());
    const now = await page.evaluate(() => Date.now());
    await page.clock.pauseAt(now + idleLimit + tick);
    await expect(
        page.getByRole("heading", { name: "Pyracast cannot be reached" }),
    ).toBeVisible();
    const showSaved = page.getByRole("button", {
        name: "Show last loaded figures",
        exact: true,
    });
    const questions: string[] = [];
    page.on("dialog", (dialog) => {
        questions.push(dialog.type());
        void dialog.dismiss();
    });
    await showSaved.click();
    await expect.poll(() => questions).toEqual(["beforeunload"]);
    expect(await isMarked(page)).toBe(true);
    await expect(showSaved).toBeVisible();
});
