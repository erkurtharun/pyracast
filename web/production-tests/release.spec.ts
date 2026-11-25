import { readFileSync } from "node:fs";
import path from "node:path";
import type { APIRequestContext } from "@playwright/test";
import { expect, test, type Page } from "../tests/support/test";
import {
    answers,
    now,
    openPlant,
    serveDirectProduction,
    signIn,
} from "../tests/support/plant-screens";
import { firstAccount, mockApi } from "../tests/support/workspace";
import { serveSession } from "../tests/support/session";
import { BUILD_DIRECTORIES } from "../testing/builds";

type Build = keyof typeof BUILD_DIRECTORIES;

const BUILD_IDS = Object.fromEntries(
    Object.entries(BUILD_DIRECTORIES).map(([build, directory]) => [
        build,
        readFileSync(
            path.resolve(__dirname, "..", directory, "BUILD_ID"),
            "utf8",
        ).trim(),
    ]),
) as Record<Build, string>;
const DEEP_ADDRESS = "/app/w/1/plants/11/production/";

async function release(request: APIRequestContext) {
    const answer = await request.put("/__front/build", { data: "second" });
    expect(answer.status()).toBe(200);
}

async function servedBuild(request: APIRequestContext) {
    const answer = await request.get("/__front/state");
    return ((await answer.json()) as { build: Build }).build;
}

// The build id is in the flight data the server inlined into the document's scripts.
async function documentBuilds(page: Page) {
    const found = await page.evaluate(
        (ids) =>
            ids.filter((id) =>
                [...document.scripts].some((script) =>
                    script.text.includes(`\\"b\\":\\"${id}\\"`),
                ),
            ),
        Object.values(BUILD_IDS),
    );
    return (Object.keys(BUILD_IDS) as Build[]).filter((build) =>
        found.includes(BUILD_IDS[build]),
    );
}

async function markDocument(page: Page) {
    await page.evaluate(() => {
        Object.assign(window, { releaseMarker: true });
    });
}

const marked = (page: Page) =>
    page.evaluate(() => "releaseMarker" in window).catch(() => true);

function questions(page: Page) {
    const asked: string[] = [];
    page.on("dialog", (dialog) => {
        asked.push(dialog.type());
    });
    return asked;
}

async function stay(page: Page, start: () => Promise<unknown>) {
    const asked = new Promise<string>((resolve) =>
        page.once("dialog", (dialog) => {
            resolve(dialog.type());
            void dialog.dismiss();
        }),
    );
    await start();
    expect(await asked).toBe("beforeunload");
}

const editorValue = (page: Page) =>
    page.locator(".production-row-editor").getByRole("textbox").first();

const actuals = (page: Page) =>
    page.locator("fieldset", {
        has: page.getByRole("heading", { name: "Actual production" }),
    });

const comparison = (page: Page) =>
    page.getByRole("button", { name: "Comparison", exact: true });

const shellPlants = (page: Page) =>
    page.getByRole("button", { name: "Plants", exact: true });

test.beforeAll(() => {
    expect(BUILD_IDS.first).not.toBe(BUILD_IDS.second);
});

test.afterEach(async ({ request }) => {
    await request.delete("/__front/state");
});

test("after a release a navigation in an open window loads a document of the new build", async ({
    page,
    request,
}) => {
    await page.clock.setFixedTime(now);
    await signIn(page);
    await page.goto("/app/");
    const plant = page.getByRole("button", { name: /East array/ });
    await expect(plant).toBeVisible();
    expect(await documentBuilds(page)).toEqual(["first"]);
    await markDocument(page);
    await release(request);
    await plant.click();
    await expect(
        page.getByRole("button", { name: "Forecast" }),
    ).toHaveAttribute("aria-current", "page");
    await expect.poll(() => marked(page)).toBe(false);
    expect(await documentBuilds(page)).toEqual(["second"]);
});

test("after a release a changed value asks before each navigation, Stay keeps the old document whose save the new server applies, and the next navigation loads the new build", async ({
    page,
    request,
}) => {
    await page.clock.setFixedTime(now);
    await signIn(page);
    const server = await serveDirectProduction(page, answers());
    const savedWhileServing: Build[] = [];
    await page.route("**/production/changes/", async (route) => {
        savedWhileServing.push(await servedBuild(request));
        await route.fallback();
    });
    await openPlant(page, "Production");
    await page.getByRole("button", { name: /^Edit 08:00/ }).click();
    await editorValue(page).fill("0,5");
    expect(await documentBuilds(page)).toEqual(["first"]);
    await markDocument(page);
    const url = page.url();
    await release(request);
    for (const target of [shellPlants, comparison]) {
        await stay(page, () => target(page).click());
        await expect(actuals(page)).not.toHaveAttribute("inert");
        await expect(editorValue(page)).toHaveValue("0,5");
        expect(await marked(page)).toBe(true);
        expect(await documentBuilds(page)).toEqual(["first"]);
    }
    await page
        .locator(".production-row-editor")
        .getByRole("button", { name: "Save", exact: true })
        .click();
    await expect(page.getByText("Production saved.")).toBeVisible();
    expect(server.energy()).toBe(0.5);
    expect(savedWhileServing).toEqual(["second"]);
    expect(page.url()).toBe(url);
    expect(await documentBuilds(page)).toEqual(["first"]);
    const asked = questions(page);
    await comparison(page).click();
    await expect.poll(() => marked(page)).toBe(false);
    await expect(comparison(page)).toHaveAttribute("aria-current", "page");
    expect(await documentBuilds(page)).toEqual(["second"]);
    expect(asked).toEqual([]);
});

test("a sign-in completed on a sign-in screen of the old build at a deep address ends in a document of the new build there", async ({
    page,
    request,
}) => {
    await page.clock.setFixedTime(now);
    await serveSession(page, await mockApi(page, { anonymous: true }));
    await page.goto(DEEP_ADDRESS);
    await expect(
        page.getByRole("heading", { name: "Welcome back" }),
    ).toBeVisible();
    expect(await documentBuilds(page)).toEqual(["first"]);
    await markDocument(page);
    await release(request);
    await page
        .getByLabel("Email", { exact: true })
        .fill(firstAccount.user.email);
    await page
        .getByLabel("Password", { exact: true })
        .fill("Test-only-cobalt-79!");
    await page.locator("form").getByRole("button", { name: "Sign in" }).click();
    await expect(
        page.getByRole("button", { name: "Production", exact: true }),
    ).toHaveAttribute("aria-current", "page");
    expect(await marked(page)).toBe(false);
    expect(new URL(page.url()).pathname).toBe(DEEP_ADDRESS);
    expect(await documentBuilds(page)).toEqual(["second"]);
});
