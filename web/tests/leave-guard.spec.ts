import { expect, test, type Page } from "./support/test";
import { errorBody } from "./support/errors";
import {
    answer as apiAnswer,
    mockApi,
    paged,
    plant,
    region,
    reminderApi,
    openReminders,
} from "./support/workspace";
import {
    activeOutage,
    answers,
    focusWindow,
    now,
    openPlant,
    reviewRequest,
    serveDirectProduction,
    serveImport,
    serveOutageWrites,
    serveProduction,
    signIn,
} from "./support/plant-screens";

test.use({ timezoneId: "Europe/Istanbul" });

// Whether a full load would ask first; the event marks the document as asked like a real question.
function asks(page: Page) {
    return page.evaluate(() => {
        const leaving = new Event("beforeunload", { cancelable: true });
        window.dispatchEvent(leaving);
        return leaving.defaultPrevented;
    });
}

// Asks right after the commit in which the text appears or goes, before any later task of the page runs.
async function asksOnce(
    page: Page,
    text: { shown: string } | { gone: string },
) {
    await page.evaluate((text) => {
        const answer = new Promise<boolean>((resolve) => {
            const observer = new MutationObserver(() => {
                const content = document.body.textContent ?? "";
                if (
                    "shown" in text
                        ? !content.includes(text.shown)
                        : content.includes(text.gone)
                )
                    return;
                observer.disconnect();
                const leaving = new Event("beforeunload", { cancelable: true });
                window.dispatchEvent(leaving);
                resolve(leaving.defaultPrevented);
            });
            observer.observe(document.body, {
                subtree: true,
                childList: true,
                characterData: true,
            });
        });
        Object.assign(window, { pyracastShownAnswer: answer });
    }, text);
    return () =>
        page.evaluate(
            () =>
                Reflect.get(window, "pyracastShownAnswer") as Promise<boolean>,
        );
}

async function markDocument(page: Page) {
    await page.evaluate(() => {
        Object.assign(window, { pyracastMarker: true });
    });
}

function marked(page: Page) {
    return page.evaluate(() => "pyracastMarker" in window);
}

async function expectReloaded(page: Page) {
    await expect.poll(() => marked(page).catch(() => true)).toBe(false);
}

// Every question the browser asks before the document is left, each answered Leave so the load goes on.
function questions(page: Page) {
    const asked: string[] = [];
    page.on("dialog", (dialog) => {
        asked.push(dialog.type());
        void dialog.accept();
    });
    return asked;
}

// The router's requests, which a soft change sends and a document load does not.
function routerRequests(page: Page) {
    const sent: string[] = [];
    page.on("request", (request) => {
        if (request.headers()["rsc"]) sent.push(request.url());
    });
    return sent;
}

// A start that loads a document resolves only once that load ends, so Leave does not wait for it.
async function answer(
    page: Page,
    leave: boolean,
    start: () => Promise<unknown>,
) {
    const asked = new Promise<string>((resolve) =>
        page.once("dialog", (dialog) => {
            resolve(dialog.type());
            void (leave ? dialog.accept() : dialog.dismiss());
        }),
    );
    const started = start();
    expect(await asked).toBe("beforeunload");
    if (leave) void started.catch(() => undefined);
    else await started;
}

// A traversal the browser asks about never finishes loading when Stay is chosen, so Playwright's own Back and Forward would wait for a load that does not come.
const traverse = (page: Page, direction: "back" | "forward") =>
    page.evaluate(
        (to) => history[to === "back" ? "back" : "forward"](),
        direction,
    );

const stay = (page: Page, start: () => Promise<unknown>) =>
    answer(page, false, start);

function editorValue(page: Page) {
    return page.locator(".production-row-editor").getByRole("textbox").first();
}

function saveValue(page: Page) {
    return page
        .locator(".production-row-editor")
        .getByRole("button", { name: "Save", exact: true })
        .click();
}

async function editValue(page: Page, value: string) {
    await page.getByRole("button", { name: /^Edit 08:00/ }).click();
    await editorValue(page).fill(value);
}

function actualsOf(page: Page) {
    return page.locator("fieldset", {
        has: page.getByRole("heading", { name: "Actual production" }),
    });
}

const meter = (name: string) => ({
    name,
    mimeType: "text/csv",
    buffer: Buffer.from(
        "starts_at,ends_at,energy_kwh,quality,source\n2026-11-09T08:00:00+03:00,2026-11-09T08:30:00+03:00,250,valid,meter",
    ),
});

async function openImport(page: Page) {
    await page.getByRole("button", { name: "Import CSV", exact: true }).click();
}

test("nothing unsaved and the sign-in form never ask before a full load", async ({
    page,
}) => {
    await mockApi(page, { anonymous: true });
    await page.goto("/app/");
    await page.getByLabel("Email").fill("owner@example.test");
    expect(await asks(page)).toBe(false);
    await signIn(page);
    await page.goto("/app/");
    await openPlant(page, "Settings");
    expect(await asks(page)).toBe(false);
});

test("a changed plant settings form asks until it is saved", async ({
    page,
}) => {
    await signIn(page);
    await page.route("**/api/tenants/1/regions/11/plants/21/", (route) =>
        route.fulfill({
            json: { ...plant, name: "New name", parameters_revision: 2 },
        }),
    );
    await openPlant(page, "Settings");
    await page.getByLabel("Plant name").fill("New name");
    expect(await asks(page)).toBe(true);
    await page.getByRole("button", { name: "Save technical details" }).click();
    await expect(page.getByText("Saved.", { exact: true })).toBeVisible();
    expect(await asks(page)).toBe(false);
});

async function requestLocation(page: Page) {
    let pending = false;
    const item = {
        id: 1,
        base_revision: 1,
        latitude: 37,
        longitude: 31,
        timezone: "Europe/Istanbul",
        reason: "",
        status: "pending",
        created_at: "2026-01-01T00:00:00Z",
        reviewed_at: null,
        decision_reason: "",
    };
    await page.route("**/location-change-requests/", (route) => {
        const posted = route.request().method() === "POST";
        pending ||= posted;
        return route.fulfill({
            json: posted ? item : paged(pending ? [item] : []),
        });
    });
}

test("a changed location form asks until it is submitted", async ({ page }) => {
    await signIn(page);
    await requestLocation(page);
    await openPlant(page, "Settings");
    await page.getByRole("button", { name: "Location", exact: true }).click();
    await page.getByText("Change location", { exact: true }).click();
    await page.getByLabel("Latitude (°)").fill("37");
    expect(await asks(page)).toBe(true);
    await page.getByLabel("Longitude (°)").fill("31");
    await page
        .getByRole("button", { name: "Send location change to Pyracast" })
        .click();
    await expect(page.getByText("37°, 31° · Europe/Istanbul")).toBeVisible();
    expect(await asks(page)).toBe(false);
});

