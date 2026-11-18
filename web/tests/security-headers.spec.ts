import { test, expect } from "@playwright/test";
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

test("pages send the same-origin referrer policy", async ({ request }) => {
    for (const path of ["/", "/tr/privacy/", "/app/"]) {
        const response = await request.get(path);
        expect(response.headers()["referrer-policy"], path).toBe("same-origin");
    }
});

test("a spoofed language header on /app/ is ignored", async ({ request }) => {
    const response = await request.get("/app/", {
        headers: { "x-pyracast-public-language": "tr" },
    });
    expect(await response.text()).toMatch(/<html lang="en"/);
});
