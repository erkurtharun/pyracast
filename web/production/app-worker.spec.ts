import {
    expect,
    test,
    type APIRequestContext,
    type Page,
} from "@playwright/test";
import { accessibilityViolations } from "../tests/support/accessibility";
import { mockApi } from "../tests/support/workspace";

const NOTICE_POLICY =
    "default-src 'none'; style-src 'unsafe-inline'; form-action 'none'; base-uri 'none'; frame-ancestors 'none'";
const PLANT_ADDRESS = "/app/w/1/plants/2/forecast/";
const FRESH_VISITS: [string, string[]][] = [
    ["/", []],
    ["/tr/", []],
    ["/privacy/", []],
    ["/app/", ["/app/sw.js"]],
];

type Marked = Window & { marker?: string; violations?: string[] };
type Probed = Window & { attempts?: string[]; effects?: Promise<void> };

async function controlled(page: Page, address = "/app/") {
    await page.goto(address);
    await page.evaluate(() => navigator.serviceWorker.ready);
    await page.reload();
    expect(await isControlled(page)).toBe(true);
}

const isControlled = (page: Page) =>
    page.evaluate(() => navigator.serviceWorker.controller !== null);

const registrations = (page: Page) =>
    page.evaluate(async () =>
        (await navigator.serviceWorker.getRegistrations()).map(
            ({ scope, updateViaCache }) => ({ scope, updateViaCache }),
        ),
    );

function switchFront(
    request: APIRequestContext,
    setting: "script" | "upstream",
    value: string,
) {
    return request.put(`/__front/${setting}`, { data: value });
}

test.afterEach(async ({ request }) => {
    await request.delete("/__front/state");
});

test("a fresh visit makes no registration attempt outside the app layout", async ({
    page,
}) => {
    await page.addInitScript(() => {
        const probed = window as Probed;
        const attempts: string[] = [];
        probed.attempts = attempts;
        const container = navigator.serviceWorker;
        const register = container.register.bind(container);
        container.register = (url, options) => {
            attempts.push(String(url));
            return register(url, options);
        };
        probed.effects = new Promise((resolve) => {
            // React calls this hook, production builds included, once the effects of a commit ran.
            Object.defineProperty(window, "__REACT_DEVTOOLS_GLOBAL_HOOK__", {
                value: {
                    supportsFiber: true,
                    inject: () => 1,
                    onCommitFiberRoot: () => undefined,
                    onPostCommitFiberRoot: () => resolve(),
                },
            });
        });
    });
    for (const [address, attempted] of FRESH_VISITS) {
        await page.goto(address);
        await page.evaluate(() => (window as Probed).effects);
        expect(
            await page.evaluate(() => (window as Probed).attempts),
            address,
        ).toEqual(attempted);
    }
});

test("the worker controls the app pages and no other page", async ({
    page,
    baseURL,
}) => {
    await controlled(page);
    expect(await registrations(page)).toEqual([
        { scope: `${baseURL}/app/`, updateViaCache: "none" },
    ]);
    await page.goto("/app/account/");
    expect(await isControlled(page)).toBe(true);
    for (const address of ["/", "/tr/", "/admin/"]) {
        await page.goto(address);
        expect(await isControlled(page), address).toBe(false);
    }
});

test("the worker address cannot widen its scope and no other script takes the app scope", async ({
    page,
}) => {
    await controlled(page);
    const refusals = await page.evaluate(async () => {
        const script = document.querySelector<HTMLScriptElement>(
            "script[src^='/_next/static/']",
        )!.src;
        const attempt = (url: string, scope: string) =>
            navigator.serviceWorker.register(url, { scope }).then(
                () => "registered",
                (error: DOMException) => error.name,
            );
        return [
            await attempt("/app/sw.js", "/"),
            await attempt(script, "/app/"),
        ];
    });
    expect(refusals).toEqual(["SecurityError", "SecurityError"]);
});

test("the page policy refuses a worker made from a blob", async ({ page }) => {
    await page.goto("/app/");
    const outcome = await page.evaluate(
        () =>
            new Promise<string>((resolve) => {
                document.addEventListener("securitypolicyviolation", (event) =>
                    resolve(`refused by ${event.effectiveDirective}`),
                );
                const source = new Blob(["postMessage('ran')"], {
                    type: "text/javascript",
                });
                const worker = new Worker(URL.createObjectURL(source));
                worker.onmessage = () => resolve("ran");
            }),
    );
    expect(outcome).toBe("refused by worker-src");
});

