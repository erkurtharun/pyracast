import type { BrowserContext } from "@playwright/test";

export async function languageCookieOf(context: BrowserContext) {
    return (await context.cookies()).find(
        ({ name }) => name === "pyracast_language",
    );
}
