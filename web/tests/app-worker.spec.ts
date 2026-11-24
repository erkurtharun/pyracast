import { readdirSync, readFileSync } from "node:fs";
import path from "node:path";
import { expect, test } from "@playwright/test";
import { loadScript, SCOPE } from "./support/worker-script";
import { workerWithdrawn } from "./support/worker-state";

const NOTICE_POLICY =
    "default-src 'none'; style-src 'unsafe-inline'; form-action 'none'; base-uri 'none'; frame-ancestors 'none'";
const SOURCE = path.resolve(__dirname, "../src");
const REGISTERING = workerWithdrawn
    ? { mentions: [], importers: [] }
    : {
          mentions: ["lib/worker-registration.tsx"],
          importers: ["app/app/layout.tsx"],
      };

const loadWorker = (options?: { preload?: boolean }) =>
    loadScript("../public/app/sw.js", options);
const loadWithdrawal = () => loadScript("../testing/sw-withdraw.js");

async function noticeOf(response: Response | undefined) {
    expect(response).toBeDefined();
    return { response: response!, body: await response!.text() };
}

if (!workerWithdrawn)
    test.describe("the app worker at its address", () => {
        test("the app worker listens for install, activate and fetch", () => {
            expect([...loadWorker().listeners.keys()].sort()).toEqual([
                "activate",
                "fetch",
                "install",
            ]);
        });

        test("the app worker asks to skip waiting on install and does nothing else", async () => {
            const worker = loadWorker();
            await worker.dispatch("install");
            expect(worker.calls).toEqual(["skipWaiting"]);
        });

        test("the app worker enables navigation preload on activate and claims no window", async () => {
            const worker = loadWorker();
            await worker.dispatch("activate");
            expect(worker.calls).toEqual(["navigationPreload.enable"]);
        });

        test("the app worker activates without navigation preload where the browser has none", async () => {
            const worker = loadWorker({ preload: false });
            expect(await worker.dispatch("activate")).toBe(0);
            expect(worker.calls).toEqual([]);
        });

        test("a navigation is answered by its preload answer unchanged", async () => {
            const worker = loadWorker();
            const preloaded = new Response("maintenance", { status: 503 });
            const { answer } = await worker.navigate({
                preloadResponse: Promise.resolve(preloaded),
            });
            expect(answer).toBe(preloaded);
            expect(worker.fetched).toEqual([]);
        });

        test("a navigation without a preload answer is fetched and passed on unchanged", async () => {
            const worker = loadWorker();
            const served = new Response("not found", { status: 404 });
            worker.answerWith(() => Promise.resolve(served));
            const { request, answer } = await worker.navigate();
            expect(answer).toBe(served);
            expect(worker.fetched).toEqual([request]);
        });

        test("a navigation whose preload answer rejects gets the notice", async () => {
            const worker = loadWorker();
            const { response } = await noticeOf(
                (
                    await worker.navigate({
                        preloadResponse: Promise.reject(
                            new TypeError("offline"),
                        ),
                    })
                ).answer,
            );
            expect(response.status).toBe(503);
        });

        test("a navigation whose fetch rejects gets the notice", async () => {
            const worker = loadWorker();
            const { response } = await noticeOf(
                (await worker.navigate()).answer,
            );
            expect(response.status).toBe(503);
        });

        test("the worker leaves other requests to the browser", async () => {
            const worker = loadWorker();
            for (const navigation of [
                { mode: "cors" },
                { mode: "no-cors", url: `${SCOPE}icons/any-192.png` },
                { method: "POST" },
                { url: "https://pyracast.test/" },
                { url: "https://pyracast.test/tr/" },
                { url: "https://pyracast.test/admin/" },
                { url: "https://pyracast.test/api/session/" },
                { url: "https://pyracast.test/application/" },
            ])
                expect(
                    (await worker.navigate(navigation)).answer,
                    JSON.stringify(navigation),
                ).toBeUndefined();
            expect(worker.fetched).toEqual([]);
        });

        test("the notice is an uncached 503 page under its own policy that does not reload", async () => {
            const worker = loadWorker();
            const { response, body } = await noticeOf(
                (await worker.navigate()).answer,
            );
            expect(response.status).toBe(503);
            expect(response.headers.get("content-type")).toBe(
                "text/html; charset=utf-8",
            );
            expect(response.headers.get("cache-control")).toBe("no-store");
            expect(response.headers.get("content-security-policy")).toBe(
                NOTICE_POLICY,
            );
            expect(response.headers.get("refresh")).toBeNull();
            expect(body).not.toMatch(
                /<script|<meta[^>]+http-equiv|<form|<iframe/i,
            );
            expect(body).not.toMatch(/\son[a-z]+=/i);
        });

        test("the notice says the server cannot be reached in both languages with a link to the same address", async () => {
            const worker = loadWorker();
            const { body } = await noticeOf((await worker.navigate()).answer);
            expect(body).toMatch(/<html lang="en">/);
            expect(body).toContain('<section lang="en">');
            expect(body).toContain('<section lang="tr">');
            expect(body).toContain("Pyracast cannot be reached");
            expect(body).toContain("Pyracast'e ulaşılamıyor");
            expect(body).not.toMatch(/offline|çevrimdışı/i);
            expect(body).toContain(
                "<p>If you came here from an invitation link, open that link again from the email.</p>",
            );
            expect(body).toContain(
                "<p>Buraya bir davet bağlantısıyla geldiyseniz bağlantıyı e-postadan yeniden açın.</p>",
            );
            const links = [...body.matchAll(/<a\s([^>]*)>([^<]+)<\/a>/g)].map(
                ([, attributes, text]) => [attributes, text],
            );
            expect(links).toEqual([
                ['href=""', "Try again"],
                ['href=""', "Tekrar dene"],
            ]);
        });
    });

test("the withdrawal script asks to skip waiting on install and does nothing else", async () => {
    const withdrawal = loadWithdrawal();
    await withdrawal.dispatch("install");
    expect(withdrawal.calls).toEqual(["skipWaiting"]);
});

test("the withdrawal script unregisters on activate, claims nothing and navigates no window", async () => {
    const withdrawal = loadWithdrawal();
    expect([...withdrawal.listeners.keys()].sort()).toEqual([
        "activate",
        "install",
    ]);
    expect(await withdrawal.dispatch("activate")).toBe(1);
    expect(withdrawal.calls).toEqual(["unregister"]);
});

function sourcesContaining(text: string) {
    return readdirSync(SOURCE, { recursive: true })
        .map(String)
        .filter((file) => /\.(?:ts|tsx)$/.test(file))
        .filter((file) =>
            readFileSync(path.join(SOURCE, file), "utf8").includes(text),
        )
        .map((file) => file.split(path.sep).join("/"));
}

test("only the registering module mentions a worker and only the app layout mounts it", () => {
    expect({
        mentions: sourcesContaining("serviceWorker"),
        importers: sourcesContaining("@/lib/worker-registration"),
    }).toEqual(REGISTERING);
});
