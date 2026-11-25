import { cadence } from "../src/lib/api/query";
import { errorBody } from "./support/errors";
import { isMarked, markDocument } from "./support/session";
import { expect, test, type Page } from "./support/test";
import {
    accountWith,
    mockApi,
    paged,
    plant,
    region,
    sessionIdentity,
    type AccountState,
} from "./support/workspace";

const chunks = "**/_next/static/**/*.js";
const englishCatalogue = (body: string) => body.includes("Calibration history");
const turkishCatalogue = (body: string) => body.includes("Kalibrasyon geçmişi");
// Code that reads the namespace's texts; a catalogue holds it as a key, not as an argument.
const readingTexts = (namespace: string) => (body: string) =>
    body.includes(`("${namespace}")`);

// Every script is answered from the server; one that matches is held until `release`, or aborted,
// and after `release` none is.
async function interceptScript(
    page: Page,
    matches: (body: string) => boolean,
    outcome: "held" | "aborted",
) {
    let reach!: () => void;
    const reached = new Promise<void>((resolve) => (reach = resolve));
    let settle!: () => void;
    const settled = new Promise<void>((resolve) => (settle = resolve));
    let active = true;
    await page.route(chunks, async (route) => {
        const response = await route.fetch();
        const body = await response.text();
        if (active && matches(body)) {
            reach();
            if (outcome === "aborted") {
                await route.abort("internetdisconnected");
                return;
            }
            await settled;
        }
        await route.fulfill({ response, body });
    });
    return {
        reached,
        release: () => {
            active = false;
            settle();
        },
    };
}

function scriptBodies(page: Page) {
    const bodies: Promise<string>[] = [];
    page.on("response", (response) => {
        if (response.request().resourceType() === "script")
            bodies.push(response.text());
    });
    return bodies;
}

async function recordHeadings(page: Page) {
    await page.addInitScript(() => {
        const seen: string[] = [];
        Object.assign(window, { seenHeadings: seen });
        new MutationObserver(() => {
            for (const heading of document.querySelectorAll("h1, h2"))
                seen.push(heading.textContent ?? "");
        }).observe(document, {
            childList: true,
            subtree: true,
            characterData: true,
        });
    });
}

const seenHeadings = (page: Page) =>
    page.evaluate(
        () => (window as unknown as { seenHeadings: string[] }).seenHeadings,
    );

async function guestSession(page: Page, cookie?: string, baseURL?: string) {
    if (cookie)
        await page
            .context()
            .addCookies([
                { name: "pyracast_language", value: cookie, url: baseURL! },
            ]);
    const account = await mockApi(page, { anonymous: true });
    let language = "en";
    let writes = 0;
    const session = () => ({ ...sessionIdentity(account.current), language });
    await page.route("**/api/session/", (route) =>
        route.fulfill({ json: session() }),
    );
    await page.route("**/api/session/language/", async (route) => {
        writes += 1;
        language = route.request().postDataJSON().language as string;
        await route.fulfill({ json: session() });
    });
    return { writes: () => writes };
}

async function mockPlant(page: Page) {
    await mockApi(page);
    await page.route("**/plants/21/outages/", (route) =>
        route.fulfill({ json: paged([]) }),
    );
}

async function openForecast(page: Page) {
    await mockPlant(page);
    await page.goto("/app/w/1/plants/11/forecast/");
    await expect(page.getByLabel("Forecast date")).toBeVisible();
}

function scriptRequests(page: Page) {
    const urls: string[] = [];
    page.on("request", (request) => {
        if (request.resourceType() === "script") urls.push(request.url());
    });
    return urls;
}

const tab = (page: Page, name: string) =>
    page.getByRole("button", { name, exact: true });

const sections = [
    {
        name: "calibration",
        before: [],
        open: "Calibration",
        shown: (page: Page) =>
            page.getByRole("heading", { name: "Calibration history" }),
    },
    {
        name: "production history with its import and editing",
        before: [],
        open: "Production",
        shown: (page: Page) =>
            page.getByRole("button", { name: "Import CSV", exact: true }),
    },
    {
        name: "performance view",
        before: ["Production"],
        open: "Comparison",
        shown: (page: Page) =>
            page.getByRole("heading", {
                name: "Production comparison",
                exact: true,
            }),
    },
    {
        name: "outage editing",
        before: [],
        open: "Outages",
        shown: (page: Page) =>
            page.getByRole("button", { name: "Report outage", exact: true }),
    },
];

