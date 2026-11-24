import { expect, instant, test, type Page } from "./support/test";
import { accessibilityViolations } from "./support/accessibility";
import { errorBody } from "./support/errors";
import { mockApi, paged, plant, region, tenantRow } from "./support/workspace";

function requestName(request: {
    url(): string;
    method(): string;
    headers(): Record<string, string>;
    isNavigationRequest(): boolean;
}) {
    const { pathname } = new URL(request.url());
    if (request.isNavigationRequest()) return `document ${pathname}`;
    if (request.headers().rsc === "1") return `RSC ${pathname}`;
    if (pathname.startsWith("/api/")) return `${request.method()} ${pathname}`;
    return undefined;
}

function recordRequests(page: Page) {
    const events: string[] = [];
    page.on("request", (request) => {
        const name = requestName(request);
        if (name) events.push(`sent ${name}`);
    });
    page.on("requestfinished", (request) => {
        const name = requestName(request);
        if (name) events.push(`answered ${name}`);
    });
    return events;
}

// Read after every DOM change: a superset of what was painted, until the shell or the sign-in form follows another screen.
function watchScreens() {
    const screens: string[] = [];
    Object.assign(window, { launchScreens: screens });
    const screen = () => {
        if (document.querySelector(".shell")) return "shell";
        if (document.querySelector("input[type=password]"))
            return "sign-in form";
        const main = document.querySelector("main");
        if (!main) return "no screen";
        const statuses = document.querySelectorAll("[role=status]").length;
        if (main.querySelector(".auth-card .loading-indicator"))
            return statuses === 1
                ? "launch card"
                : `launch card with ${statuses} statuses`;
        return `other: ${main.textContent}`;
    };
    const record = () => {
        const last = screens.at(-1);
        if (screens.length > 1 && (last === "shell" || last === "sign-in form"))
            return;
        const shown = screen();
        if (shown !== last) screens.push(shown);
    };
    record();
    new MutationObserver(record).observe(document, {
        childList: true,
        subtree: true,
        characterData: true,
        attributes: true,
    });
}

const shownScreens = (page: Page) => () =>
    page.evaluate(() => Reflect.get(window, "launchScreens") as string[]);

async function recordScreens(page: Page) {
    await page.addInitScript(watchScreens);
    return shownScreens(page);
}

async function recordScreensFromNow(page: Page) {
    await page.evaluate(watchScreens);
    return shownScreens(page);
}

async function holdRequest(page: Page, name: string) {
    const reached = Promise.withResolvers<void>();
    const answer = Promise.withResolvers<void>();
    let held = false;
    await page.route(
        (url) => url.pathname === name.slice(name.indexOf(" ") + 1),
        async (route) => {
            if (!held && requestName(route.request()) === name) {
                held = true;
                reached.resolve();
                await answer.promise;
            }
            await route.fallback();
        },
    );
    return { reached: reached.promise, release: () => answer.resolve() };
}

async function failWorkspaceReads(page: Page) {
    const reads = { failing: false };
    await page.route("**/api/tenants/", (route) =>
        reads.failing
            ? route.fulfill({
                  status: 503,
                  json: errorBody("busy", "The workspaces could not be read."),
              })
            : route.fallback(),
    );
    return reads;
}

const listAnswer = (response: { url(): string }) =>
    new URL(response.url()).pathname === "/api/tenants/";

async function readStaleListsOnFocus(page: Page) {
    await page.evaluate(() =>
        window.dispatchEvent(new Event("visibilitychange")),
    );
}

async function servedStatuses(page: Page, html: string) {
    return page.evaluate(
        (source) =>
            new DOMParser()
                .parseFromString(source, "text/html")
                .querySelectorAll("main .auth-card [role=status]").length,
        html,
    );
}

const plantCard = (page: Page, name = /East array/) =>
    page.getByRole("button", { name });

