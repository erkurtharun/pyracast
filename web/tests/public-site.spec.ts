import AxeBuilder from "@axe-core/playwright";
import { test, expect } from "./support/test";

const pages = [
    {
        path: "/",
        language: "en",
        alternate: "/tr/",
        title: "Solar production forecasts",
    },
    {
        path: "/tr/",
        language: "tr",
        alternate: "/",
        title: "Güneş enerjisi üretim tahmini",
    },
    {
        path: "/privacy/",
        language: "en",
        alternate: "/tr/privacy/",
        title: "Privacy policy",
    },
    {
        path: "/tr/privacy/",
        language: "tr",
        alternate: "/privacy/",
        title: "Gizlilik politikası",
    },
    {
        path: "/terms/",
        language: "en",
        alternate: "/tr/terms/",
        title: "Terms of use",
    },
    {
        path: "/tr/terms/",
        language: "tr",
        alternate: "/terms/",
        title: "Kullanım koşulları",
    },
] as const;

for (const entry of pages) {
    test(`public ${entry.path} is readable without JavaScript and has localized search metadata`, async ({
        browser,
        baseURL,
    }) => {
        const context = await browser.newContext({
            baseURL,
            javaScriptEnabled: false,
            locale: entry.language === "en" ? "tr-TR" : "en-US",
            viewport: { width: 320, height: 800 },
            extraHTTPHeaders: {
                "x-pyracast-public-language":
                    entry.language === "en" ? "tr" : "en",
            },
        });
        try {
            await context.addCookies([
                {
                    name: "pyracast_language",
                    value: entry.language === "en" ? "tr" : "en",
                    url: baseURL!,
                },
            ]);
            const page = await context.newPage();
            const apiRequests: string[] = [];
            page.on("request", (request) => {
                if (new URL(request.url()).pathname.startsWith("/api/"))
                    apiRequests.push(request.url());
            });
            const response = await page.goto(entry.path);
            expect(response?.status()).toBe(200);
            expect(response?.headers()["x-robots-tag"]).toBeUndefined();
            expect(response?.headers()["content-security-policy"]).toContain(
                "form-action 'self'",
            );
            await expect(page).toHaveTitle(`${entry.title} | Pyracast`);
            await expect(page.locator("html")).toHaveAttribute(
                "lang",
                entry.language,
            );
            await expect(page.locator("h1")).toHaveCount(1);
            await expect(page.locator("h1")).toBeVisible();
            if (entry.path === "/" || entry.path === "/tr/") {
                const schema = await page
                    .locator('script[type="application/ld+json"]')
                    .textContent();
                expect(JSON.parse(schema!)).toMatchObject({
                    "@type": "WebSite",
                    name: "Pyracast",
                    url: "https://pyracast.app/",
                    inLanguage: ["en", "tr"],
                });
            }
            await expect(page.locator('link[rel="canonical"]')).toHaveAttribute(
                "href",
                `https://pyracast.app${entry.path}`,
            );
            await expect(
                page.locator(
                    `link[rel="alternate"][hreflang="${entry.language === "en" ? "tr" : "en"}"]`,
                ),
            ).toHaveAttribute("href", `https://pyracast.app${entry.alternate}`);
            await expect(
                page.locator('meta[name="description"]'),
            ).toHaveAttribute("content", /\S.{30,}/);
            await expect(
                page.locator('meta[property="og:locale"]'),
            ).toHaveAttribute(
                "content",
                entry.language === "en" ? "en_US" : "tr_TR",
            );
            await expect(page.locator('meta[name="robots"]')).toHaveAttribute(
                "content",
                "index, follow",
            );
            await expect(
                page.locator('a[href="mailto:info@pyracast.app"]').first(),
            ).toBeVisible();
            expect(
                await page.evaluate(
                    () =>
                        document.documentElement.scrollWidth <=
                        window.innerWidth,
                ),
            ).toBe(true);
            expect(apiRequests).toEqual([]);
            await page
                .locator(
                    `header a[hreflang="${entry.language === "en" ? "tr" : "en"}"]`,
                )
                .click();
            await expect(page).toHaveURL(entry.alternate);
        } finally {
            await context.close();
        }
    });
}

test("public pages meet accessibility checks at mobile and desktop sizes", async ({
    page,
}) => {
    for (const width of [390, 1440]) {
        await page.setViewportSize({ width, height: 900 });
        for (const path of ["/", "/tr/privacy/", "/terms/"]) {
            await page.goto(path);
            const results = await new AxeBuilder({ page })
                .withTags(["wcag2a", "wcag2aa", "wcag21aa", "wcag22aa"])
                .analyze();
            expect(results.violations).toEqual([]);
        }
    }
});

test("search discovery contains only public canonical pages", async ({
    request,
}) => {
    const sitemap = await request.get("/sitemap.xml");
    expect(sitemap.status()).toBe(200);
    const xml = await sitemap.text();
    expect(
        [...xml.matchAll(/<loc>(.*?)<\/loc>/g)].map((match) => match[1]).sort(),
    ).toEqual(pages.map(({ path }) => `https://pyracast.app${path}`).sort());
    expect(xml).toContain('hreflang="tr"');
    expect(xml).toContain('hreflang="en"');
    const robots = await request.get("/robots.txt");
    expect(await robots.text()).toContain(
        "Sitemap: https://pyracast.app/sitemap.xml",
    );
    expect(await robots.text()).toContain("Disallow: /api/");
    const app = await request.get("/app/");
    expect(app.headers()["x-robots-tag"]).toBe("noindex, nofollow");
    expect(await app.text()).toMatch(
        /name="robots" content="noindex, nofollow"/,
    );
    const image = await request.get("/opengraph-image");
    expect(image.status()).toBe(200);
    expect(image.headers()["content-type"]).toContain("image/png");
    const data = await image.body();
    expect(data.readUInt32BE(16)).toBe(1200);
    expect(data.readUInt32BE(20)).toBe(630);
});

test("previous account links retain their parameters on the private route", async ({
    request,
}) => {
    const response = await request.get(
        "/?social_callback=1&error=denied&account=security&item=a&item=b",
        { maxRedirects: 0 },
    );
    expect(response.status()).toBe(307);
    expect(response.headers().location).toBe(
        "/app/?social_callback=1&error=denied&account=security&item=a&item=b",
    );
});
