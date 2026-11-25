import { test, expect, type Page } from "./support/test";
import {
    installedModes,
    setDisplayMode,
    standInDisplayMode,
} from "./support/display-mode";
import { mockApi, user } from "./support/workspace";

const copy = {
    en: {
        name: "English",
        plants: "Plants",
        forecast: "Forecast",
        settings: "Settings",
    },
    tr: {
        name: "Turkish",
        plants: "Santraller",
        forecast: "Tahmin",
        settings: "Ayarlar",
    },
} as const;
type Language = keyof typeof copy;
const languages = Object.keys(copy) as Language[];
const workspace = "Sunfield Energy";
const plant = "East array";

async function signIn(page: Page, language: Language) {
    await mockApi(page);
    await page.route("**/api/session/", (route) =>
        route.fulfill({
            json: {
                user: { ...user, language_preference: language },
                csrf_token: "signed-token",
                language,
            },
        }),
    );
}

async function openPortfolio(page: Page, language: Language) {
    await page.goto("/app/w/1/");
    await expect(
        page.getByRole("heading", { name: copy[language].plants }),
    ).toBeVisible();
}

test("the start address and the sign-in screen carry the app's name alone", async ({
    page,
}) => {
    await mockApi(page, { anonymous: true });
    const html = await (await page.goto("/app/"))!.text();
    expect(html.match(/<title>[^<]*<\/title>/g)).toEqual([
        "<title>Pyracast</title>",
    ]);
    await expect(
        page.getByRole("heading", { name: "Welcome back" }),
    ).toBeVisible();
    await expect(page).toHaveTitle("Pyracast");
});

for (const language of languages) {
    const { name, plants, forecast, settings } = copy[language];

    test(`in a browser tab a screen's title names it before the app in ${name}`, async ({
        page,
    }) => {
        await signIn(page, language);
        await openPortfolio(page, language);
        await expect(page).toHaveTitle(`${plants} · ${workspace} | Pyracast`);
        await page.goto("/app/w/1/plants/11/forecast/");
        await expect(page).toHaveTitle(`${forecast} · ${plant} | Pyracast`);
    });

    test(`in an installed window a screen's title leaves the app's name to the window in ${name}`, async ({
        page,
    }) => {
        await standInDisplayMode(page, "standalone");
        await signIn(page, language);
        await openPortfolio(page, language);
        await expect(page).toHaveTitle(`${plants} · ${workspace}`);
        await page.getByRole("button", { name: new RegExp(plant) }).click();
        await expect(page).toHaveTitle(`${forecast} · ${plant}`);
        await page.getByRole("button", { name: settings, exact: true }).click();
        await expect(page).toHaveTitle(`${settings} · ${plant}`);
    });
}

for (const mode of installedModes)
    test(`a screen's title follows a change between a browser tab and a ${mode} window while it is open`, async ({
        page,
    }) => {
        await standInDisplayMode(page, "browser");
        await signIn(page, "en");
        await page.goto("/app/w/1/plants/11/forecast/");
        await expect(page).toHaveTitle(`Forecast · ${plant} | Pyracast`);
        await setDisplayMode(page, mode);
        await expect(page).toHaveTitle(`Forecast · ${plant}`);
        await setDisplayMode(page, "browser");
        await expect(page).toHaveTitle(`Forecast · ${plant} | Pyracast`);
    });

for (const [address, title] of [
    ["/app/w/1/members/", `Members · ${workspace}`],
    ["/app/w/1/inbox/", "Inbox"],
    ["/app/w/1/trash/", "Recycle bin"],
    ["/app/account/", "Account"],
    ["/app/invitation/#token=invite-secret", "Invitation"],
] as const)
    test(`${address} is titled ${title}`, async ({ page }) => {
        await mockApi(page);
        await page.route("**/api/tenants/1/members/", (route) =>
            route.fulfill({
                json: {
                    members: [{ id: 1, email: user.email, role: "admin" }],
                },
            }),
        );
        await page.route("**/api/tenants/1/invitations/", (route) =>
            route.fulfill({ json: { invitations: [] } }),
        );
        await page.goto(address);
        await expect(
            page.getByRole("heading", {
                level: 1,
                name: title.split(" · ")[0],
                exact: true,
            }),
        ).toBeVisible();
        await expect(page).toHaveTitle(`${title} | Pyracast`);
    });