test("the worker passes the app documents on and leaves API requests and other methods alone", async ({
    page,
}) => {
    await controlled(page);
    const answered: [string, string, boolean][] = [];
    page.on("response", (response) =>
        answered.push([
            response.request().method(),
            new URL(response.url()).pathname,
            response.fromServiceWorker(),
        ]),
    );
    const plant = await page.goto(PLANT_ADDRESS);
    expect(plant?.fromServiceWorker()).toBe(true);
    await page.evaluate(() => fetch("/api/session/"));
    const posted = page.waitForResponse(
        (response) => response.request().method() === "POST",
    );
    await page.evaluate(() => {
        const form = document.createElement("form");
        form.method = "post";
        form.action = "/app/";
        document.body.append(form);
        form.submit();
    });
    expect((await posted).fromServiceWorker()).toBe(false);
    const fromWorker = answered.filter(([, , worker]) => worker);
    expect(fromWorker).toEqual([["GET", PLANT_ADDRESS, true]]);
    expect(answered).toContainEqual(["GET", "/api/session/", false]);
});

test("the worker leaves nothing stored on the device", async ({
    page,
    context,
}) => {
    await controlled(page);
    await page.goto(PLANT_ADDRESS);
    await context.setOffline(true);
    await page.goto(PLANT_ADDRESS);
    await context.setOffline(false);
    await page.goto("/app/");
    const stored = await page.evaluate(async () => ({
        caches: await caches.keys(),
        databases: await indexedDB.databases(),
    }));
    expect(stored).toEqual({ caches: [], databases: [] });
});

test("a navigation the server does not answer shows the notice, which returns to the app", async ({
    page,
    context,
    baseURL,
}) => {
    await controlled(page);
    await page.addInitScript(() => {
        const marked = window as Marked;
        marked.violations = [];
        document.addEventListener("securitypolicyviolation", (event) =>
            marked.violations?.push(event.effectiveDirective),
        );
    });
    await context.setOffline(true);
    const notice = await page.goto(PLANT_ADDRESS);
    expect(notice?.status()).toBe(503);
    expect(notice?.fromServiceWorker()).toBe(true);
    const headers = notice?.headers() ?? {};
    expect(headers["content-security-policy"]).toBe(NOTICE_POLICY);
    expect(headers["cache-control"]).toBe("no-store");
    await expect(
        page.getByRole("heading", { name: "Pyracast cannot be reached" }),
    ).toBeVisible();
    await expect(
        page.getByRole("heading", { name: "Pyracast'e ulaşılamıyor" }),
    ).toBeVisible();
    expect(await accessibilityViolations(page)).toEqual([]);
    expect(await page.evaluate(() => (window as Marked).violations)).toEqual(
        [],
    );
    await context.setOffline(false);
    const returned = page.waitForResponse(
        (response) =>
            response.request().isNavigationRequest() &&
            response.url() === `${baseURL}${PLANT_ADDRESS}`,
    );
    await page.getByRole("link", { name: "Try again" }).click();
    expect((await returned).status()).toBe(200);
    await expect(page).toHaveTitle("Pyracast workspace | Pyracast");
});

test("an invitation opened without a connection asks to open the link again and returns without its token", async ({
    page,
    context,
    baseURL,
}) => {
    await mockApi(page, { anonymous: true });
    await controlled(page);
    await context.setOffline(true);
    await page.goto("/app/invitation/#token=invite-secret");
    for (const sentence of [
        "If you came here from an invitation link, open that link again from the email.",
        "Buraya bir davet bağlantısıyla geldiyseniz bağlantıyı e-postadan yeniden açın.",
    ])
        await expect(page.getByText(sentence)).toBeVisible();
    await context.setOffline(false);
    await page.getByRole("link", { name: "Try again" }).click();
    await expect(
        page.getByText(
            "This invitation link can no longer be used. Ask the workspace administrator to send it again.",
        ),
    ).toBeVisible();
    expect(page.url()).toBe(`${baseURL}/app/invitation/`);
});

test("a 503 page of the server reaches the window as served", async ({
    page,
    request,
}) => {
    await controlled(page);
    await switchFront(request, "upstream", "http://127.0.0.1:1");
    const answer = await page.goto(PLANT_ADDRESS);
    expect(answer?.status()).toBe(503);
    expect(answer?.fromServiceWorker()).toBe(true);
    expect(answer?.headers()["content-security-policy"]).toBeUndefined();
    expect(await answer?.text()).toBe(
        "No server answers behind the test front.",
    );
});

test("the withdrawal script replaces the worker without touching an open window", async ({
    page,
    context,
    request,
}) => {
    await controlled(page);
    await page.evaluate(() => {
        (window as Marked).marker = "open";
    });
    await switchFront(request, "script", "withdrawal");
    await page.evaluate(async () => {
        const registration =
            await navigator.serviceWorker.getRegistration("/app/");
        await registration!.update();
    });
    await expect.poll(() => registrations(page)).toEqual([]);
    expect(await page.evaluate(() => (window as Marked).marker)).toBe("open");
    expect(new URL(page.url()).pathname).toBe("/app/");
    await context.setOffline(true);
    const second = await context.newPage();
    await expect(second.goto(PLANT_ADDRESS)).rejects.toThrow(
        "net::ERR_INTERNET_DISCONNECTED",
    );
});
