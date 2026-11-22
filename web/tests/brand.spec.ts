import { test, expect } from "./support/test";
import type { Locator } from "@playwright/test";
import { accessibilityViolations } from "./support/accessibility";
import { mockApi } from "./support/workspace";
import artwork from "./support/brand-artwork.json";

const { colours } = artwork;
const ink = { light: colours.dark, dark: colours.light };

function drawing(svg: SVGElement | HTMLElement) {
    return {
        root: Object.fromEntries(
            [...svg.attributes]
                .filter(({ name }) => name !== "width" && name !== "height")
                .map(({ name, value }) => [name, value]),
        ),
        others: [...svg.querySelectorAll(":not(g, path)")].map(
            (element) => element.tagName,
        ),
        shapes: [...svg.querySelectorAll("path")].map((shape) => {
            const transform: string[] = [];
            let fill: string | null = null;
            for (
                let node: Element | null = shape;
                node && node !== svg;
                node = node.parentElement
            ) {
                const own = node.getAttribute("transform");
                if (own) transform.unshift(own);
                fill ??= node.getAttribute("fill");
            }
            return {
                transform,
                fill: fill?.toLowerCase(),
                d: shape.getAttribute("d"),
            };
        }),
    };
}

function logoDrawing(surface: keyof typeof ink) {
    const { logo, mark } = artwork;
    return {
        root: logo.root,
        others: [],
        shapes: [
            { transform: [logo.mark], fill: colours.sun, d: mark.sun },
            { transform: [logo.mark], fill: ink[surface], d: mark.bar },
            ...logo.letters.map(([letter, d]) => ({
                transform: [logo.wordmark, letter],
                fill: ink[surface],
                d,
            })),
        ],
    };
}

async function expectLogo(place: Locator, surface: keyof typeof ink) {
    const logo = place.getByRole("img", { name: "Pyracast", exact: true });
    await expect(logo).toHaveCount(1);
    await expect(logo).toBeVisible();
    expect(await logo.evaluate(drawing)).toEqual(logoDrawing(surface));
    const box = (await logo.boundingBox())!;
    expect(box.width / box.height).toBeCloseTo(518 / 101, 1);
}

test("the favicon is the kit's favicon file", async ({ request }) => {
    const response = await request.get("/icon.svg");
    expect(response.status()).toBe(200);
    expect(await response.text()).toBe(artwork.favicon);
});

test("the first paint of the application already draws the logo for light surfaces", async ({
    browser,
    baseURL,
}) => {
    const context = await browser.newContext({
        baseURL,
        javaScriptEnabled: false,
    });
    try {
        const page = await context.newPage();
        await page.goto("/app/");
        await expectLogo(page.locator(".auth-card"), "light");
    } finally {
        await context.close();
    }
});

test("the sign-in story names the logo for dark surfaces once", async ({
    page,
}) => {
    await mockApi(page, { anonymous: true });
    await page.goto("/app/");
    await expect(
        page.getByRole("heading", { name: "Welcome back" }),
    ).toBeVisible();
    await expectLogo(page.locator(".auth-story"), "dark");
    await expect(
        page.getByRole("img", { name: "Pyracast", exact: true }),
    ).toHaveCount(1);
    expect(await accessibilityViolations(page)).toEqual([]);
});

test("the workspace sidebar names the logo for dark surfaces once", async ({
    page,
}) => {
    await mockApi(page);
    await page.goto("/app/");
    await expect(page.getByRole("heading", { name: "Plants" })).toBeVisible();
    await expectLogo(page.getByRole("complementary"), "dark");
    await expect(
        page.getByRole("img", { name: "Pyracast", exact: true }),
    ).toHaveCount(1);
    await page.setViewportSize({ width: 360, height: 740 });
    await expectLogo(page.getByRole("complementary"), "dark");
    expect(await accessibilityViolations(page)).toEqual([]);
});

