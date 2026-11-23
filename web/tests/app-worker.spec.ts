import { readdirSync, readFileSync } from "node:fs";
import path from "node:path";
import { runInNewContext } from "node:vm";
import { expect, test } from "@playwright/test";

type Listener = (event: {
    waitUntil: (work: Promise<unknown>) => void;
}) => void;

function loadWorker() {
    const listeners = new Map<string, Listener>();
    const calls: string[] = [];
    const record = (name: string) => () => {
        calls.push(name);
        return Promise.resolve(true);
    };
    const self = {
        addEventListener: (type: string, listener: Listener) =>
            listeners.set(type, listener),
        skipWaiting: record("skipWaiting"),
        registration: { unregister: record("unregister") },
        clients: new Proxy(
            {},
            {
                get: (_, name) => record(`clients.${String(name)}`),
            },
        ),
    };
    runInNewContext(
        readFileSync(path.resolve(__dirname, "../public/app/sw.js"), "utf8"),
        { self },
    );
    async function dispatch(type: string) {
        const waited: Promise<unknown>[] = [];
        listeners.get(type)?.({ waitUntil: (work) => waited.push(work) });
        await Promise.all(waited);
        return waited.length;
    }
    return { listeners, calls, dispatch };
}

test("the app worker listens only for install and activate", () => {
    expect([...loadWorker().listeners.keys()].sort()).toEqual([
        "activate",
        "install",
    ]);
});

test("the app worker asks to skip waiting on install and does nothing else", async () => {
    const worker = loadWorker();
    await worker.dispatch("install");
    expect(worker.calls).toEqual(["skipWaiting"]);
});

test("the app worker unregisters itself on activate without touching a window", async () => {
    const worker = loadWorker();
    expect(await worker.dispatch("activate")).toBe(1);
    expect(worker.calls).toEqual(["unregister"]);
});

test("no application source registers a worker", () => {
    const source = path.resolve(__dirname, "../src");
    const registering = readdirSync(source, { recursive: true })
        .map(String)
        .filter((file) => /\.(?:ts|tsx)$/.test(file))
        .filter((file) =>
            readFileSync(path.join(source, file), "utf8").includes(
                "serviceWorker",
            ),
        );
    expect(registering).toEqual([]);
});
