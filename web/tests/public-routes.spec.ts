import { test, expect } from "@playwright/test";
import { languages } from "../src/i18n/language";
import {
    publicPages,
    publicPath,
    publicRoute,
} from "../src/lib/public-metadata";

test("every public path round-trips through publicRoute", () => {
    for (const language of languages)
        for (const page of publicPages) {
            const path = publicPath(language, page);
            expect(publicRoute(path)).toEqual({ language, page });
            if (path !== "/")
                expect(publicRoute(path.slice(0, -1))).toEqual({
                    language,
                    page,
                });
        }
    for (const path of ["/app/", "/tr/foo/", "/privacy/x/", "/en/", ""])
        expect(publicRoute(path)).toBeUndefined();
});

test("slash-less public paths redirect permanently", async ({ request }) => {
    for (const [path, target] of [
        ["/tr", "/tr/"],
        ["/privacy", "/privacy/"],
        ["/tr/terms?ref=mail", "/tr/terms/?ref=mail"],
    ] as const) {
        const response = await request.get(path, { maxRedirects: 0 });
        expect(response.status()).toBe(308);
        const location = new URL(response.headers().location ?? "", "http://x");
        expect(`${location.pathname}${location.search}`).toBe(target);
    }
});