test("the logo's surfaces take the brand palette", async ({ page }) => {
    await mockApi(page);
    await page.goto("/app/");
    await expect(page.getByRole("heading", { name: "Plants" })).toBeVisible();
    await expect(page.locator("body")).toHaveCSS(
        "background-color",
        "rgb(249, 247, 242)",
    );
    await expect(page.getByRole("complementary")).toHaveCSS(
        "background-color",
        "rgb(9, 56, 58)",
    );
    await page.goto("/");
    await expect(page.getByRole("banner").locator("..")).toHaveCSS(
        "background-color",
        "rgb(249, 247, 242)",
    );
});

function contrast(first: string, second: string) {
    const [high, low] = [first, second]
        .map((colour) => {
            const [r, g, b] = colour
                .match(/\d+/g)!
                .slice(0, 3)
                .map((channel) => {
                    const value = Number(channel) / 255;
                    return value <= 0.04045
                        ? value / 12.92
                        : ((value + 0.055) / 1.055) ** 2.4;
                }) as [number, number, number];
            return 0.2126 * r + 0.7152 * g + 0.0722 * b;
        })
        .sort((a, b) => b - a) as [number, number];
    return (high + 0.05) / (low + 0.05);
}

test("the skip link's focus rings hold over the sidebar and over the page", async ({
    page,
}) => {
    await mockApi(page);
    await page.goto("/app/");
    await expect(page.getByRole("heading", { name: "Plants" })).toBeVisible();
    await page.keyboard.press("Tab");
    const skip = page.getByRole("link", { name: "Skip to content" });
    await expect(skip).toBeFocused();
    await expect(skip).toHaveCSS("outline-style", "solid");
    await expect(skip).toHaveCSS("outline-width", "3px");
    await expect(skip).toHaveCSS("outline-offset", "0px");
    const dark = "rgb(9, 56, 58)";
    const light = "rgb(244, 242, 232)";
    await expect(skip).toHaveCSS("outline-color", dark);
    await expect(skip).toHaveCSS("box-shadow", `${light} 0px 0px 0px 6px`);
    expect(contrast(dark, light)).toBeGreaterThanOrEqual(9);
    const surfaces = [
        await page
            .getByRole("complementary")
            .evaluate((node) => getComputedStyle(node).backgroundColor),
        await page
            .locator("body")
            .evaluate((node) => getComputedStyle(node).backgroundColor),
    ];
    for (const surface of surfaces)
        expect(
            Math.max(contrast(dark, surface), contrast(light, surface)),
            surface,
        ).toBeGreaterThanOrEqual(3);
});

for (const path of ["/", "/tr/"]) {
    test(`the public site ${path} links home through the logo for light surfaces`, async ({
        page,
    }) => {
        await page.goto(path);
        for (const place of [
            page.getByRole("banner"),
            page.getByRole("contentinfo"),
        ]) {
            const home = place.getByRole("link", {
                name: "Pyracast",
                exact: true,
            });
            await expect(home).toHaveAttribute("href", path);
            await expectLogo(home, "light");
        }
    });
}

test("actions take the brand's petrol while the forecast keeps its chart colour", async ({
    page,
}) => {
    await page.goto("/");
    const forecast = "rgb(40, 102, 78)";
    const series = page.locator(
        'figure svg[role="img"] path[stroke="currentColor"]',
    );
    await expect(series).toHaveCount(1);
    await expect(series).toHaveCSS("stroke", forecast);
    await expect(page.locator('[class*="forecastLine"]')).toHaveCSS(
        "border-top-color",
        forecast,
    );
    const production = "rgb(161, 112, 39)";
    const productionSeries = page.locator(
        'figure svg[role="img"] path[stroke-dasharray="5 7"]',
    );
    await expect(productionSeries).toHaveCount(1);
    await expect(productionSeries).toHaveCSS("stroke", production);
    const productionLegend = page.locator('[class*="actualLine"]');
    await expect(productionLegend).toHaveCSS("border-top-color", production);
    await expect(productionLegend).toHaveCSS("border-top-style", "dashed");
    await expect(page.locator('[class*="primaryButton"]').first()).toHaveCSS(
        "background-color",
        "rgb(35, 100, 103)",
    );
    await mockApi(page);
    await page.goto("/app/");
    await expect(page.getByRole("heading", { name: "Plants" })).toBeVisible();
    await expect(page.locator(".button").first()).toHaveCSS(
        "background-color",
        "rgb(17, 105, 109)",
    );
});