test("a signed-in launch reaches the plants through one navigation behind the launch card", async ({
    page,
}) => {
    // A fixed date keeps every cached answer fresh, so each read the launch mounts is sent once.
    await page.clock.setFixedTime(instant);
    await mockApi(page);
    const screens = await recordScreens(page);
    const events = recordRequests(page);
    const response = await page.goto("/app/");
    await expect(plantCard(page)).toBeVisible();
    const shown = [...events];
    expect(shown.filter((event) => event.startsWith("sent "))).toEqual([
        "sent document /app/",
        "sent GET /api/session/",
        "sent GET /api/tenants/",
        "sent RSC /app/w/1/",
        "sent GET /api/tenants/1/sites/",
    ]);
    expect(shown).toEqual(
        expect.arrayContaining([
            "answered GET /api/session/",
            "answered GET /api/tenants/",
        ]),
    );
    expect(shown.indexOf("answered GET /api/session/")).toBeLessThan(
        shown.indexOf("sent GET /api/tenants/"),
    );
    expect(shown.indexOf("answered GET /api/tenants/")).toBeLessThan(
        shown.indexOf("sent RSC /app/w/1/"),
    );
    expect(await servedStatuses(page, await response!.text())).toBe(1);
    expect(await screens()).toEqual(["no screen", "launch card", "shell"]);
    await expect(page).toHaveURL("/app/w/1/");
    await expect(page.getByText("Loading your workspaces…")).toHaveCount(0);
});

test("a signed-out launch shows the launch card and then the sign-in form", async ({
    page,
}) => {
    await mockApi(page, { anonymous: true });
    const screens = await recordScreens(page);
    const response = await page.goto("/app/");
    await expect(
        page.getByRole("heading", { name: "Welcome back" }),
    ).toBeVisible();
    expect(await servedStatuses(page, await response!.text())).toBe(1);
    expect(await screens()).toEqual([
        "no screen",
        "launch card",
        "sign-in form",
    ]);
    await expect(page).toHaveURL("/app/");
});

test("the launch card is accessible while the session is read", async ({
    page,
}) => {
    await mockApi(page);
    const answer = Promise.withResolvers<void>();
    await page.route("**/api/session/", async (route) => {
        await answer.promise;
        await route.fallback();
    });
    await page.goto("/app/");
    await expect(page.getByRole("status")).toHaveCount(1);
    await expect(
        page.getByRole("progressbar", { name: "Loading…" }),
    ).toBeVisible();
    expect(await accessibilityViolations(page)).toEqual([]);
    answer.resolve();
    await expect(plantCard(page)).toBeVisible();
});

test("the launch card stays while the workspaces are read", async ({
    page,
}) => {
    await mockApi(page);
    const reached = Promise.withResolvers<void>();
    const answer = Promise.withResolvers<void>();
    await page.route("**/api/tenants/", async (route) => {
        reached.resolve();
        await answer.promise;
        await route.fallback();
    });
    await page.goto("/app/");
    await reached.promise;
    await expect(page).toHaveURL("/app/");
    await expect(
        page.getByRole("progressbar", { name: "Loading…" }),
    ).toBeVisible();
    await expect(page.getByRole("status")).toHaveCount(1);
    await expect(page.locator(".shell")).toHaveCount(0);
    answer.resolve();
    await expect(plantCard(page)).toBeVisible();
    await expect(page).toHaveURL("/app/w/1/");
});

const sentCount = (events: string[], name: string) =>
    events.filter((event) => event === `sent ${name}`).length;

test("a launch whose workspace list fails offers Try again on the card", async ({
    page,
}) => {
    await page.clock.setFixedTime(instant);
    await mockApi(page);
    const events = recordRequests(page);
    let failing = true;
    await page.route("**/api/tenants/", (route) =>
        failing
            ? route.fulfill({
                  status: 503,
                  json: errorBody("busy", "The workspaces could not be read."),
              })
            : route.fallback(),
    );
    await page.goto("/app/");
    await expect(
        page.getByRole("heading", { name: "Pyracast could not be opened" }),
    ).toBeVisible();
    await expect(
        page.getByText("The workspaces could not be read."),
    ).toBeVisible();
    await expect(page).toHaveURL("/app/");
    const lists = sentCount(events, "GET /api/tenants/");
    const sessions = sentCount(events, "GET /api/session/");
    failing = false;
    await page.getByRole("button", { name: "Try again", exact: true }).click();
    await expect(plantCard(page)).toBeVisible();
    await expect(page).toHaveURL("/app/w/1/");
    expect(sentCount(events, "GET /api/tenants/")).toBe(lists + 1);
    expect(sentCount(events, "GET /api/session/")).toBe(sessions);
});

const twoWorkspaces = paged([
    tenantRow({ id: 1, name: "Sunfield Energy", role: "admin" }),
    tenantRow({ id: 2, name: "Second Energy", role: "viewer" }),
]);

async function serveSecondWorkspace(page: Page) {
    await page.route("**/api/tenants/2/sites/", (route) =>
        route.fulfill({
            json: paged([
                {
                    ...region,
                    id: 12,
                    name: "Konya field",
                    plant: { ...plant, id: 22, name: "West array" },
                    reminder: null,
                },
            ]),
        }),
    );
}