test("a typed rejection reason that a failed and a recovered read of the request bring back asks before a shell destination, and Stay keeps it", async ({
    page,
}) => {
    const { detail, refresh } = await reviewRequest(page, answers());
    const reason = detail.getByLabel("Reason for rejection");
    await reason.fill("The meter reading is wrong");
    let gone = true;
    await page.route(/change-requests\/1\/$/, (route) =>
        gone
            ? route.fulfill({
                  status: 404,
                  json: errorBody("not_found", "Not found."),
              })
            : route.fallback(),
    );
    await refresh.click();
    await expect(detail).toContainText("Not found.");
    await expect(reason).toHaveCount(0);
    gone = false;
    await refresh.click();
    await expect(reason).toHaveValue("The meter reading is wrong");
    expect(await asks(page)).toBe(true);
    await stay(page, () =>
        page.getByRole("button", { name: "Plants", exact: true }).click(),
    );
    await expect(reason).toHaveValue("The meter reading is wrong");
});

test("two registrations ask independently: saving the changed value keeps the chosen file asking", async ({
    page,
}) => {
    await page.clock.setFixedTime(now);
    await signIn(page);
    const server = await serveDirectProduction(page, answers());
    await openPlant(page, "Production");
    await editValue(page, "0,5");
    await openImport(page);
    await page
        .getByLabel("CSV file", { exact: true })
        .setInputFiles(meter("meter.csv"));
    await expect(page.getByText(/^meter\.csv · 1 row read/)).toBeVisible();
    await saveValue(page);
    await expect(page.getByText("Saved.", { exact: true })).toBeVisible();
    expect(server.applied()).toBe(1);
    expect(await asks(page)).toBe(true);
    await page.getByLabel("CSV file", { exact: true }).setInputFiles([]);
    expect(await asks(page)).toBe(false);
});

test("an outage form asks until it is closed, and a resolution time until it is saved", async ({
    page,
}) => {
    await page.clock.setFixedTime(now);
    await signIn(page);
    await serveOutageWrites(page, answers(), [activeOutage]);
    await openPlant(page, "Outages");
    await page
        .getByRole("button", { name: "Report outage", exact: true })
        .click();
    await page.getByLabel("Title").fill("Grid fault");
    expect(await asks(page)).toBe(true);
    await page.getByRole("button", { name: "Close" }).click();
    expect(await asks(page)).toBe(false);
    await page.getByRole("button", { name: "End outage", exact: true }).click();
    await page.getByLabel("End", { exact: true }).fill("2026-11-10T13:00");
    expect(await asks(page)).toBe(true);
    await page.getByRole("button", { name: "End outage" }).click();
    await expect(page.getByText("Ended", { exact: true })).toBeVisible();
    expect(await asks(page)).toBe(false);
});

test("a form whose submission is running asks until it is answered", async ({
    page,
}) => {
    await page.clock.setFixedTime(now);
    await signIn(page);
    await serveOutageWrites(page, answers(), [activeOutage]);
    let release = () => {};
    const released = new Promise<void>((resolve) => (release = resolve));
    await page.route("**/outages/51/resolve/", async (route) => {
        await released;
        await route.fallback();
    });
    await openPlant(page, "Outages");
    await page.getByRole("button", { name: "End outage", exact: true }).click();
    expect(await asks(page)).toBe(false);
    await page.getByRole("button", { name: "End outage" }).click();
    await expect(
        page.getByRole("button", { name: "Saving…", exact: true }),
    ).toBeVisible();
    expect(await asks(page)).toBe(true);
    release();
    await expect(page.getByText("Ended", { exact: true })).toBeVisible();
    expect(await asks(page)).toBe(false);
});

// Holds every read of `url` sent while armed until `release`; `held` settles once one is held, `pass` lets later reads through.
async function holdReads(page: Page, url: string) {
    let armed = false;
    let release = () => {};
    let entered = () => {};
    const released = new Promise<void>((resolve) => (release = resolve));
    const held = new Promise<void>((resolve) => (entered = resolve));
    await page.route(url, async (route) => {
        if (armed && route.request().method() === "GET") {
            entered();
            await released;
        }
        await route.fallback();
    });
    return {
        arm: () => {
            armed = true;
        },
        pass: () => {
            armed = false;
        },
        held,
        release,
    };
}

const holdWorkspaceReads = (page: Page) => holdReads(page, "**/api/tenants/");

async function chooseHalfHours(page: Page) {
    await page.getByLabel("Interval", { exact: true }).selectOption("30");
}

async function expectForecastInPage(page: Page) {
    await page.getByRole("button", { name: "Forecast", exact: true }).click();
    await expect(page.getByLabel("Interval", { exact: true })).toHaveValue(
        "30",
    );
    expect(await marked(page)).toBe(true);
}

test("a resolution whose save was answered while its follow-up reads run asks nothing, and another plant tab opens in the page", async ({
    page,
}) => {
    await page.clock.setFixedTime(now);
    await signIn(page);
    await serveOutageWrites(page, answers(), [activeOutage]);
    const reads = await holdWorkspaceReads(page);
    await openPlant(page);
    await chooseHalfHours(page);
    await page.getByRole("button", { name: "Outages", exact: true }).click();
    await page.getByRole("button", { name: "End outage", exact: true }).click();
    await page.getByLabel("End", { exact: true }).fill("2026-11-10T13:00");
    await markDocument(page);
    reads.arm();
    await page.getByRole("button", { name: "End outage", exact: true }).click();
    await reads.held;
    await expect(page.getByText("Ended", { exact: true })).toBeVisible();
    await expect(
        page.getByRole("button", { name: "Saving…", exact: true }),
    ).toBeVisible();
    expect(await asks(page)).toBe(false);
    await expectForecastInPage(page);
    reads.release();
});

test("plant settings whose save was answered while their follow-up reads run ask nothing, and another plant tab opens in the page", async ({
    page,
}) => {
    await page.clock.setFixedTime(now);
    await signIn(page);
    await page.route("**/api/tenants/1/regions/11/plants/21/", (route) =>
        route.fulfill({
            json: { ...plant, name: "New name", parameters_revision: 2 },
        }),
    );
    const reads = await holdWorkspaceReads(page);
    await openPlant(page);
    await chooseHalfHours(page);
    await page.getByRole("button", { name: "Settings", exact: true }).click();
    await page.getByLabel("Plant name").fill("New name");
    await markDocument(page);
    reads.arm();
    await page.getByRole("button", { name: "Save technical details" }).click();
    await reads.held;
    await expect(
        page.getByRole("heading", { name: "New name", level: 1 }),
    ).toBeVisible();
    await expect(
        page.getByRole("button", { name: "Saving…", exact: true }),
    ).toBeVisible();
    expect(await asks(page)).toBe(false);
    await expectForecastInPage(page);
    reads.release();
});

test("a resolution whose save is unanswered loads another plant tab as a document after the browser asks, and its lost answer keeps asking", async ({
    page,
}) => {
    await page.clock.setFixedTime(now);
    await signIn(page);
    await serveOutageWrites(page, answers(502), [activeOutage]);
    let release = () => {};
    const released = new Promise<void>((resolve) => (release = resolve));
    await page.route("**/outages/51/resolve/", async (route) => {
        await released;
        await route.fallback();
    });
    await openPlant(page, "Outages");
    await page.getByRole("button", { name: "End outage", exact: true }).click();
    await page.getByLabel("End", { exact: true }).fill("2026-11-10T13:00");
    await page.getByRole("button", { name: "End outage", exact: true }).click();
    await expect(
        page.getByRole("button", { name: "Saving…", exact: true }),
    ).toBeVisible();
    await stay(page, () =>
        page.getByRole("button", { name: "Forecast", exact: true }).click(),
    );
    release();
    await expect(page.getByText("Outcome unknown")).toBeVisible();
    expect(await asks(page)).toBe(true);
});

