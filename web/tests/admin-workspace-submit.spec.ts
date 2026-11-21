import { expect, test } from "@playwright/test";
import { readFile } from "node:fs/promises";
import { createServer, type Server } from "node:http";
import path from "node:path";

async function serveConsolePage(page: string) {
    const script = await readFile(
        path.resolve(
            "../src/pyracast/regions/static/pyracast/admin-workspace.js",
        ),
    );
    const posts: string[] = [];
    const server = createServer(
        (request, response) =>
            void (async () => {
                if (request.method === "POST") {
                    let body = "";
                    for await (const chunk of request) body += chunk.toString();
                    posts.push(body);
                    await new Promise((resolve) => setTimeout(resolve, 500));
                    response.writeHead(303, { Location: "/admin/plants/7/" });
                    response.end();
                    return;
                }
                if (request.url === "/admin-workspace.js") {
                    response.setHeader(
                        "Content-Type",
                        "application/javascript",
                    );
                    response.end(script);
                    return;
                }
                response.setHeader("Content-Type", "text/html");
                response.end(page);
            })(),
    );
    await new Promise<void>((resolve) =>
        server.listen(0, "127.0.0.1", resolve),
    );
    const address = server.address();
    if (!address || typeof address === "string") throw new Error("No port");
    return { server, posts, origin: `http://127.0.0.1:${address.port}` };
}

function close(server: Server) {
    return new Promise((resolve) => server.close(resolve));
}

test("a console form is sent once and keeps the pressed action", async ({
    page,
}) => {
    const { server, posts, origin } =
        await serveConsolePage(`<!doctype html><html lang="en"><title>Plant</title>
          <form method="post" action="/admin/plants/7/">
            <input type="hidden" name="binding_id" value="3">
            <button type="submit" name="action" value="validate">Validate connection</button>
          </form><script src="/admin-workspace.js" defer></script></html>`);
    try {
        await page.goto(`${origin}/admin/plants/7/`);
        const validate = page.getByRole("button", {
            name: "Validate connection",
        });
        await validate.click();
        await validate.click({ force: true, noWaitAfter: true });
        await page.waitForURL(`${origin}/admin/plants/7/`);
        await page.waitForLoadState("load");
        expect(posts).toEqual(["binding_id=3&action=validate"]);
    } finally {
        await close(server);
    }
});

test("a form its own script sends is never blocked by the console guard", async ({
    page,
}) => {
    const { server, origin } =
        await serveConsolePage(`<!doctype html><html lang="en"><title>Upload</title>
          <form method="post" data-own><button type="submit">Preview</button></form>
          <output></output>
          <script>
            let sent = 0;
            document.querySelector("[data-own]").addEventListener("submit", (event) => {
              event.preventDefault();
              document.querySelector("output").textContent = String(++sent);
            });
          </script><script src="/admin-workspace.js" defer></script></html>`);
    try {
        await page.goto(`${origin}/admin/plants/7/historical-import/`);
        const preview = page.getByRole("button", { name: "Preview" });
        await preview.click();
        await preview.click();
        await expect(page.locator("output")).toHaveText("2");
    } finally {
        await close(server);
    }
});
