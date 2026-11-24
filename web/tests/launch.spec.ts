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

// Read after every DOM change once parsed: a superset of what was painted.
async function recordScreens(page: Page) {
    await page.addInitScript(() => {
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
            if (last === "shell" || last === "sign-in form") return;
            const shown = screen();
            if (shown !== last) screens.push(shown);
        };
        document.addEventListener("DOMContentLoaded", () => {
            record();
            new MutationObserver(record).observe(document, {
                childList: true,
                subtree: true,
                characterData: true,
                attributes: true,
            });
        });
    });
    return () =>
        page.evaluate(() => Reflect.get(window, "launchScreens") as string[]);
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
    expect(shown.indexOf("answered GET /api/session/")).toBeLessThan(
        shown.indexOf("sent GET /api/tenants/"),
    );
    expect(shown.indexOf("answered GET /api/tenants/")).toBeLessThan(
        shown.indexOf("sent RSC /app/w/1/"),
    );
    expect(await servedStatuses(page, await response!.text())).toBe(1);
    expect(await screens()).toEqual(["launch card", "shell"]);
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
    expect(await screens()).toEqual(["launch card", "sign-in form"]);
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

test("a launch whose workspace list fails offers Try again on the card", async ({
    page,
}) => {
    await mockApi(page);
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
    failing = false;
    await page.getByRole("button", { name: "Try again", exact: true }).click();
    await expect(plantCard(page)).toBeVisible();
    await expect(page).toHaveURL("/app/w/1/");
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