async function holdRequests(page: Page, url: string) {
    let release = () => {};
    const released = new Promise<void>((resolve) => (release = resolve));
    await page.route(url, async (route) => {
        await released;
        await route.fallback();
    });
    return release;
}

const saving = (page: Page) =>
    page.getByRole("button", { name: "Saving…", exact: true });

async function enterResolution(page: Page) {
    await page.getByRole("button", { name: "End outage", exact: true }).click();
    await page.getByLabel("End", { exact: true }).fill("2026-11-10T13:00");
}

const saveResolution = (page: Page) =>
    page.getByRole("button", { name: "End outage", exact: true }).click();

async function refuseOutageList(page: Page) {
    let refused = false;
    await page.route("**/plants/21/outages/", (route) =>
        refused && route.request().method() === "GET"
            ? route.fulfill({
                  status: 404,
                  json: errorBody("not_found", "Not found."),
              })
            : route.fallback(),
    );
    return (next: boolean) => {
        refused = next;
    };
}

const lateInvitation = {
    id: 3,
    email: "late@example.test",
    role: "viewer",
    state: "pending",
    created_at: "2026-11-09T09:00:00Z",
    expires_at: "2026-11-16T09:00:00Z",
    delivery_status: "sent",
} as const;

test("an answered invitation lets another view open in the page while its reads and another invitation sent again are unanswered, and that send asks before a full load until it is answered", async ({
    page,
}) => {
    await mockApi(page);
    await page.route("**/api/tenants/1/members/", (route) =>
        route.fulfill({
            json: apiAnswer("WorkspaceMembersOutput", {
                members: [
                    { id: 1, email: "owner@example.test", role: "admin" },
                ],
                plants_at_recipient_limit: 0,
            }),
        }),
    );
    await page.route("**/api/tenants/1/invitations/", (route) =>
        route.fulfill(
            route.request().method() === "POST"
                ? {
                      status: 201,
                      json: apiAnswer("InvitationOutput", {
                          ...lateInvitation,
                          id: 4,
                          email: "new@example.test",
                      }),
                  }
                : {
                      json: apiAnswer("WorkspaceInvitationsOutput", {
                          invitations: [lateInvitation],
                      }),
                  },
        ),
    );
    await page.route("**/api/tenants/1/invitations/3/resend/", (route) =>
        route.fulfill({
            status: 201,
            json: apiAnswer("InvitationOutput", lateInvitation),
        }),
    );
    const releaseResend = await holdRequests(
        page,
        "**/api/tenants/1/invitations/3/resend/",
    );
    const reads = await holdWorkspaceReads(page);
    await page.goto("/app/");
    await page.getByRole("button", { name: "Members", exact: true }).click();
    const resend = page.getByRole("button", {
        name: "Send again: late@example.test",
        exact: true,
    });
    await expect(resend).toBeVisible();
    await page
        .getByLabel("Email address", { exact: true })
        .fill("new@example.test");
    await markDocument(page);
    reads.arm();
    const followed = page.waitForResponse(
        (response) =>
            response.url().endsWith("/api/tenants/1/invitations/") &&
            response.request().method() === "GET",
    );
    await page
        .getByRole("button", { name: "Send invitation", exact: true })
        .click();
    await followed;
    await reads.held;
    await expect(saving(page)).toBeVisible();
    const resent = page.waitForRequest("**/invitations/3/resend/");
    await resend.click();
    await resent;
    await page.getByRole("button", { name: "Plants", exact: true }).click();
    await expect(page).toHaveURL("/app/w/1/");
    expect(await marked(page)).toBe(true);
    expect(await asks(page)).toBe(true);
    releaseResend();
    await expect.poll(() => asks(page)).toBe(false);
    reads.release();
});

test("a resolution repeated after its lost answer asks until the repeat is answered and no more while its reads run", async ({
    page,
}) => {
    await page.clock.setFixedTime(now);
    await signIn(page);
    await serveOutageWrites(page, answers("drop"), [activeOutage]);
    const reads = await holdWorkspaceReads(page);
    await openPlant(page, "Outages");
    await enterResolution(page);
    await saveResolution(page);
    await expect(page.getByText("Outcome unknown")).toBeVisible();
    expect(await asks(page)).toBe(true);
    const releaseRepeat = await holdRequests(page, "**/outages/51/resolve/");
    reads.arm();
    const repeated = page.waitForRequest("**/outages/51/resolve/");
    await saveResolution(page);
    await repeated;
    await expect(saving(page)).toBeVisible();
    expect(await asks(page)).toBe(true);
    await expect(page.getByText("Outcome unknown")).toBeVisible();
    const settled = await asksOnce(page, { gone: "Outcome unknown" });
    releaseRepeat();
    expect(await settled()).toBe(false);
    await reads.held;
    await expect(page.getByText("Ended", { exact: true })).toBeVisible();
    await expect(saving(page)).toBeVisible();
    expect(await asks(page)).toBe(false);
    await expect(saving(page)).toBeVisible();
    reads.release();
    await expect(saving(page)).toHaveCount(0);
});

test("an outage report whose answer was lost keeps asking while another report is saved, its reads run and its form closes", async ({
    page,
}) => {
    await page.clock.setFixedTime(now);
    await signIn(page);
    await serveOutageWrites(page, answers("drop"));
    const reads = await holdWorkspaceReads(page);
    await openPlant(page, "Outages");
    await page
        .getByRole("button", { name: "Report outage", exact: true })
        .click();
    const form = page.getByRole("region", { name: "Report outage" });
    const title = form.getByLabel("Title", { exact: true });
    await title.fill("Grid fault");
    const save = form.getByRole("button", { name: "Save", exact: true });
    await save.click();
    await expect(form.getByText("Outcome unknown")).toBeVisible();
    await title.fill("Inverter fault");
    reads.arm();
    await save.click();
    await reads.held;
    await expect(
        page.getByRole("heading", { name: "Inverter fault", exact: true }),
    ).toBeVisible();
    await expect(saving(page)).toBeVisible();
    expect(await asks(page)).toBe(true);
    reads.release();
    await expect(form).toHaveCount(0);
    await expect(page.getByText("Outcome unknown")).toBeVisible();
    expect(await asks(page)).toBe(true);
});

test("an outage edit repeated after its lost answer, whose conflict shows the edit applied, asks no more while its reads run", async ({
    page,
}) => {
    await page.clock.setFixedTime(now);
    await signIn(page);
    const server = await serveOutageWrites(page, answers(502), [activeOutage]);
    const reads = await holdWorkspaceReads(page);
    await openPlant(page, "Outages");
    await page
        .getByRole("button", { name: "Edit outage", exact: true })
        .click();
    await page.getByLabel("Title").fill("Inverter replaced");
    const save = page.getByRole("button", { name: "Save", exact: true });
    await save.click();
    await expect(page.getByText("Outcome unknown")).toBeVisible();
    expect(await asks(page)).toBe(true);
    reads.arm();
    const settled = await asksOnce(page, { gone: "Outcome unknown" });
    await save.click();
    expect(await settled()).toBe(false);
    await reads.held;
    await expect(saving(page)).toBeVisible();
    expect(await asks(page)).toBe(false);
    await expect(saving(page)).toBeVisible();
    reads.release();
    await expect(
        page.getByRole("heading", { name: "Inverter replaced", exact: true }),
    ).toBeVisible();
    expect(server.applied()).toBe(1);
});

