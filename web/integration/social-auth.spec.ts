import { expect, test } from "@playwright/test";
import { createServer, type Server } from "node:http";
import type { AddressInfo } from "node:net";

let identityOrigin: string;
let federationOrigin: string;
const identityServer = createServer((_request, response) => {
    response.writeHead(200, { "Content-Type": "text/html" });
    response.end("<h1>Provider sign-in</h1>");
});
const federationServer = createServer((request, response) => {
    response.writeHead(302, {
        Location: `${identityOrigin}${request.url}`,
    });
    response.end();
});

async function listen(server: Server): Promise<string> {
    await new Promise<void>((resolve) =>
        server.listen(0, "127.0.0.1", resolve),
    );
    return `http://127.0.0.1:${(server.address() as AddressInfo).port}`;
}

test.beforeAll(async () => {
    identityOrigin = await listen(identityServer);
    federationOrigin = await listen(federationServer);
});

test.afterAll(async () => {
    await identityServer[Symbol.asyncDispose]();
    await federationServer[Symbol.asyncDispose]();
});

const providers = [
    {
        id: "google",
        origin: "https://accounts.google.com",
        login: "Continue with Google",
        connect: "Connect Google",
    },
    {
        id: "microsoft",
        origin: "https://login.microsoftonline.com",
        login: "Sign in with Microsoft",
        connect: "Connect Microsoft",
    },
] as const;

for (const provider of providers) {
    for (const flow of ["login", "connect"] as const) {
        test(`${provider.id} ${flow} reaches a federated provider with strict form CSP`, async ({
            page,
        }) => {
            const violations: string[] = [];
            page.on("console", (message) => {
                if (message.text().includes("form-action"))
                    violations.push(message.text());
            });
            const destination = `${identityOrigin}/${provider.id}/signin`;
            await page.route(`${provider.origin}/**`, async (route) => {
                const authorization = new URL(route.request().url());
                expect(authorization.searchParams.get("response_type")).toBe(
                    "code",
                );
                expect(
                    authorization.searchParams.get("code_challenge_method"),
                ).toBe("S256");
                expect(authorization.searchParams.get("state")).toBeTruthy();
                await route.fulfill({
                    status: 302,
                    headers: {
                        location:
                            provider.id === "microsoft"
                                ? `${federationOrigin}/${provider.id}/signin`
                                : destination,
                    },
                });
            });
            const document = await page.goto("/");
            expect(document?.headers()["content-security-policy"]).toMatch(
                /(?:^|;\s*)form-action 'self'(?:;|$)/,
            );
            if (flow === "connect") {
                await page
                    .getByLabel("Email", { exact: true })
                    .fill(process.env.BROWSER_EMAIL!);
                await page
                    .getByLabel("Password", { exact: true })
                    .fill(process.env.BROWSER_PASSWORD!);
                await page
                    .locator("form")
                    .getByRole("button", { name: "Sign in", exact: true })
                    .click();
                await expect(
                    page.getByRole("heading", { name: "Plants", exact: true }),
                ).toBeVisible();
                await page.getByRole("button", { name: "Account" }).click();
                const accounts = page.getByRole("region", {
                    name: "Connected accounts",
                });
                await accounts
                    .getByRole("button", { name: "Connect an account" })
                    .click();
                await accounts
                    .getByLabel("Current password", { exact: true })
                    .fill(process.env.BROWSER_PASSWORD!);
                await accounts
                    .getByRole("button", { name: "Confirm password" })
                    .click();
            }

            await page
                .getByRole("button", { name: provider[flow], exact: true })
                .click();
            await expect(page).toHaveURL(destination);
            await expect(
                page.getByRole("heading", { name: "Provider sign-in" }),
            ).toBeVisible();
            expect(violations).toEqual([]);
        });
    }
}
