import { readFileSync } from "node:fs";
import http from "node:http";
import path from "node:path";
import type { APIRequestContext } from "@playwright/test";
import { expect, test, type Page } from "../tests/support/test";
import { errorBody } from "../tests/support/errors";
import {
    answers,
    now,
    openPlant,
    serveDirectProduction,
    signIn,
} from "../tests/support/plant-screens";
import { firstAccount, mockApi } from "../tests/support/workspace";
import { serveSession } from "../tests/support/session";
import { API_PORTS, BUILD_DIRECTORIES } from "../testing/builds";

type Build = keyof typeof BUILD_DIRECTORIES;
type Written = { status: number; json: unknown };
type Receipt = { build: Build; tag: string | undefined; status: number };

const BUILDS = Object.keys(BUILD_DIRECTORIES) as Build[];
const DEEP_ADDRESS = "/app/w/1/plants/11/production/";
const SESSION_HOURS = 12;

let buildIds: Record<Build, string>;
let apis: http.Server[] = [];
let write: ((body: never) => Written) | undefined;
const receipts: Receipt[] = [];

function readBuildIds() {
    return Object.fromEntries(
        BUILDS.map((build) => [
            build,
            readFileSync(
                path.resolve(
                    __dirname,
                    "..",
                    BUILD_DIRECTORIES[build],
                    "BUILD_ID",
                ),
                "utf8",
            ).trim(),
        ]),
    ) as Record<Build, string>;
}

async function bodyOf(request: http.IncomingMessage) {
    const chunks: Buffer[] = [];
    for await (const chunk of request) chunks.push(chunk as Buffer);
    return Buffer.concat(chunks).toString("utf8");
}

// Like the server, refuses a tag other than the session's before anything is written.
function answerWrite(
    request: http.IncomingMessage,
    body: string,
    tag: string | undefined,
): Written {
    if (!write || !request.url?.endsWith("/production/changes/"))
        return { status: 404, json: errorBody("not_found", "Not found.") };
    if (tag !== undefined && tag !== firstAccount.tag)
        return {
            status: 409,
            json: errorBody("session_changed", "The session changed."),
        };
    return write(JSON.parse(body) as never);
}

async function answerApi(
    build: Build,
    request: http.IncomingMessage,
    response: http.ServerResponse,
) {
    const body = await bodyOf(request);
    const carried = request.headers["x-session-tag"];
    const tag = Array.isArray(carried) ? carried.join(",") : carried;
    const { status, json } = answerWrite(request, body, tag);
    receipts.push({ build, tag, status });
    response
        .writeHead(status, { "Content-Type": "application/json" })
        .end(JSON.stringify(json));
}

function serveApi(build: Build) {
    const server = http.createServer((request, response) => {
        void answerApi(build, request, response);
    });
    return new Promise<http.Server>((resolve) =>
        server.listen(API_PORTS[build], "127.0.0.1", () => resolve(server)),
    );
}

async function release(request: APIRequestContext) {
    const answer = await request.put("/__front/build", { data: "second" });
    expect(answer.status()).toBe(200);
}

async function signInWithSession(page: Page) {
    await signIn(page);
    await serveSession(
        page,
        { current: firstAccount },
        new Date(now.getTime() + SESSION_HOURS * 3_600_000).toISOString(),
    );
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
        Object.values(buildIds),
    );
    return BUILDS.filter((build) => found.includes(buildIds[build]));
}

async function markDocument(page: Page) {
    await page.evaluate(() => {
        Object.assign(window, { releaseMarker: true });
    });
}

const marked = (page: Page) => page.evaluate(() => "releaseMarker" in window);

const routerPayload = (headers: Record<string, string>) =>
    headers["rsc"] !== undefined &&
    headers["next-router-prefetch"] === undefined;

// The router's navigation payload requests and the document loads, in the order the browser saw them.
function navigations(page: Page) {
    const seen: string[] = [];
    const pathOf = (url: string) => new URL(url).pathname;
    page.on("request", (request) => {
        if (routerPayload(request.headers()))
            seen.push(`payload request ${pathOf(request.url())}`);
        else if (
            request.isNavigationRequest() &&
            request.frame() === page.mainFrame()
        )
            seen.push(`document request ${pathOf(request.url())}`);
    });
    page.on("response", (response) => {
        const request = response.request();
        const build = response.headers()["x-front-build"];
        if (routerPayload(request.headers()))
            seen.push(
                `payload ${response.status()} ${response.headers()["content-type"]} from ${build}`,
            );
        else if (
            request.isNavigationRequest() &&
            request.frame() === page.mainFrame()
        )
            seen.push(`document ${response.status()} from ${build}`);
    });
    return seen;
}