test("a resolution form shown again while the reads after its answered save run lets another plant tab open in the page", async ({
    page,
}) => {
    await page.clock.setFixedTime(now);
    await signIn(page);
    await serveOutageWrites(page, answers(), [activeOutage]);
    const refuse = await refuseOutageList(page);
    const reads = await holdWorkspaceReads(page);
    await openPlant(page);
    await chooseHalfHours(page);
    await page.getByRole("button", { name: "Outages", exact: true }).click();
    await enterResolution(page);
    await markDocument(page);
    reads.arm();
    refuse(true);
    await saveResolution(page);
    await expect(page.getByText("Not found.")).toBeVisible();
    refuse(false);
    await page.getByRole("button", { name: "Try again", exact: true }).click();
    await reads.held;
    await expect(page.getByText("Ended", { exact: true })).toBeVisible();
    await expect(saving(page)).toBeVisible();
    await expectForecastInPage(page);
    reads.release();
});

test("a resolution form shown again while its save is unanswered asks before another plant tab", async ({
    page,
}) => {
    await page.clock.install({ time: now });
    await signIn(page);
    await serveOutageWrites(page, answers(), [activeOutage]);
    const refuse = await refuseOutageList(page);
    const release = await holdRequests(page, "**/outages/51/resolve/");
    await openPlant(page, "Outages");
    await enterResolution(page);
    await saveResolution(page);
    await expect(saving(page)).toBeVisible();
    refuse(true);
    await page.clock.runFor(5_000);
    await focusWindow(page);
    await expect(page.getByText("Not found.")).toBeVisible();
    refuse(false);
    await page.getByRole("button", { name: "Try again", exact: true }).click();
    await expect(saving(page)).toBeVisible();
    await stay(page, () =>
        page.getByRole("button", { name: "Forecast", exact: true }).click(),
    );
    release();
    await expect(page.getByText("Ended", { exact: true })).toBeVisible();
});

test("a reload while the reads after an answered resolution run asks nothing and loads the plant again", async ({
    page,
}) => {
    await page.clock.setFixedTime(now);
    await signIn(page);
    await serveOutageWrites(page, answers(), [activeOutage]);
    const reads = await holdWorkspaceReads(page);
    await openPlant(page, "Outages");
    await enterResolution(page);
    await markDocument(page);
    reads.arm();
    await saveResolution(page);
    await reads.held;
    await expect(page.getByText("Ended", { exact: true })).toBeVisible();
    await expect(saving(page)).toBeVisible();
    const asked = questions(page);
    reads.pass();
    await page.reload();
    await expect(
        page.getByRole("button", { name: "Report outage", exact: true }),
    ).toBeVisible();
    expect(await marked(page)).toBe(false);
    expect(asked).toEqual([]);
});