for (const section of sections) {
    test(`the ${section.name} loads its code when it is first opened`, async ({
        page,
    }) => {
        await openForecast(page);
        for (const name of section.before) {
            await tab(page, name).click();
            await expect(tab(page, name)).toHaveAttribute(
                "aria-current",
                "page",
            );
        }
        const requested = scriptRequests(page);
        await tab(page, section.open).click();
        await expect(section.shown(page)).toBeVisible();
        expect(requested).not.toEqual([]);
        await tab(page, "Forecast").click();
        await expect(page.getByLabel("Forecast date")).toBeVisible();
        const again = scriptRequests(page);
        for (const name of [...section.before, section.open])
            await tab(page, name).click();
        await expect(section.shown(page)).toBeVisible();
        expect(again).toEqual([]);
    });
}

test("a section whose code cannot be loaded shows the page error and loads it on retry", async ({
    page,
}) => {
    await openForecast(page);
    await page.route(chunks, (route) => route.abort("internetdisconnected"));
    await tab(page, "Calibration").click();
    await expect(
        page.getByRole("heading", { name: "This page could not be displayed" }),
    ).toBeVisible();
    await expect(page).toHaveURL("/app/w/1/plants/11/calibration/");
    await page.unroute(chunks);
    await page.getByRole("button", { name: "Try again", exact: true }).click();
    await expect(
        page.getByRole("heading", { name: "Calibration history" }),
    ).toBeVisible();
    await expect(tab(page, "Calibration")).toHaveAttribute(
        "aria-current",
        "page",
    );
});

test("a plant screen failure keeps the workspace navigation and offers a reload", async ({
    page,
}) => {
    await openForecast(page);
    await page.route(chunks, (route) => route.abort("internetdisconnected"));
    await tab(page, "Calibration").click();
    await expect(
        page.getByRole("heading", { name: "This page could not be displayed" }),
    ).toBeVisible();
    await expect(
        page
            .getByRole("navigation", { name: "Main navigation" })
            .getByRole("button", { name: "Plants" }),
    ).toBeVisible();
    await page.evaluate(() => Object.assign(window, { failedDocument: true }));
    await page.unroute(chunks);
    await page
        .getByRole("button", { name: "Reload page", exact: true })
        .click();
    await expect(
        page.getByRole("heading", { name: "Calibration history" }),
    ).toBeVisible();
    expect(await page.evaluate(() => "failedDocument" in window)).toBe(false);
});

test("a link into an on-demand section opens it on the first load", async ({
    page,
}) => {
    await mockPlant(page);
    await page.goto("/app/w/1/plants/11/outages/");
    await expect(
        page.getByRole("heading", { name: "Outages", exact: true }),
    ).toBeVisible();
    await expect(tab(page, "Outages")).toHaveAttribute("aria-current", "page");
});

async function routeTurkishSession(page: Page, account: AccountState) {
    account.current = accountWith({ language_preference: "tr" });
    await page.route("**/api/session/", (route) =>
        route.fulfill({
            json: { ...sessionIdentity(account.current), language: "tr" },
        }),
    );
}

test("a Turkish visit loads only the Turkish catalogue", async ({
    page,
    baseURL,
}) => {
    await page.context().addCookies([
        {
            name: "pyracast_language",
            value: "tr",
            url: baseURL!,
        },
    ]);
    const account = await mockApi(page);
    await routeTurkishSession(page, account);
    const scripts: Promise<string>[] = [];
    page.on("response", (response) => {
        if (response.request().resourceType() === "script")
            scripts.push(response.text());
    });
    await page.goto("/app/");
    await expect(
        page.getByRole("heading", { name: "Santraller" }),
    ).toBeVisible();
    const bodies = await Promise.all(scripts);
    expect(bodies.some((body) => body.includes("Kalibrasyon geçmişi"))).toBe(
        true,
    );
    expect(bodies.some((body) => body.includes("Calibration history"))).toBe(
        false,
    );
});

test("a language changed in another tab replaces the whole screen once its catalogue arrives", async ({
    page,
}) => {
    const account = await mockApi(page);
    await page.goto("/app/w/1/plants/11/forecast/");
    await expect(page.getByLabel("Forecast date")).toBeVisible();
    await routeTurkishSession(page, account);
    await page.evaluate(() => window.dispatchEvent(new Event("focus")));
    await expect(page.getByLabel("Tahmin tarihi")).toBeVisible();
    await expect(page.locator("html")).toHaveAttribute("lang", "tr");
    await expect(tab(page, "Kalibrasyon")).toBeVisible();
});

