import { readFileSync } from "node:fs";
import path from "node:path";
import { test, expect } from "./support/test";
import { productionServer } from "./support/server";

test("a fresh CSP nonce permits the app and blocks injected scripts", async ({
    page,
}) => {
    await page.route("**/api/session/", (route) =>
        route.fulfill({ json: { user: null, csrf_token: "token" } }),
    );
    await page.route("**/api/auth/browser/v1/auth/session", (route) =>
        route.fulfill({
            status: 401,
            json: {
                status: 401,
                data: { flows: [] },
                meta: { is_authenticated: false },
            },
        }),
    );
    const response = await page.goto("/app/");
    const policy = response?.headers()["content-security-policy"] ?? "";
    const nonce = policy.match(/'nonce-([^']+)'/)?.[1];
    expect(nonce).toBeTruthy();
    expect(policy.split("; ")).toEqual(
        expect.arrayContaining([
            "base-uri 'none'",
            "report-uri /api/csp-reports/",
        ]),
    );
    await expect(
        page.getByRole("heading", { name: "Welcome back" }),
    ).toBeVisible();
    let injectedResponse = false;
    await page.route("/app/", async (route) => {
        const response = await route.fetch();
        const body = (await response.text()).replace(
            "</head>",
            "<script>document.documentElement.dataset.injected = 'yes'</script><style>html { --injected: yes; }</style></head>",
        );
        expect(body).toContain("document.documentElement.dataset.injected");
        injectedResponse = true;
        await route.fulfill({ response, body });
    });
    const second = await page.reload();
    expect(injectedResponse).toBe(true);
    await expect(page.locator("html")).not.toHaveAttribute("data-injected");
    if (productionServer) {
        expect(policy).not.toContain("'unsafe-eval'");
        expect(
            await page
                .locator("html")
                .evaluate((element) =>
                    getComputedStyle(element).getPropertyValue("--injected"),
                ),
        ).toBe("");
    }
    expect(second?.headers()["content-security-policy"]).not.toContain(
        `'nonce-${nonce}'`,
    );
});

test("the page policy admits workers from the own origin only", async ({
    request,
}) => {
    const response = await request.get("/app/");
    const policy = response.headers()["content-security-policy"] ?? "";
    expect(policy.replace(/'nonce-[^']+'/g, "'nonce'").split("; ")).toEqual([
        "default-src 'self'",
        `script-src 'self' 'nonce' 'strict-dynamic'${productionServer ? "" : " 'unsafe-eval'"}`,
        "style-src 'self'",
        `style-src-elem 'self' ${productionServer ? "'nonce'" : "'unsafe-inline'"}`,
        "style-src-attr 'unsafe-inline'",
        "img-src 'self' data: blob:",
        "font-src 'self'",
        `connect-src 'self'${productionServer ? "" : " ws:"}`,
        "worker-src 'self'",
        "object-src 'none'",
        "base-uri 'none'",
        "form-action 'self'",
        "frame-ancestors 'none'",
        "report-uri /api/csp-reports/",
    ]);
});

test("pages send the same-origin referrer policy", async ({ request }) => {
    for (const path of ["/", "/tr/privacy/", "/app/"]) {
        const response = await request.get(path);
        expect(response.headers()["referrer-policy"], path).toBe("same-origin");
    }
});

const appFiles = [
    ["/app/manifest.webmanifest", "application/manifest+json"],
    ["/app/icons/any-192.png", "image/png"],
    ["/app/icons/any-512.png", "image/png"],
    ["/app/icons/maskable-192.png", "image/png"],
    ["/app/icons/maskable-512.png", "image/png"],
    ["/app/icons/apple-touch-icon.png", "image/png"],
] as const;

test("the app manifest and icons are served as files with their own type and revalidation", async ({
    request,
}) => {
    for (const [file, type] of appFiles) {
        const response = await request.get(file);
        expect(response.status(), file).toBe(200);
        const headers = response.headers();
        expect(headers["content-type"]?.split(";")[0], file).toBe(type);
        expect(headers["cache-control"], file).toBe("no-cache");
        expect(headers["content-security-policy"], file).toBeUndefined();
        expect(headers["x-robots-tag"], file).toBeUndefined();
    }
});

test("an app file address with a trailing slash redirects to the file", async ({
    request,
}) => {
    for (const file of [...appFiles.map(([file]) => file), "/app/sw.js"]) {
        const response = await request.get(`${file}/`, { maxRedirects: 0 });
        expect(response.status(), file).toBe(308);
        expect(response.headers()["location"], file).toBe(file);
    }
});

test("pages and unknown files under /app/ keep the page headers", async ({
    request,
}) => {
    for (const [address, status] of [
        ["/app/", 200],
        ["/app/account/", 200],
        ["/app/w/1/plants/2/forecast/", 200],
        ["/app/icon-1024.png", 404],
        ["/app/icons/any-1024.png", 404],
        ["/app/icons/apple-touch-icon-precomposed.png", 404],
        ["/app/manifest.json", 404],
        ["/app/manifest.webmanifest/child/", 404],
        ["/app/icons/any-192.png/child/", 404],
        ["/app/sw.js/child/", 404],
        ["/app/icons/missing.png/", 404],
        ["/app/w/1.2/", 200],
    ] as const) {
        const response = await request.get(address, { maxRedirects: 0 });
        expect(response.status(), address).toBe(status);
        const headers = response.headers();
        expect(headers["content-security-policy"], address).toMatch(
            /script-src 'self' 'nonce-[^']+' 'strict-dynamic'/,
        );
        expect(headers["cache-control"], address).toContain("no-store");
        expect(headers["x-robots-tag"], address).toBe("noindex, nofollow");
    }
});

test("a spoofed language header on /app/ is ignored", async ({ request }) => {
    const response = await request.get("/app/", {
        headers: { "x-pyracast-public-language": "tr" },
    });
    expect(await response.text()).toMatch(/<html lang="en"/);
});

test("the app worker address serves its script uncached under its own policy", async ({
    request,
}) => {
    const response = await request.get("/app/sw.js");
    expect(response.status()).toBe(200);
    const headers = response.headers();
    expect(headers["content-type"]?.split(";")[0]).toBe("text/javascript");
    expect(headers["cache-control"]).toBe("no-store");
    expect(headers["content-security-policy"]).toBe(
        "default-src 'none'; connect-src 'self'",
    );
    expect(headers["service-worker-allowed"]).toBeUndefined();
    expect(await response.text()).toBe(
        readFileSync(path.resolve(__dirname, "../public/app/sw.js"), "utf8"),
    );
});