test("a resolution whose save is unanswered loads another plant tab as a new document when Leave is chosen", async ({
    page,
}) => {
    await page.clock.setFixedTime(now);
    await signIn(page);
    await serveOutageWrites(page, answers(), [activeOutage]);
    await holdRequests(page, "**/outages/51/resolve/");
    await openPlant(page, "Outages");
    await enterResolution(page);
    await markDocument(page);
    await saveResolution(page);
    await expect(saving(page)).toBeVisible();
    await answer(page, true, () =>
        page.getByRole("button", { name: "Forecast", exact: true }).click(),
    );
    await expectReloaded(page);
    await expect(page).toHaveURL(/\/forecast\//);
    await expect(page.getByLabel("Interval", { exact: true })).toBeVisible();
});

test("a password change the server confirmed asks nothing while the session is read again", async ({
    page,
}) => {
    await mockApi(page);
    await page.route(
        "**/api/auth/browser/v1/account/password/change",
        (route) =>
            route.fulfill({
                status: 401,
                json: {
                    status: 401,
                    meta: { is_authenticated: false, password_changed: true },
                },
            }),
    );
    const session = await holdReads(page, "**/api/session/");
    await page.goto("/app/account/");
    const password = page.locator(".password-disclosure");
    await password.locator("summary").click();
    await password
        .getByLabel("Current password", { exact: true })
        .fill("current-password");
    await password
        .getByLabel("New password", { exact: true })
        .fill("a-new-and-long-password");
    session.arm();
    await password.getByRole("button", { name: "Change password" }).click();
    await session.held;
    expect(await asks(page)).toBe(false);
    session.release();
});

const schedule = {
    enabled: true,
    times: ["09:00", "12:00"],
    timezone: "Europe/Istanbul",
    last_success_at: null,
    next_collection_at: "2026-11-11T06:00:00Z",
    state: "collecting",
    inactive_reason: null,
    requests_today: 0,
    history_requests_reserved_per_day: 1,
    history_local_time: "03:00",
    history_retry_until_local: "09:00",
    history_retry_minutes: 30,
    max_forecast_times: 9,
    effective_from: "2026-11-01T00:00:00Z",
    recent_jobs: [],
};

test("a removed collection time asks until the schedule is saved, without typing first", async ({
    page,
}) => {
    await page.clock.setFixedTime(now);
    await signIn(page);
    let saved = schedule;
    await page.route("**/collection-schedule/", (route) => {
        if (route.request().method() === "PUT")
            saved = { ...saved, ...route.request().postDataJSON() };
        return route.fulfill({ json: saved });
    });
    await openPlant(page, "Settings");
    await page
        .getByRole("button", { name: "Forecast update times", exact: true })
        .click();
    expect(await asks(page)).toBe(false);
    await page.getByRole("button", { name: "Remove update time 2" }).click();
    expect(await asks(page)).toBe(true);
    const shown = await asksOnce(page, { shown: "Saved." });
    await page.getByRole("button", { name: "Save", exact: true }).click();
    expect(await shown()).toBe(false);
    await expect(
        page.getByRole("button", { name: "Remove update time 2" }),
    ).toHaveCount(0);
    expect(saved.times).toEqual(["09:00"]);
    expect(await asks(page)).toBe(false);
});

test("swapped collection times save nothing and ask no more once saved", async ({
    page,
}) => {
    await page.clock.setFixedTime(now);
    await signIn(page);
    let puts = 0;
    await page.route("**/collection-schedule/", (route) => {
        if (route.request().method() === "PUT") puts++;
        return route.fulfill({ json: schedule });
    });
    await openPlant(page, "Settings");
    await page
        .getByRole("button", { name: "Forecast update times", exact: true })
        .click();
    await page.getByLabel("Update time 1", { exact: true }).fill("12:00");
    await page.getByLabel("Update time 2", { exact: true }).fill("09:00");
    expect(await asks(page)).toBe(true);
    await page.getByRole("button", { name: "Save", exact: true }).click();
    await expect(page.getByText("Saved.", { exact: true })).toBeVisible();
    expect(await asks(page)).toBe(false);
    expect(puts).toBe(0);
});

test("a saved schedule whose next read is held asks nothing, and saving it again sends nothing", async ({
    page,
}) => {
    await page.clock.setFixedTime(now);
    await signIn(page);
    let saved = schedule;
    let puts = 0;
    let holding = false;
    let release = () => {};
    const released = new Promise<void>((resolve) => (release = resolve));
    await page.route("**/collection-schedule/", async (route) => {
        if (route.request().method() === "PUT") {
            puts++;
            saved = { ...saved, ...route.request().postDataJSON() };
        } else if (holding) await released;
        await route.fulfill({ json: saved });
    });
    await openPlant(page, "Settings");
    await page
        .getByRole("button", { name: "Forecast update times", exact: true })
        .click();
    await page.getByRole("button", { name: "Remove update time 2" }).click();
    holding = true;
    const save = page.getByRole("button", {
        name: "Save",
        exact: true,
    });
    await save.click();
    const confirmed = page.getByText("Saved.", { exact: true });
    await expect(confirmed).toBeVisible();
    expect(await asks(page)).toBe(false);
    const time = page.getByLabel("Update time 1", { exact: true });
    await time.fill("10:00");
    await expect(confirmed).toHaveCount(0);
    await time.fill("09:00");
    await save.click();
    await expect(confirmed).toBeVisible();
    expect(puts).toBe(1);
    expect(await asks(page)).toBe(false);
    release();
});

test("a first time added to an empty saved schedule asks", async ({ page }) => {
    await page.clock.setFixedTime(now);
    await signIn(page);
    await page.route("**/collection-schedule/", (route) =>
        route.fulfill({ json: { ...schedule, enabled: false, times: [] } }),
    );
    await openPlant(page, "Settings");
    await page
        .getByRole("button", { name: "Forecast update times", exact: true })
        .click();
    expect(await asks(page)).toBe(false);
    await page.getByRole("button", { name: "Add time", exact: true }).click();
    await expect(
        page.getByLabel("Update time 1", { exact: true }),
    ).toBeVisible();
    expect(await asks(page)).toBe(true);
});

async function saveReminders(page: Page) {
    const saved = page.getByText("Saved.", { exact: true });
    await expect(saved).toHaveCount(0);
    await page.getByRole("button", { name: "Save", exact: true }).click();
    await expect(saved).toBeVisible();
}

for (const removed of ["Remove email address 2", "Remove send time 1"])
    test(`a reminder edit made by "${removed}" asks until the reminders are saved`, async ({
        page,
    }) => {
        await reminderApi(page, {
            recipients: ["one@example.test", "two@example.test"],
        });
        await openReminders(page);
        await page
            .getByRole("button", { name: "Add time", exact: true })
            .click();
        await page.getByLabel("Send time 2", { exact: true }).fill("17:30");
        await saveReminders(page);
        expect(await asks(page)).toBe(false);
        const remove = page.getByRole("button", { name: removed, exact: true });
        await expect(remove).toBeVisible();
        await remove.click();
        await expect(remove).toHaveCount(0);
        expect(await asks(page)).toBe(true);
        await saveReminders(page);
        expect(await asks(page)).toBe(false);
    });

test("the plant wizard asks while it holds entered figures across its steps until it is closed", async ({
    page,
}) => {
    await mockApi(page);
    await page.goto("/app/");
    await page.getByRole("button", { name: "Add plant", exact: true }).click();
    expect(await asks(page)).toBe(false);
    await page.getByLabel("Plant name", { exact: true }).fill("West plant");
    await page.getByLabel("Latitude (°)").fill("37");
    await page.getByLabel("Longitude (°)").fill("31");
    expect(await asks(page)).toBe(true);
    await page.getByRole("button", { name: "Continue", exact: true }).click();
    await expect(page.getByLabel("Plant name", { exact: true })).toBeHidden();
    expect(await asks(page)).toBe(true);
    await page.getByRole("button", { name: "Cancel", exact: true }).click();
    expect(await asks(page)).toBe(false);
});

const holdSites = (page: Page) => holdReads(page, "**/api/tenants/1/sites/");

test("a plant the wizard created opens in the page without a question", async ({
    page,
}) => {
    await mockApi(page, { empty: true });
    const sites = await holdSites(page);
    await page.goto("/app/");
    await page.getByRole("button", { name: "Add plant", exact: true }).click();
    await page.getByLabel("Plant name", { exact: true }).fill("West plant");
    await page.getByLabel("Latitude (°)").fill("37");
    await page.getByLabel("Longitude (°)").fill("31");
    await page.getByRole("button", { name: "Continue", exact: true }).click();
    await page.getByLabel("Panel capacity (MWp)").fill("5");
    await page.getByRole("button", { name: "Continue", exact: true }).click();
    await markDocument(page);
    const asked = questions(page);
    sites.arm();
    const created = page.waitForResponse(
        (response) =>
            response.url().endsWith("/api/tenants/1/sites/") &&
            response.request().method() === "POST",
    );
    await page.getByRole("button", { name: "Send for approval" }).click();
    await created;
    await sites.held;
    expect(await asks(page)).toBe(false);
    sites.release();
    await expect(
        page.getByRole("heading", { name: "West plant" }),
    ).toBeVisible();
    expect(await marked(page)).toBe(true);
    expect(asked).toEqual([]);
});

test("a plant the wizard restored opens in the page without a question", async ({
    page,
}) => {
    await mockApi(page);
    let deleted = true;
    await page.route("**/api/tenants/1/**", async (route) => {
        const path = new URL(route.request().url()).pathname;
        if (path === "/api/tenants/1/regions/11/restore/") {
            deleted = false;
            return route.fulfill({ json: region });
        }
        if (path === "/api/tenants/1/trash/regions/")
            return route.fulfill({
                json: paged(
                    deleted
                        ? [
                              {
                                  id: region.id,
                                  name: region.name,
                                  plant_name: plant.name,
                                  latitude: region.latitude,
                                  longitude: region.longitude,
                                  deleted_at: "2026-11-10T12:00:00Z",
                                  purge_after: "2026-12-10T12:00:00Z",
                                  restore_blocker: null,
                              },
                          ]
                        : [],
                ),
            });
        if (path === "/api/tenants/1/sites/")
            return route.fulfill({
                json: paged(deleted ? [] : [{ ...region, plant }]),
            });
        return route.fallback();
    });
    const sites = await holdSites(page);
    await page.goto("/app/");
    await page.getByRole("button", { name: "Add plant", exact: true }).click();
    await page.getByLabel("Plant name", { exact: true }).fill("Again");
    await page.getByLabel("Latitude (°)").fill(String(region.latitude));
    await page.getByLabel("Longitude (°)").fill(String(region.longitude));
    await page.getByRole("button", { name: "Continue", exact: true }).click();
    await markDocument(page);
    const asked = questions(page);
    sites.arm();
    const restored = page.waitForResponse((response) =>
        response.url().endsWith("/regions/11/restore/"),
    );
    await page.getByRole("button", { name: `Restore ${plant.name}` }).click();
    await restored;
    await sites.held;
    expect(await asks(page)).toBe(false);
    sites.release();
    await expect(page).toHaveURL("/app/w/1/plants/11/forecast/");
    expect(await marked(page)).toBe(true);
    expect(asked).toEqual([]);
});

// Holds every file read of the page until the test lets the next one through.
async function holdFileReads(page: Page) {
    await page.evaluate(() => {
        const waiting: (() => void)[] = [];
        Object.assign(window, {
            pyracastReleaseRead: () => waiting.shift()?.(),
        });
        Blob.prototype.text = function (this: Blob) {
            return new Promise<void>((resolve) => waiting.push(resolve)).then(
                () => new Response(this).text(),
            );
        };
    });
    return () =>
        page.evaluate(() =>
            (
                window as unknown as { pyracastReleaseRead: () => void }
            ).pyracastReleaseRead(),
        );
}

test("a file being read asks from its choice, also when it replaces a prepared file, until it is cleared", async ({
    page,
}) => {
    await page.clock.setFixedTime(now);
    await signIn(page);
    await serveProduction(page, [{ can_edit: true }]);
    await openPlant(page, "Production");
    await openImport(page);
    const releaseRead = await holdFileReads(page);
    const input = page.getByLabel("CSV file", { exact: true });
    await input.setInputFiles(meter("first.csv"));
    expect(await asks(page)).toBe(true);
    await releaseRead();
    await expect(page.getByText(/^first\.csv · 1 row read/)).toBeVisible();
    expect(await asks(page)).toBe(true);
    await input.setInputFiles(meter("second.csv"));
    await expect(page.getByText(/^first\.csv · 1 row read/)).toHaveCount(0);
    expect(await asks(page)).toBe(true);
    await releaseRead();
    await expect(page.getByText(/^second\.csv · 1 row read/)).toBeVisible();
    await input.setInputFiles([]);
    expect(await asks(page)).toBe(false);
});

test("a prepared import asks until it is confirmed", async ({ page }) => {
    await page.clock.setFixedTime(now);
    await signIn(page);
    await serveProduction(page, [{ can_edit: true }]);
    await serveImport(page, answers(), {});
    await openPlant(page, "Production");
    await openImport(page);
    await page
        .getByLabel("CSV file", { exact: true })
        .setInputFiles(meter("meter.csv"));
    await page.getByRole("button", { name: "Preview", exact: true }).click();
    const confirm = page.getByRole("button", {
        name: "Import 1 value",
        exact: true,
    });
    await expect(confirm).toBeVisible();
    expect(await asks(page)).toBe(true);
    await confirm.click();
    await expect(page.getByText(/^Imported\./)).toBeVisible();
    expect(await asks(page)).toBe(false);
});

test("a changed production value or quality asks until it is saved or cancelled", async ({
    page,
}) => {
    await page.clock.setFixedTime(now);
    await signIn(page);
    const server = await serveDirectProduction(page, answers());
    await openPlant(page, "Production");
    await page.getByRole("button", { name: /^Edit 08:00/ }).click();
    expect(await asks(page)).toBe(false);
    await editorValue(page).fill("0,5");
    expect(await asks(page)).toBe(true);
    await editorValue(page).fill("1");
    expect(await asks(page)).toBe(false);
    await page.getByRole("button", { name: "Cancel", exact: true }).click();
    await page.getByRole("button", { name: /^Edit 08:00/ }).click();
    await page
        .locator(".production-row-editor")
        .getByLabel("Status")
        .selectOption("outage");
    expect(await asks(page)).toBe(true);
    await page.getByRole("button", { name: "Cancel", exact: true }).click();
    expect(await asks(page)).toBe(false);
    await editValue(page, "0,5");
    await saveValue(page);
    await expect(page.getByText("Saved.", { exact: true })).toBeVisible();
    expect(await asks(page)).toBe(false);
    expect(server.applied()).toBe(1);
});

test("an unchanged Remove entered value asks while it runs and while its answer is unconfirmed, and no more once applied", async ({
    page,
}) => {
    await page.clock.setFixedTime(now);
    await signIn(page);
    const server = await serveDirectProduction(page, answers("abort"));
    server.changeByOperator(0.9);
    let release = () => {};
    const released = new Promise<void>((resolve) => (release = resolve));
    let holding = true;
    await page.route("**/production/changes/", async (route) => {
        if (holding) await released;
        await route.fallback();
    });
    await openPlant(page, "Production");
    await page.getByRole("button", { name: /^Edit 08:00/ }).click();
    const editor = page.locator(".production-row-editor");
    const revert = editor.getByRole("button", {
        name: /^Remove entered value for 08:00/,
    });
    expect(await asks(page)).toBe(false);
    await revert.click();
    await expect(
        editor.getByRole("button", { name: "Saving…", exact: true }),
    ).toBeVisible();
    expect(await asks(page)).toBe(true);
    holding = false;
    release();
    await expect(editor.getByRole("alert")).toContainText("Outcome unknown");
    expect(await asks(page)).toBe(true);
    await revert.click();
    await expect(editor).toHaveCount(0);
    expect(await asks(page)).toBe(false);
    expect(server.applied()).toBe(1);
});

test("a staged proposal asks until it is discarded", async ({ page }) => {
    await page.clock.setFixedTime(now);
    await signIn(page, { role: "viewer" });
    await serveProduction(page, [{ can_edit: false, can_propose: true }]);
    await openPlant(page, "Production");
    await page.getByRole("button", { name: "Edit 08:00" }).click();
    const editor = page.locator(".production-row-editor");
    await editor.getByRole("textbox").first().fill("0,5");
    await editor
        .getByRole("button", { name: "Add to change request", exact: true })
        .click();
    expect(await asks(page)).toBe(true);
    await page
        .getByRole("button", { name: "Discard change request", exact: true })
        .click();
    expect(await asks(page)).toBe(false);
});

// Holds the production reads once the test arms it, until it lets them through.
async function holdProductionReads(page: Page) {
    let held: Promise<void> | null = null;
    let release = () => {};
    await page.route("**/api/**/production/?*", async (route) => {
        if (held) await held;
        await route.fallback();
    });
    return {
        arm() {
            held = new Promise<void>((resolve) => (release = resolve));
        },
        release() {
            held = null;
            release();
        },
    };
}

test("a refresh that changes the row under an unchanged editor, and the editor it orphans, ask nothing", async ({
    page,
}) => {
    await page.clock.install({ time: now });
    await signIn(page);
    let editable = true;
    const server = await serveDirectProduction(page, answers());
    await page.route("**/api/**/production/?*", async (route) => {
        const date = new URL(route.request().url()).searchParams.get(
            "start_date",
        )!;
        const [row, ...rest] = server.day(date).intervals;
        await route.fulfill({
            json: {
                ...server.day(date),
                intervals: [{ ...row!, can_edit: editable }, ...rest],
            },
        });
    });
    const reads = await holdProductionReads(page);
    await openPlant(page, "Production");
    await page.getByRole("button", { name: /^Edit 08:00/ }).click();
    await expect(editorValue(page)).toHaveValue("1");
    server.changeByOperator(0.7);
    reads.arm();
    await page.clock.runFor(5_000);
    await focusWindow(page);
    expect(await asks(page)).toBe(false);
    reads.release();
    await expect(
        page
            .locator(".production-table tbody tr")
            .first()
            .locator(".production-value"),
    ).toContainText("0.7");
    await expect(editorValue(page)).toHaveValue("1");
    expect(await asks(page)).toBe(false);
    editable = false;
    await page.clock.runFor(5_000);
    await focusWindow(page);
    await expect(
        page.getByText(/^This interval changed and can no longer be edited/),
    ).toBeVisible();
    expect(await asks(page)).toBe(false);
});

const targets = {
    Comparison: (page: Page) =>
        page.getByRole("button", { name: "Comparison", exact: true }),
    "a plant tab": (page: Page) =>
        page.getByRole("button", { name: "Outages", exact: true }),
    "a shell entry": (page: Page) =>
        page.getByRole("button", { name: "Plants", exact: true }),
    "a link within the application": (page: Page) =>
        page.getByRole("link", { name: /^Waiting for approval/ }),
};

for (const [name, target] of Object.entries(targets))
    test(`with a changed value ${name} loads a document after the browser asks, and after Stay the value saves and the next click loads a document without a question`, async ({
        page,
    }) => {
        await page.clock.setFixedTime(now);
        await signIn(page);
        await serveOutageWrites(page, answers());
        const server = await serveDirectProduction(page, answers(), [
            { pending_request_id: 7 },
        ]);
        await page.route("**/change-requests/7/", (route) =>
            route.fulfill({
                status: 404,
                json: errorBody("not_found", "Not found."),
            }),
        );
        await openPlant(page, "Production");
        await editValue(page, "0,5");
        const url = page.url();
        await markDocument(page);
        const sent = routerRequests(page);
        await stay(page, () => target(page).click());
        await expect(actualsOf(page)).not.toHaveAttribute("inert");
        await expect(actualsOf(page)).toBeEnabled();
        await expect(editorValue(page)).toHaveValue("0,5");
        await saveValue(page);
        await expect(page.getByText("Saved.", { exact: true })).toBeVisible();
        expect(server.energy()).toBe(0.5);
        expect(sent).toEqual([]);
        expect(page.url()).toBe(url);
        expect(await marked(page)).toBe(true);
        const asked = questions(page);
        await target(page).click();
        await expectReloaded(page);
        await expect(page).not.toHaveURL(url);
        expect(asked).toEqual([]);
        expect(sent).toEqual([]);
    });

test("with a changed value Forward and Back each load a document after the browser asks, and after Stay the value saves", async ({
    page,
}) => {
    await page.clock.setFixedTime(now);
    await signIn(page);
    await serveOutageWrites(page, answers());
    const server = await serveDirectProduction(page, answers());
    await openPlant(page, "Production");
    await expect(page).toHaveURL(/\/production\//);
    await page.getByRole("button", { name: "Outages", exact: true }).click();
    await expect(page).toHaveURL(/\/outages\//);
    await page.goBack();
    await expect(page).toHaveURL(/\/production\//);
    await editValue(page, "0,5");
    await markDocument(page);
    const sent = routerRequests(page);
    await stay(page, () => traverse(page, "forward"));
    await expect(editorValue(page)).toHaveValue("0,5");
    await stay(page, () => traverse(page, "back"));
    await expect(editorValue(page)).toHaveValue("0,5");
    await expect(actualsOf(page)).not.toHaveAttribute("inert");
    await saveValue(page);
    await expect(page.getByText("Saved.", { exact: true })).toBeVisible();
    expect(server.energy()).toBe(0.5);
    expect(sent).toEqual([]);
    expect(await marked(page)).toBe(true);
});

// Only scripts: a held stylesheet would hold the inline payload scripts and with them the parsed document.
const script = (url: URL) =>
    url.pathname.startsWith("/_next/static/chunks/") &&
    url.pathname.endsWith(".js");

// Holds the application's scripts; the returned step answers the held ones last first.
async function holdScripts(page: Page) {
    const held: (() => void)[] = [];
    let released = false;
    await page.route(script, async (route) => {
        if (!released) await new Promise<void>((resolve) => held.push(resolve));
        await route.continue();
    });
    return () => {
        released = true;
        for (const answer of held.reverse()) answer();
    };
}

async function openDeepEntry(page: Page, held: boolean) {
    const release = held ? await holdScripts(page) : null;
    await page.goto("/app/w/1/plants/11/outages/", {
        waitUntil: "domcontentloaded",
    });
    release?.();
    const report = page.getByRole("button", {
        name: "Report outage",
        exact: true,
    });
    await expect(report).toBeVisible();
    return report;
}

// The router's listener reads a traversal's state and the guard's does not, so the reads count what the router took up.
async function routerTraversals(page: Page) {
    await page.addInitScript(() => {
        const state = Object.getOwnPropertyDescriptor(
            PopStateEvent.prototype,
            "state",
        );
        const received = new Set<Event>();
        Object.defineProperty(PopStateEvent.prototype, "state", {
            ...state,
            get(this: PopStateEvent) {
                received.add(this);
                return state?.get?.call(this) as unknown;
            },
        });
        Object.defineProperty(window, "routerTraversals", {
            get: () => received.size,
        });
    });
    return () =>
        page.evaluate(() => Reflect.get(window, "routerTraversals") as number);
}

test("on a document loaded at a deep address with its scripts answered last first, the router takes up Back and Forward until a value changes and then neither", async ({
    page,
}) => {
    await page.clock.setFixedTime(now);
    await signIn(page);
    await serveOutageWrites(page, answers());
    await serveDirectProduction(page, answers());
    const taken = await routerTraversals(page);
    const report = await openDeepEntry(page, true);
    const production = page.getByRole("button", { name: /^Edit 08:00/ });
    await page.getByRole("button", { name: "Production", exact: true }).click();
    await expect(production).toBeVisible();
    await traverse(page, "back");
    await expect(report).toBeVisible();
    await traverse(page, "forward");
    await expect(production).toBeVisible();
    expect(await taken()).toBe(2);
    await editValue(page, "0,5");
    await markDocument(page);
    await stay(page, () => traverse(page, "back"));
    await stay(page, () => traverse(page, "forward"));
    await expect(editorValue(page)).toHaveValue("0,5");
    expect(await taken()).toBe(2);
    expect(await marked(page)).toBe(true);
});

for (const scripts of ["answered at once", "answered last first"])
    for (const leave of [false, true])
        test(`on a document loaded at a deep address with its scripts ${scripts}, Back with a changed value asks, and ${leave ? "Leave loads the entry traversed to as a new document" : "Stay keeps the value at that address"}`, async ({
            page,
        }) => {
            await page.clock.setFixedTime(now);
            await signIn(page);
            await serveOutageWrites(page, answers());
            await serveDirectProduction(page, answers());
            const report = await openDeepEntry(
                page,
                scripts === "answered last first",
            );
            const deep = page.url();
            await page
                .getByRole("button", { name: "Production", exact: true })
                .click();
            await expect(page).toHaveURL(/\/production\//);
            await editValue(page, "0,5");
            await markDocument(page);
            const sent = routerRequests(page);
            await answer(page, leave, () => page.goBack());
            if (leave) {
                await expectReloaded(page);
                await expect(page).toHaveURL(deep);
                await expect(report).toBeVisible();
            } else {
                await expect(editorValue(page)).toHaveValue("0,5");
                expect(page.url()).toBe(deep);
                expect(await marked(page)).toBe(true);
            }
            expect(sent).toEqual([]);
        });

const unavailable = {
    status: 503,
    contentType: "text/html",
    body: "<p>Unavailable</p>",
};

test("a file chosen while a clean day change is on its way asks when that change falls back to a document load, and after Stay the import applies and the next navigation loads a document", async ({
    page,
}) => {
    await page.clock.setFixedTime(now);
    await signIn(page);
    await serveOutageWrites(page, answers());
    await serveProduction(page, [{ can_edit: true }]);
    const server = await serveImport(page, answers(), {});
    let observed = () => {};
    const routerAsked = new Promise<void>((resolve) => (observed = resolve));
    let answerRouter = () => {};
    const routerAnswered = new Promise<void>(
        (resolve) => (answerRouter = resolve),
    );
    const documents: string[] = [];
    await page.route(
        (url) => url.searchParams.get("date") === "2026-11-08",
        async (route) => {
            if (!route.request().headers()["rsc"]) {
                documents.push(route.request().url());
                return route.fallback();
            }
            observed();
            await routerAnswered;
            return route.fulfill(unavailable);
        },
    );
    await openPlant(page, "Production");
    await openImport(page);
    await markDocument(page);
    await page.getByRole("button", { name: "Previous day" }).click();
    await routerAsked;
    await page
        .getByLabel("CSV file", { exact: true })
        .setInputFiles(meter("meter.csv"));
    await expect(page.getByText(/^meter\.csv · 1 row read/)).toBeVisible();
    await stay(page, () => Promise.resolve(answerRouter()));
    expect(documents).toEqual([]);
    await page.getByRole("button", { name: "Preview", exact: true }).click();
    await page
        .getByRole("button", { name: "Import 1 value", exact: true })
        .click();
    await expect(page.getByText(/^Imported\./)).toBeVisible();
    expect(server.applied()).toBe(1);
    expect(await marked(page)).toBe(true);
    const asked = questions(page);
    await page.getByRole("button", { name: "Outages", exact: true }).click();
    await expectReloaded(page);
    await expect(page).toHaveURL(/\/outages\//);
    expect(asked).toEqual([]);
});

test("a clean switch to the comparison answered by a server failure loads its document without a question", async ({
    page,
}) => {
    await page.clock.setFixedTime(now);
    await signIn(page);
    await serveProduction(page, [{ can_edit: true }]);
    await page.route(
        (url) => url.searchParams.get("view") === "comparison",
        (route) =>
            route.request().headers()["rsc"]
                ? route.fulfill(unavailable)
                : route.fallback(),
    );
    await openPlant(page, "Production");
    await markDocument(page);
    const asked = questions(page);
    await page.getByRole("button", { name: "Comparison", exact: true }).click();
    await expectReloaded(page);
    await expect(page).toHaveURL(/view=comparison/);
    expect(asked).toEqual([]);
});

// The workspace's plant; the returned step gives it a new configuration and waits for the read that shows it.
async function serveConfiguration(page: Page) {
    let configuration = plant.current_configuration;
    await page.route("**/api/tenants/1/sites/", (route) =>
        route.request().method() === "GET"
            ? route.fulfill({
                  json: paged([
                      {
                          ...region,
                          plant: {
                              ...plant,
                              current_configuration: configuration,
                          },
                          reminder: null,
                      },
                  ]),
              })
            : route.fallback(),
    );
    return async () => {
        configuration = { ...configuration, id: 32, revision: 2 };
        const reread = page.waitForResponse(
            (response) =>
                response.url().endsWith("/api/tenants/1/sites/") &&
                response.request().method() === "GET",
        );
        await page.clock.runFor(5_000);
        await focusWindow(page);
        await reread;
    };
}

const discardedNotice =
    "The plant details changed; your unsent entries were discarded.";

async function stageEight(page: Page) {
    await page.getByRole("button", { name: "Edit 08:00" }).click();
    const editor = page.locator(".production-row-editor");
    await editor.getByRole("textbox").first().fill("0,5");
    await editor
        .getByRole("button", { name: "Add to change request", exact: true })
        .click();
}

for (const staged of [true, false])
    test(`a configuration change ${staged ? "with a staged proposal says its unsent entries were discarded" : "with nothing unsent says nothing"}`, async ({
        page,
    }) => {
        await page.clock.install({ time: now });
        await signIn(page, { role: "viewer" });
        const changeConfiguration = await serveConfiguration(page);
        await serveProduction(page, [{ can_edit: false, can_propose: true }]);
        await openPlant(page, "Production");
        const suggest = page.getByRole("button", {
            name: "Edit 08:00",
        });
        if (staged) await stageEight(page);
        await changeConfiguration();
        await expect(
            page.getByRole("region", { name: "Change request" }),
        ).toHaveCount(0);
        await expect(suggest).toBeEnabled();
        await expect(page.getByText(discardedNotice)).toHaveCount(
            staged ? 1 : 0,
        );
        expect(await asks(page)).toBe(false);
        await page.getByRole("button", { name: "Previous day" }).click();
        await expect(page).toHaveURL(/date=/);
        await expect(page.getByText(discardedNotice)).toHaveCount(0);
    });

test("the notice of entries a configuration change discarded ends when plant midnight shows the next day", async ({
    page,
}) => {
    await page.clock.install({ time: new Date(now.getTime() + 539 * 60_000) });
    await signIn(page, { role: "viewer" });
    const changeConfiguration = await serveConfiguration(page);
    await serveProduction(page, [{ can_edit: false, can_propose: true }]);
    await openPlant(page, "Production");
    const day = page.getByLabel("Day", { exact: true });
    await expect(day).toHaveValue("2026-11-09");
    await stageEight(page);
    await changeConfiguration();
    await expect(page.getByText(discardedNotice)).toHaveCount(1);
    await page.clock.runFor(2 * 60_000);
    await focusWindow(page);
    await expect(day).toHaveValue("2026-11-10");
    await expect(page.getByText(discardedNotice)).toHaveCount(0);
});

for (const typed of [false, true])
    test(`a configuration change after a Remove entered value whose answer was lost ${typed ? "says a value typed after it was discarded" : "and nothing typed after it says nothing was discarded"}`, async ({
        page,
    }) => {
        await page.clock.install({ time: now });
        await signIn(page);
        const changeConfiguration = await serveConfiguration(page);
        const server = await serveDirectProduction(page, answers("abort"));
        server.changeByOperator(0.9);
        await openPlant(page, "Production");
        await page.getByRole("button", { name: /^Edit 08:00/ }).click();
        const editor = page.locator(".production-row-editor");
        await editor
            .getByRole("button", { name: /^Remove entered value for 08:00/ })
            .click();
        await expect(editor.getByRole("alert")).toContainText(
            "Outcome unknown",
        );
        if (typed) await editorValue(page).fill("0,6");
        await changeConfiguration();
        await expect(editor).toHaveCount(0);
        await expect(page.getByText(discardedNotice)).toHaveCount(
            typed ? 1 : 0,
        );
        expect(server.applied()).toBe(1);
    });
