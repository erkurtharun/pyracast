import { expect, test, type Page } from "./support/test";
import { mockApi, paged, user } from "./support/workspace";

const chunks = "**/_next/static/**/*.js";

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

test("a link into an on-demand section opens it on the first load", async ({
    page,
}) => {
    await mockPlant(page);
    await page.goto("/app/w/1/plants/11/outages/");
    await expect(
        page.getByRole("heading", { name: "Plant outages", exact: true }),
    ).toBeVisible();
    await expect(tab(page, "Outages")).toHaveAttribute("aria-current", "page");
});

async function routeTurkishSession(page: Page) {
    await page.route("**/api/session/", (route) =>
        route.fulfill({
            json: {
                user: { ...user, language_preference: "tr" },
                csrf_token: "signed-token",
                language: "tr",
            },
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
    await mockApi(page);
    await routeTurkishSession(page);
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
    await mockApi(page);
    await page.goto("/app/w/1/plants/11/forecast/");
    await expect(page.getByLabel("Forecast date")).toBeVisible();
    await routeTurkishSession(page);
    await page.evaluate(() => window.dispatchEvent(new Event("focus")));
    await expect(page.getByLabel("Tahmin tarihi")).toBeVisible();
    await expect(page.locator("html")).toHaveAttribute("lang", "tr");
    await expect(tab(page, "Kalibrasyon")).toBeVisible();
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