test("a tab change keeps the outgoing section and marks the tabs busy until the next section's code arrives", async ({
    page,
}) => {
    await openForecast(page);
    const section = await interceptScript(page, () => true, "held");
    await tab(page, "Calibration").click();
    await section.reached;
    await expect(page.getByLabel("Forecast date")).toBeVisible();
    await expect(
        page.getByRole("navigation", { name: "Plant sections" }),
    ).toHaveAttribute("aria-busy", "true");
    await expect(tab(page, "Forecast")).toHaveAttribute("aria-current", "page");
    await expect(page.getByText("Loading section…")).toHaveCount(0);
    section.release();
    await expect(
        page.getByRole("heading", { name: "Calibration history" }),
    ).toBeVisible();
    await expect(
        page.getByRole("navigation", { name: "Plant sections" }),
    ).not.toHaveAttribute("aria-busy", "true");
});

test("a link into an on-demand section keeps the plant header, tabs and back button and announces one loading state", async ({
    page,
}) => {
    await mockPlant(page);
    const section = await interceptScript(
        page,
        readingTexts("Outages"),
        "held",
    );
    await page.goto("/app/w/1/plants/11/outages/", {
        waitUntil: "domcontentloaded",
    });
    await section.reached;
    await expect(
        page.getByRole("heading", { name: "East array", level: 1 }),
    ).toBeVisible();
    await expect(
        page.getByRole("button", { name: "← All plants", exact: true }),
    ).toBeVisible();
    await expect(tab(page, "Outages")).toHaveAttribute("aria-current", "page");
    await expect(
        page.getByRole("status").filter({ hasText: "Loading section…" }),
    ).toHaveCount(1);
    await expect(page.getByText("Loading plant…")).toHaveCount(0);
    section.release();
    await expect(
        page.getByRole("heading", { name: "Outages", exact: true }),
    ).toBeVisible();
    await expect(page.getByText("Loading section…")).toHaveCount(0);
});

test("a refresh that brings an on-demand section replaces only the section slot", async ({
    page,
}) => {
    await mockPlant(page);
    let configured = false;
    await page.route("**/api/tenants/1/sites/", (route) =>
        route.fulfill({
            json: paged([
                {
                    ...region,
                    reminder: null,
                    plant: configured
                        ? plant
                        : { ...plant, current_configuration: null },
                },
            ]),
        }),
    );
    await page.goto("/app/w/1/plants/11/calibration/");
    await expect(
        page.getByText(
            "Calibration is available after Pyracast approves the plant.",
        ),
    ).toBeVisible();
    const section = await interceptScript(page, () => true, "held");
    configured = true;
    await page.clock.runFor(cadence.background);
    await section.reached;
    await expect(
        page.getByRole("heading", { name: "East array", level: 1 }),
    ).toBeVisible();
    await expect(tab(page, "Calibration")).toHaveAttribute(
        "aria-current",
        "page",
    );
    await expect(
        page.getByRole("status").filter({ hasText: "Loading section…" }),
    ).toHaveCount(1);
    section.release();
    await expect(
        page.getByRole("heading", { name: "Calibration history" }),
    ).toBeVisible();
});

for (const visit of [
    { name: "in English", cookie: undefined, turkish: false },
    { name: "in Turkish", cookie: "tr", turkish: true },
    {
        name: "when the language cookie and the session disagree",
        cookie: "en",
        turkish: true,
    },
])
    test(`the app renders without a page or console error ${visit.name}`, async ({
        page,
        baseURL,
    }) => {
        const errors: string[] = [];
        page.on("pageerror", (error) => errors.push(error.message));
        page.on("console", (message) => {
            if (message.type() === "error") errors.push(message.text());
        });
        if (visit.cookie)
            await page.context().addCookies([
                {
                    name: "pyracast_language",
                    value: visit.cookie,
                    url: baseURL!,
                },
            ]);
        const account = await mockApi(page);
        if (visit.turkish) await routeTurkishSession(page, account);
        await page.goto("/app/");
        await expect(
            page.getByRole("heading", {
                name: visit.turkish ? "Santraller" : "Plants",
                exact: true,
            }),
        ).toBeVisible();
        expect(errors).toEqual([]);
    });