async function rememberWorkspace(page: Page, tenant: number) {
    await page.addInitScript((id) => {
        window.localStorage.setItem("pyracast.workspace", String(id));
    }, tenant);
}

test("a launch opens the workspace remembered on the device", async ({
    page,
}) => {
    await mockApi(page);
    await page.route("**/api/tenants/", (route) =>
        route.fulfill({ json: twoWorkspaces }),
    );
    await serveSecondWorkspace(page);
    await rememberWorkspace(page, 2);
    await page.goto("/app/");
    await expect(plantCard(page, /West array/)).toBeVisible();
    await expect(page).toHaveURL("/app/w/2/");
});

test("a launch with a remembered workspace the person has left opens the first one", async ({
    page,
}) => {
    await mockApi(page);
    await rememberWorkspace(page, 7);
    await page.goto("/app/");
    await expect(plantCard(page)).toBeVisible();
    await expect(page).toHaveURL("/app/w/1/");
});

test("a launch without a workspace shows the empty state", async ({ page }) => {
    await mockApi(page);
    await page.route("**/api/tenants/", (route) =>
        route.fulfill({ json: paged([]) }),
    );
    await page.goto("/app/");
    await expect(
        page.getByRole("heading", { name: "No workspace yet" }),
    ).toBeVisible();
    await expect(page).toHaveURL("/app/w/");
});

test("a launch with an invitation stored in the tab opens the invitation", async ({
    page,
}) => {
    await mockApi(page);
    await page.addInitScript(() =>
        window.sessionStorage.setItem(
            "pyracast.workspaceInvitation",
            "invite-secret",
        ),
    );
    const tenants: string[] = [];
    page.on("request", (request) => {
        if (new URL(request.url()).pathname === "/api/tenants/")
            tenants.push(request.url());
    });
    await page.goto("/app/");
    await expect(
        page.getByRole("heading", { name: "Join workspace" }),
    ).toBeVisible();
    await expect(page).toHaveURL("/app/invitation/");
    expect(tenants).toEqual([]);
});

async function openStartAddress(page: Page) {
    await page.evaluate(() =>
        (
            window as unknown as {
                next: { router: { push(href: string): void } };
            }
        ).next.router.push("/app/"),
    );
}

const pastFreshness = new Date(instant.getTime() + 10_000);

test("a start address opened with a cached workspace list chooses from the list read again", async ({
    page,
}) => {
    await page.clock.setFixedTime(instant);
    await mockApi(page);
    let workspaces = twoWorkspaces;
    await page.route("**/api/tenants/", (route) =>
        route.fulfill({ json: workspaces }),
    );
    await serveSecondWorkspace(page);
    await page.goto("/app/w/2/");
    await expect(plantCard(page, /West array/)).toBeVisible();
    workspaces = paged([
        tenantRow({ id: 1, name: "Sunfield Energy", role: "admin" }),
    ]);
    await page.clock.setFixedTime(pastFreshness);
    const events = recordRequests(page);
    await openStartAddress(page);
    await expect(plantCard(page)).toBeVisible();
    await expect(page).toHaveURL("/app/w/1/");
    expect(events.filter((event) => event.startsWith("sent "))).toEqual([
        "sent RSC /app/",
        "sent GET /api/tenants/",
        "sent RSC /app/w/1/",
        "sent GET /api/tenants/1/sites/",
    ]);
    await expect(
        page.getByText("This workspace is not available to your account."),
    ).toHaveCount(0);
});

test("a start address whose cached workspace list cannot be read again offers Try again on the card", async ({
    page,
}) => {
    await page.clock.setFixedTime(instant);
    await mockApi(page);
    const reads = await failWorkspaceReads(page);
    await page.goto("/app/w/1/");
    await expect(plantCard(page)).toBeVisible();
    reads.failing = true;
    await page.clock.setFixedTime(pastFreshness);
    await openStartAddress(page);
    await expect(
        page.getByRole("heading", { name: "Pyracast could not be opened" }),
    ).toBeVisible();
    await expect(
        page.getByText("The workspaces could not be read."),
    ).toBeVisible();
    await expect(page).toHaveURL("/app/");
    reads.failing = false;
    await page.getByRole("button", { name: "Try again", exact: true }).click();
    await expect(plantCard(page)).toBeVisible();
    await expect(page).toHaveURL("/app/w/1/");
});