function routerRequests(page: Page) {
    const sent: string[] = [];
    page.on("request", (request) => {
        if (request.headers()["rsc"]) sent.push(request.url());
    });
    return sent;
}

// Accepts an unexpected question, so the navigation goes on and the assertion on `asked` reports it.
function questions(page: Page) {
    const asked: string[] = [];
    page.on("dialog", (dialog) => {
        asked.push(dialog.type());
        void dialog.accept();
    });
    return asked;
}

async function stay(page: Page, sent: string[], start: () => Promise<unknown>) {
    const asked = new Promise<[string, string[]]>((resolve) =>
        page.once("dialog", (dialog) => {
            resolve([dialog.type(), [...sent]]);
            void dialog.dismiss();
        }),
    );
    await start();
    expect(await asked).toEqual(["beforeunload", []]);
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

test.beforeAll(async () => {
    buildIds = readBuildIds();
    expect(buildIds.first).not.toBe(buildIds.second);
    apis = await Promise.all(BUILDS.map(serveApi));
});

test.afterAll(async () => {
    await Promise.all(
        apis.map(
            (server) =>
                new Promise((resolve) => server.close(() => resolve(null))),
        ),
    );
});

test.afterEach(async ({ request }) => {
    write = undefined;
    receipts.length = 0;
    await request.delete("/__front/state");
});

test("after a release a navigation in an open window takes the new build's payload and then loads a document of the new build", async ({
    page,
    request,
}) => {
    await page.clock.setFixedTime(now);
    await signInWithSession(page);
    await page.goto("/app/");
    const plant = page.getByRole("button", { name: /East array/ });
    await expect(plant).toBeVisible();
    expect(await documentBuilds(page)).toEqual(["first"]);
    await markDocument(page);
    await release(request);
    const seen = navigations(page);
    const loaded = page.waitForEvent("load");
    await plant.click();
    await loaded;
    await expect(
        page.getByRole("button", { name: "Forecast" }),
    ).toHaveAttribute("aria-current", "page");
    const destination = new URL(page.url()).pathname;
    expect(seen).toEqual([
        `payload request ${destination}`,
        "payload 200 text/x-component from second",
        `document request ${destination}`,
        "document 200 from second",
    ]);
    expect(await marked(page)).toBe(false);
    expect(await documentBuilds(page)).toEqual(["second"]);
});

test("after a release a changed value asks before each navigation without a payload request, Stay keeps the old document whose save the new release's API answers, and the next navigation loads the new build", async ({
    page,
    request,
}) => {
    await page.clock.setFixedTime(now);
    await signInWithSession(page);
    const server = await serveDirectProduction(page, answers());
    write = server.change;
    await page.route("**/production/changes/", (route) => route.continue());
    await openPlant(page, "Production");
    await page.getByRole("button", { name: /^Edit 08:00/ }).click();
    await editorValue(page).fill("0,5");
    expect(await documentBuilds(page)).toEqual(["first"]);
    await markDocument(page);
    const url = page.url();
    await release(request);
    const sent = routerRequests(page);
    for (const target of [shellPlants, comparison]) {
        await stay(page, sent, () => target(page).click());
        expect(sent).toEqual([]);
        expect(page.url()).toBe(url);
        await expect(actuals(page)).not.toHaveAttribute("inert");
        await expect(editorValue(page)).toBeEnabled();
        await expect(editorValue(page)).toHaveValue("0,5");
        expect(await marked(page)).toBe(true);
        expect(await documentBuilds(page)).toEqual(["first"]);
    }
    await page
        .locator(".production-row-editor")
        .getByRole("button", { name: "Save", exact: true })
        .click();
    await expect(page.getByText("Production saved.")).toBeVisible();
    expect(receipts).toEqual([
        { build: "second", tag: firstAccount.tag, status: 200 },
    ]);
    expect(server.energy()).toBe(0.5);
    expect(page.url()).toBe(url);
    expect(await documentBuilds(page)).toEqual(["first"]);
    const asked = questions(page);
    const loaded = page.waitForEvent("load");
    await comparison(page).click();
    await loaded;
    expect(asked).toEqual([]);
    await expect(comparison(page)).toHaveAttribute("aria-current", "page");
    expect(sent).toEqual([]);
    expect(await marked(page)).toBe(false);
    expect(await documentBuilds(page)).toEqual(["second"]);
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