test("a language hand-off shows no other language before the wanted catalogue arrives", async ({
    page,
    baseURL,
}) => {
    await guestSession(page, "en", baseURL);
    await recordHeadings(page);
    const bodies = scriptBodies(page);
    const turkish = await interceptScript(page, turkishCatalogue, "held");
    await page.goto("/app/?language=tr&signup=1", {
        waitUntil: "domcontentloaded",
    });
    await turkish.reached;
    await expect(
        page.getByRole("heading", { name: "Create workspace" }),
    ).toHaveCount(0);
    await expect(page.getByLabel("Email", { exact: true })).toHaveCount(0);
    turkish.release();
    await expect(
        page.getByRole("heading", { name: "Çalışma alanı oluştur" }),
    ).toBeVisible();
    expect(await seenHeadings(page)).not.toContain("Create workspace");
    expect((await Promise.all(bodies)).some(englishCatalogue)).toBe(false);
});

test("a language hand-off whose catalogue cannot be loaded offers the retry that loads the page again", async ({
    page,
    baseURL,
}) => {
    await guestSession(page, "en", baseURL);
    const turkish = await interceptScript(page, turkishCatalogue, "aborted");
    await page.goto("/app/?language=tr&signup=1");
    await expect(page.getByRole("main").getByRole("alert")).toHaveText(
        "Bu sayfa gösterilirken bir sorun oluştu. Tekrar deneyin; sorun sürerse sayfayı yenileyin.",
    );
    await expect(
        page.getByRole("heading", { name: "Create workspace" }),
    ).toHaveCount(0);
    turkish.release();
    await page
        .getByRole("button", { name: "Tekrar dene", exact: true })
        .click();
    await expect(
        page.getByRole("heading", { name: "Çalışma alanı oluştur" }),
    ).toBeVisible();
});

test("a language whose catalogue cannot be loaded is not saved and offers a reload", async ({
    page,
}) => {
    const session = await guestSession(page);
    const turkish = await interceptScript(page, turkishCatalogue, "aborted");
    await page.goto("/app/");
    await expect(
        page.getByRole("heading", { name: "Welcome back" }),
    ).toBeVisible();
    await page.getByLabel("Language", { exact: true }).selectOption("tr");
    await expect(page.getByRole("main").getByRole("alert")).toHaveText(
        "The language could not be loaded. Reload the page to try again.",
    );
    await expect(page.getByLabel("Language", { exact: true })).toHaveValue(
        "en",
    );
    await expect(page.locator("html")).toHaveAttribute("lang", "en");
    expect(session.writes()).toBe(0);
    turkish.release();
    await page
        .getByRole("button", { name: "Reload page", exact: true })
        .click();
    await expect(
        page.getByRole("heading", { name: "Welcome back" }),
    ).toBeVisible();
    await expect(page.getByRole("main").getByRole("alert")).toHaveCount(0);
    await page.getByLabel("Language", { exact: true }).selectOption("tr");
    await expect(
        page.getByRole("heading", { name: "Tekrar hoş geldiniz" }),
    ).toBeVisible();
    expect(session.writes()).toBe(1);
});

test("a language chosen before signing in whose catalogue arrives afterwards is not saved for the account", async ({
    page,
}) => {
    const account = await mockApi(page, { anonymous: true });
    let language = "en";
    const written: string[] = [];
    const session = () => ({ ...sessionIdentity(account.current), language });
    await page.route("**/api/auth/browser/v1/auth/login", (route) => {
        account.current = accountWith({ language_preference: language });
        return route.fulfill({
            json: { status: 200, meta: { is_authenticated: true } },
        });
    });
    await page.route("**/api/session/", (route) =>
        route.fulfill({ json: session() }),
    );
    await page.route("**/api/session/language/", (route) => {
        language = route.request().postDataJSON().language as string;
        if (account.current)
            account.current = accountWith({ language_preference: language });
        written.push(`${account.current ? "account" : "guest"}:${language}`);
        return route.fulfill({ json: session() });
    });
    const turkish = await interceptScript(page, turkishCatalogue, "held");
    await page.goto("/app/");
    await expect(
        page.getByRole("heading", { name: "Welcome back" }),
    ).toBeVisible();
    await page.getByLabel("Language", { exact: true }).selectOption("tr");
    await turkish.reached;
    await page.getByLabel("Email", { exact: true }).fill("owner@example.test");
    await page
        .getByLabel("Password", { exact: true })
        .fill("Test-only-cobalt-79!");
    await page.locator("form").getByRole("button", { name: "Sign in" }).click();
    await expect(
        page.getByRole("heading", { name: "Plants", exact: true }),
    ).toBeVisible();
    await expect(page.locator("html")).toHaveAttribute("lang", "en");
    // The account's own choice waits on the same held catalogue, so it is sent after anything the
    // sign-in screen's choice could still send.
    await page.getByLabel("Language", { exact: true }).selectOption("tr");
    await expect(
        page.getByRole("status").filter({ hasText: "Saving…" }),
    ).toBeVisible();
    turkish.release();
    await expect(
        page.getByRole("heading", { name: "Santraller", exact: true }),
    ).toBeVisible();
    await expect(page.getByText("Kaydediliyor…")).toHaveCount(0);
    expect(written).toEqual(["account:tr"]);
});

