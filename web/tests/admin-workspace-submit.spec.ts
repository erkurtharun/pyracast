import { expect, test } from "./support/test";
import { readFile } from "node:fs/promises";
import { createServer, type Server } from "node:http";
import path from "node:path";
import { coordinatePastes } from "./support/coordinate-pastes";

async function serveConsolePage(page: string) {
    const script = await readFile(
        path.resolve(
            "../src/pyracast/regions/static/pyracast/admin-workspace.js",
        ),
    );
    const posts: string[] = [];
    const firstPost = Promise.withResolvers<void>();
    const answers = Promise.withResolvers<void>();
    const server = createServer(
        (request, response) =>
            void (async () => {
                if (request.method === "POST") {
                    let body = "";
                    for await (const chunk of request) body += chunk.toString();
                    posts.push(body);
                    firstPost.resolve();
                    await answers.promise;
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
    return {
        server,
        posts,
        firstPost: firstPost.promise,
        answerPosts: answers.resolve,
        origin: `http://127.0.0.1:${address.port}`,
    };
}

function close(server: Server) {
    return new Promise((resolve) => server.close(resolve));
}

test("a console form is sent once and keeps the pressed action", async ({
    page,
}) => {
    const { server, posts, firstPost, answerPosts, origin } =
        await serveConsolePage(`<!doctype html><html lang="en"><title>Plant</title>
          <form method="post" action="/admin/plants/7/">
            <input type="hidden" name="binding_id" value="3">
            <button type="submit" name="action" value="validate">Validate connection</button>
          </form><script src="/admin-workspace.js" defer></script></html>`);
    try {
        const secondSubmit = Promise.withResolvers<boolean>();
        await page.exposeFunction("reportSecondSubmit", secondSubmit.resolve);
        await page.goto(`${origin}/admin/plants/7/`);
        const validate = page.getByRole("button", {
            name: "Validate connection",
        });
        const box = await validate.boundingBox();
        if (!box) throw new Error("No button box");
        // Registered after the deferred script's listener, so it sees the guard's decision;
        // page.evaluate cannot run once the held navigation is pending.
        await page.evaluate(() => {
            const { reportSecondSubmit } = window as unknown as {
                reportSecondSubmit: (prevented: boolean) => void;
            };
            let submits = 0;
            document.addEventListener("submit", (event) => {
                if (++submits === 2) reportSecondSubmit(event.defaultPrevented);
            });
        });
        await validate.click({ noWaitAfter: true });
        await firstPost;
        await page.mouse.click(box.x + box.width / 2, box.y + box.height / 2);
        expect(await secondSubmit.promise).toBe(true);
        const redirected = page.waitForEvent("load");
        answerPosts();
        await redirected;
        expect(posts).toEqual(["binding_id=3&action=validate"]);
    } finally {
        answerPosts();
        await close(server);
    }
});

test("a form its own script sends is never blocked by the console guard", async ({
    page,
}) => {
    const { server, answerPosts, origin } =
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
        answerPosts();
        await close(server);
    }
});

test("a console latitude fills both fields from a pasted text only when it is a pair", async ({
    page,
}) => {
    const { server, answerPosts, origin } =
        await serveConsolePage(`<!doctype html><html lang="en"><title>Location</title>
          <form method="post">
            <label>Latitude <input name="latitude" data-coordinate-pair="longitude"></label>
            <label>Longitude <input name="longitude"></label>
          </form><script src="/admin-workspace.js" defer></script></html>`);
    try {
        await page.goto(`${origin}/admin/plants/7/details/`);
        const latitude = page.getByLabel("Latitude");
        const longitude = page.getByLabel("Longitude");
        const paste = (text: string) =>
            latitude.evaluate((element, text) => {
                const data = new DataTransfer();
                data.setData("text/plain", text);
                const event = new ClipboardEvent("paste", {
                    clipboardData: data,
                    bubbles: true,
                    cancelable: true,
                });
                element.dispatchEvent(event);
                return event.defaultPrevented;
            }, text);
        for (const entry of coordinatePastes) {
            await latitude.fill("");
            await longitude.fill("");
            expect(await paste(entry.text), entry.text).toBe("pair" in entry);
            await expect(latitude).toHaveValue(
                "pair" in entry ? entry.pair[0] : "",
            );
            await expect(longitude).toHaveValue(
                "pair" in entry ? entry.pair[1] : "",
            );
        }
    } finally {
        answerPosts();
        await close(server);
    }
});
