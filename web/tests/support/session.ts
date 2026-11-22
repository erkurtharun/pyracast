import type { Page } from "@playwright/test";
import { user } from "./workspace";

export type Account = { user: typeof user; tag: string } | null;

export const firstAccount = { user, tag: "tag-a" } as const;
export const secondUser = {
    ...user,
    id: 2,
    username: "second.user",
    email: "second@example.test",
};

export const expiredNotice = "Your session has expired. Please sign in again.";
export const changedNotice =
    "The session changed in another tab. The page now shows the current session.";

export function sessionAnswer(account: Account, expiresAt: string | null) {
    return {
        user: account?.user ?? null,
        csrf_token: account ? "signed-token" : "anonymous-token",
        language: "en",
        expires_at: account ? expiresAt : null,
        session_tag: account?.tag ?? null,
    };
}

export function serveSession(
    page: Page,
    current: () => Account,
    expiresAt: string | null = null,
) {
    return page.route("**/api/session/", (route) =>
        route.fulfill({ json: sessionAnswer(current(), expiresAt) }),
    );
}

export function markDocument(page: Page) {
    return page.evaluate(() => {
        Object.assign(window, { documentMarker: true });
        const send = window.fetch.bind(window);
        window.fetch = (input, init) => {
            const address = new URL(
                input instanceof Request ? input.url : String(input),
                window.location.href,
            );
            if (address.pathname === "/api/tenants/") {
                const tags = JSON.parse(
                    window.sessionStorage.getItem("markedTenantReads") ?? "[]",
                ) as (string | null)[];
                tags.push(new Headers(init?.headers).get("X-Session-Tag"));
                window.sessionStorage.setItem(
                    "markedTenantReads",
                    JSON.stringify(tags),
                );
            }
            return send(input, init);
        };
    });
}

export function isMarked(page: Page) {
    return page.evaluate(() => "documentMarker" in window);
}

export function markedTenantReads(page: Page) {
    return page.evaluate(
        () =>
            JSON.parse(
                window.sessionStorage.getItem("markedTenantReads") ?? "[]",
            ) as (string | null)[],
    );
}

export function navigationType(page: Page) {
    return page.evaluate(
        () =>
            (
                performance.getEntriesByType(
                    "navigation",
                )[0] as PerformanceNavigationTiming
            ).type,
    );
}

export function pendingInvitation(page: Page) {
    return page.evaluate(() =>
        window.sessionStorage.getItem("pyracast.workspaceInvitation"),
    );
}

export function focusWindow(page: Page) {
    return page.evaluate(() => window.dispatchEvent(new Event("focus")));
}

export async function tagObserver(page: Page) {
    const other = await page.context().newPage();
    await other.route("**/blank", (route) =>
        route.fulfill({ contentType: "text/html", body: "<html></html>" }),
    );
    await other.goto("/blank");
    return (tag: string) =>
        other.evaluate(
            (value) =>
                window.localStorage.setItem("pyracast.sessionTag", value),
            tag,
        );
}
