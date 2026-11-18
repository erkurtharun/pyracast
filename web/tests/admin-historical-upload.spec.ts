import { expect, test } from "@playwright/test";
import { readFile } from "node:fs/promises";
import { createServer } from "node:http";
import path from "node:path";

for (const explicitAction of [true, false]) {
    test(`historical preview and save post to the page with ${explicitAction ? "explicit" : "default"} action despite named action buttons`, async ({
        page,
    }) => {
        const endpoint = "/admin/plants/7/historical-import/";
        const script = await readFile(
            path.resolve(
                "../src/pyracast/regions/static/pyracast/admin-historical.js",
            ),
        );
        const requests: { pathname: string; body: string }[] = [];
        const server = createServer(async (request, response) => {
            if (request.method === "POST") {
                let body = "";
                for await (const chunk of request) body += chunk.toString();
                requests.push({ pathname: request.url!, body });
                if (request.url !== endpoint) {
                    response.writeHead(404, { "Content-Type": "text/html" });
                    response.end("<h1>Page not found</h1>");
                    return;
                }
                response.writeHead(200, { "Content-Type": "application/json" });
                response.end(
                    JSON.stringify(
                        body.includes('name="action"\r\n\r\nsave')
                            ? { saved: true, interval_count: 1 }
                            : {
                                  html: "<h2>File preview</h2>",
                                  token: "preview-token",
                                  can_save: true,
                              },
                    ),
                );
                return;
            }
            if (request.url === "/admin-historical.js") {
                response.setHeader("Content-Type", "application/javascript");
                response.end(script);
                return;
            }
            if (request.url === endpoint) {
                response.setHeader("Content-Type", "text/html");
                response.end(`<!doctype html><html lang="en"><title>Upload</title>
              <form ${explicitAction ? `action="${endpoint}"` : ""} method="post" enctype="multipart/form-data" data-historical-upload
                data-network-error="Request failed" data-saved="Historical data saved"
                data-checking="Checking" data-saving="Saving" data-intervals="Intervals">
                <div data-upload-drop><input type="file" name="file" data-upload-file required></div>
                <input name="source_timezone"><select name="period"><option value="">Read from file</option></select>
                <button type="submit" name="action" value="preview" data-preview-button>Preview</button>
                <div data-preview-result></div><div data-save-controls hidden>
                  <input type="hidden" name="preview_token" data-preview-token>
                  <input type="checkbox" name="settings_confirmed">
                  <button type="submit" name="action" value="save" data-save-button>Save</button>
                </div><p role="status" data-upload-status></p>
              </form><script src="/admin-historical.js" defer></script></html>`);
                return;
            }
            response.writeHead(404);
            response.end();
        });
        await new Promise<void>((resolve) =>
            server.listen(0, "127.0.0.1", resolve),
        );
        const address = server.address();
        if (!address || typeof address === "string")
            throw new Error("Missing local HTTP test address");
        try {
            await page.goto(`http://127.0.0.1:${address.port}${endpoint}`);
            await page.locator("[data-upload-file]").setInputFiles({
                name: "history.json",
                mimeType: "application/json",
                buffer: Buffer.from('{"estimated_actuals":[]}'),
            });
            expect(requests).toHaveLength(0);
            await page.locator("[data-preview-button]").click();
            await expect.poll(() => requests.length).toBe(1);
            expect(requests[0]?.pathname).toBe(endpoint);
            expect(requests[0]?.body).toContain('name="action"\r\n\r\npreview');
            await expect(
                page.getByRole("heading", { name: "File preview" }),
            ).toBeVisible();
            await page.locator('[name="settings_confirmed"]').check();
            await page.locator("[data-save-button]").click();
            await expect.poll(() => requests.length).toBe(2);
            expect(requests[1]?.pathname).toBe(endpoint);
            expect(requests[1]?.body).toContain('name="action"\r\n\r\nsave');
            expect(requests[1]?.body).toContain('filename="history.json"');
            expect(requests[1]?.body).toContain(
                'name="preview_token"\r\n\r\npreview-token',
            );
            await expect(page.getByRole("status")).toHaveText(
                "Historical data saved Intervals: 1.",
            );
        } finally {
            await page.goto("about:blank");
            server.closeAllConnections();
            await new Promise<void>((resolve, reject) =>
                server.close((error) => (error ? reject(error) : resolve())),
            );
        }
    });
}
