import { test, expect } from "@playwright/test";

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
    const response = await page.goto("/");
    const policy = response?.headers()["content-security-policy"] ?? "";
    const nonce = policy.match(/'nonce-([^']+)'/)?.[1];
    expect(nonce).toBeTruthy();
    await expect(
        page.getByRole("heading", { name: "Welcome back" }),
    ).toBeVisible();
    await page.route("/", async (route) => {
        const response = await route.fetch();
        const body = (await response.text()).replace(
            "</head>",
            "<script>document.documentElement.dataset.injected = 'yes'</script><style>html { --injected: yes; }</style></head>",
        );
        await route.fulfill({ response, body });
    });
    const second = await page.reload();
    await expect(page.locator("html")).not.toHaveAttribute("data-injected");
    if (process.env.PLAYWRIGHT_COVERAGE === "1") {
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