test("a language another tab saved clears this tab's failed save of it for good", async ({
    page,
}) => {
    const account = await mockApi(page, { anonymous: true });
    let language = "en";
    await page.route("**/api/session/", (route) =>
        route.fulfill({
            json: { ...sessionIdentity(account.current), language },
        }),
    );
    await page.route("**/api/session/language/", (route) =>
        route.fulfill({
            status: 500,
            json: errorBody("server_error", "Something went wrong. Try again."),
        }),
    );
    await page.goto("/app/");
    await expect(
        page.getByRole("heading", { name: "Welcome back" }),
    ).toBeVisible();
    await page.getByLabel("Language", { exact: true }).selectOption("tr");
    await expect(
        page.getByText("Language could not be saved. Please try again."),
    ).toBeVisible();
    language = "tr";
    await page.evaluate(() => window.dispatchEvent(new Event("focus")));
    await expect(
        page.getByRole("heading", { name: "Tekrar hoş geldiniz" }),
    ).toBeVisible();
    await expect(page.getByLabel("Dil", { exact: true })).toHaveValue("tr");
    await expect(
        page.getByText("Dil kaydedilemedi. Tekrar deneyin."),
    ).toHaveCount(0);
    language = "en";
    await page.evaluate(() => window.dispatchEvent(new Event("focus")));
    await expect(
        page.getByRole("heading", { name: "Welcome back" }),
    ).toBeVisible();
    await expect(page.getByLabel("Language", { exact: true })).toHaveValue(
        "en",
    );
    await expect(
        page.getByText("Language could not be saved. Please try again."),
    ).toHaveCount(0);
});

test("a language changed in another tab whose catalogue cannot be loaded is reported with a reload", async ({
    page,
}) => {
    const account = await mockApi(page);
    const turkish = await interceptScript(page, turkishCatalogue, "aborted");
    await page.goto("/app/w/1/plants/11/forecast/");
    await expect(page.getByLabel("Forecast date")).toBeVisible();
    await routeTurkishSession(page, account);
    await page.evaluate(() => window.dispatchEvent(new Event("focus")));
    await expect(
        page
            .getByRole("alert")
            .filter({ hasText: "The language could not be loaded." }),
    ).toHaveText(
        "The language could not be loaded. Reload the page to try again.",
    );
    await expect(page.getByLabel("Forecast date")).toBeVisible();
    await expect(page.locator("html")).toHaveAttribute("lang", "en");
    turkish.release();
    await page
        .getByRole("button", { name: "Reload page", exact: true })
        .click();
    await expect(page.getByLabel("Tahmin tarihi")).toBeVisible();
});

function languageWrites(page: Page) {
    const written: string[] = [];
    page.on("request", (request) => {
        if (new URL(request.url()).pathname === "/api/session/language/")
            written.push(request.postDataJSON().language as string);
    });
    return written;
}

async function endSessionElsewhere(page: Page, account: AccountState) {
    account.current = null;
    await page.evaluate(() => window.dispatchEvent(new Event("focus")));
    await expect(
        page.getByRole("heading", { name: "Welcome back" }),
    ).toBeVisible();
}