test("a start address with a workspace list error in the cache keeps the launch card until its own read answers", async ({
    page,
}) => {
    await page.clock.setFixedTime(instant);
    await mockApi(page);
    const reads = await failWorkspaceReads(page);
    await page.goto("/app/w/1/");
    await expect(plantCard(page)).toBeVisible();
    reads.failing = true;
    await page.clock.setFixedTime(pastFreshness);
    const failed = page.waitForResponse(listAnswer);
    await readStaleListsOnFocus(page);
    const answer = await failed;
    expect(answer.status()).toBe(503);
    await answer.finished();
    reads.failing = false;
    const list = await holdRequest(page, "GET /api/tenants/");
    const screens = await recordScreensFromNow(page);
    await openStartAddress(page);
    await list.reached;
    list.release();
    await expect(plantCard(page)).toBeVisible();
    await expect(page).toHaveURL("/app/w/1/");
    expect(await screens()).toEqual(["shell", "launch card", "shell"]);
});

const reorderedWorkspaces = paged([
    tenantRow({ id: 2, name: "Second Energy", role: "viewer" }),
    tenantRow({ id: 1, name: "Sunfield Energy", role: "admin" }),
]);

test("a launch sends one navigation while the workspace list changes before the plants open", async ({
    page,
}) => {
    await page.clock.setFixedTime(instant);
    await mockApi(page);
    let workspaces = twoWorkspaces;
    await page.route("**/api/tenants/", (route) =>
        route.fulfill({ json: workspaces }),
    );
    await serveSecondWorkspace(page);
    const navigation = await holdRequest(page, "RSC /app/w/1/");
    const events = recordRequests(page);
    await page.goto("/app/");
    await navigation.reached;
    workspaces = reorderedWorkspaces;
    await page.clock.setFixedTime(pastFreshness);
    const changed = page.waitForResponse(listAnswer);
    await readStaleListsOnFocus(page);
    await (await changed).finished();
    navigation.release();
    await expect(plantCard(page)).toBeVisible();
    await expect(page).toHaveURL("/app/w/1/");
    expect(events.filter((event) => event.startsWith("sent RSC "))).toEqual([
        "sent RSC /app/w/1/",
    ]);
    expect(sentCount(events, "GET /api/tenants/")).toBe(2);
});

test("a launch keeps the card loading when the workspace list fails after the navigation is sent", async ({
    page,
}) => {
    await page.clock.setFixedTime(instant);
    await mockApi(page);
    const reads = await failWorkspaceReads(page);
    const navigation = await holdRequest(page, "RSC /app/w/1/");
    const screens = await recordScreens(page);
    await page.goto("/app/");
    await navigation.reached;
    reads.failing = true;
    await page.clock.setFixedTime(pastFreshness);
    const failed = page.waitForResponse(listAnswer);
    await readStaleListsOnFocus(page);
    const answer = await failed;
    expect(answer.status()).toBe(503);
    await answer.finished();
    reads.failing = false;
    navigation.release();
    await expect(plantCard(page)).toBeVisible();
    await expect(page).toHaveURL("/app/w/1/");
    expect(await screens()).toEqual(["no screen", "launch card", "shell"]);
});

test("the shell shows the plants while it reads a workspace list that turned stale during the navigation", async ({
    page,
}) => {
    await mockApi(page);
    const navigation = Promise.withResolvers<void>();
    const workspaceRoute = Promise.withResolvers<void>();
    await page.route(
        (url) => url.pathname === "/app/w/1/",
        async (route) => {
            if (route.request().headers().rsc === "1") {
                navigation.resolve();
                await workspaceRoute.promise;
            }
            await route.fallback();
        },
    );
    let lists = 0;
    const secondList = Promise.withResolvers<void>();
    const secondListAnswer = Promise.withResolvers<void>();
    await page.route("**/api/tenants/", async (route) => {
        lists += 1;
        if (lists === 2) {
            secondList.resolve();
            await secondListAnswer.promise;
        }
        await route.fallback();
    });
    await page.goto("/app/");
    await navigation.promise;
    await page.clock.fastForward(3_000);
    workspaceRoute.resolve();
    await secondList.promise;
    await expect(plantCard(page)).toBeVisible();
    expect(lists).toBe(2);
    const answered = page.waitForResponse(
        (response) => new URL(response.url()).pathname === "/api/tenants/",
    );
    secondListAnswer.resolve();
    await answered;
});