test("a language whose catalogue cannot be loaded keeps the workspace in its language, saves nothing and offers a reload", async ({
    page,
}) => {
    await mockApi(page);
    const written = languageWrites(page);
    await interceptScript(page, turkishCatalogue, "aborted");
    await page.goto("/app/");
    await expect(
        page.getByRole("heading", { name: "Plants", exact: true }),
    ).toBeVisible();
    await page.getByLabel("Language", { exact: true }).selectOption("tr");
    await expect(
        page
            .getByRole("alert")
            .filter({ hasText: "The language could not be loaded." }),
    ).toHaveText(
        "The language could not be loaded. Reload the page to try again.",
    );
    await expect(
        page.getByRole("button", { name: "Reload page", exact: true }),
    ).toBeVisible();
    await expect(page.getByLabel("Language", { exact: true })).toHaveValue(
        "en",
    );
    await expect(page.locator("html")).toHaveAttribute("lang", "en");
    await expect(
        page.getByRole("heading", { name: "Plants", exact: true }),
    ).toBeVisible();
    expect(written).toEqual([]);
});

test("a language save refused after the session ended reports nothing on the screen that replaced the workspace", async ({
    context,
    page,
}) => {
    const account = await mockApi(page);
    await page.route("**/api/session/", (route) =>
        route.fulfill({
            json: {
                ...sessionIdentity(account.current),
                language: "en",
                session_tag: account.current?.tag ?? "tag-b",
            },
        }),
    );
    // Another document of the origin that only writes the observed tag, as a tab's read does.
    const other = await context.newPage();
    await other.route("**/blank", (route) =>
        route.fulfill({ contentType: "text/html", body: "<html></html>" }),
    );
    let reach!: () => void;
    const reached = new Promise<void>((resolve) => (reach = resolve));
    let refuse!: () => void;
    const refused = new Promise<void>((resolve) => (refuse = resolve));
    await page.route("**/api/session/language/", async (route) => {
        reach();
        await refused;
        await route.fulfill({
            status: 500,
            json: errorBody("server_error", "Something went wrong. Try again."),
        });
    });
    await page.goto("/app/");
    await expect(
        page.getByRole("heading", { name: "Plants", exact: true }),
    ).toBeVisible();
    await other.goto("/blank");
    await page.getByLabel("Language", { exact: true }).selectOption("tr");
    await reached;
    account.current = null;
    await other.evaluate(() =>
        window.localStorage.setItem("pyracast.sessionTag", "tag-b"),
    );
    await expect(
        page.getByRole("progressbar", { name: "Loading…" }),
    ).toBeVisible();
    await expect(page.getByLabel("Language", { exact: true })).toHaveCount(0);
    refuse();
    // The read that replaces the workspace is queued behind the refused save, so its screen follows it.
    await expect(
        page.getByRole("heading", { name: "Welcome back" }),
    ).toBeVisible();
    await expect(
        page.getByText("Language could not be saved. Please try again."),
    ).toHaveCount(0);
    await expect(page.getByRole("main").getByRole("alert")).toHaveCount(0);
});

test("a language whose catalogue is still loading when the session ends elsewhere is not saved and the fresh document reports nothing", async ({
    page,
}) => {
    const account = await mockApi(page);
    const written = languageWrites(page);
    const turkish = await interceptScript(page, turkishCatalogue, "held");
    await page.goto("/app/");
    await expect(
        page.getByRole("heading", { name: "Plants", exact: true }),
    ).toBeVisible();
    await markDocument(page);
    await page.getByLabel("Language", { exact: true }).selectOption("tr");
    await turkish.reached;
    await endSessionElsewhere(page, account);
    expect(await isMarked(page)).toBe(false);
    await expect(page.getByRole("main").getByRole("alert")).toHaveCount(0);
    await expect(
        page.getByRole("button", { name: "Reload page", exact: true }),
    ).toHaveCount(0);
    await expect(page.getByLabel("Language", { exact: true })).toHaveValue(
        "en",
    );
    await expect(
        page.getByRole("heading", { name: "Welcome back" }),
    ).toBeVisible();
    expect(written).toEqual([]);
    turkish.release();
});

test("a catalogue that cannot be loaded offers a retry that loads the page again", async ({
    page,
}) => {
    await mockApi(page);
    let blocked = true;
    await page.route(chunks, async (route) => {
        const response = await route.fetch();
        const body = await response.text();
        if (blocked && body.includes("Calibration history"))
            await route.abort("internetdisconnected");
        else await route.fulfill({ response, body });
    });
    await page.goto("/app/");
    await expect(page.getByRole("main").getByRole("alert")).toHaveText(
        "Something went wrong while showing this page. Try again; if the problem continues, reload the page.",
    );
    blocked = false;
    await page.getByRole("button", { name: "Try again", exact: true }).click();
    await expect(
        page.getByRole("heading", { name: "Plants", exact: true }),
    ).toBeVisible();
});
