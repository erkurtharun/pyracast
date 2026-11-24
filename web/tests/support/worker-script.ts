import { readFileSync } from "node:fs";
import path from "node:path";
import { runInNewContext } from "node:vm";
import { expect } from "@playwright/test";
import type { SavedDatabase } from "./worker-database";

export const SCOPE = "https://pyracast.test/app/";

type Listener = (event: object) => void;

export type Navigation = {
    url?: string;
    method?: string;
    mode?: string;
    preloadResponse?: Promise<Response | undefined>;
};

export type ScriptOptions = {
    preload?: boolean;
    database?: SavedDatabase;
    now?: number | (() => number);
    monotonic?: () => number;
    zone?: string;
    setTimeout?: (run: () => void, delay: number) => unknown;
    clearTimeout?: (timer: unknown) => void;
};

function clockAt(now: number | (() => number)) {
    return class extends Date {
        static now() {
            return typeof now === "function" ? now() : now;
        }
    };
}

function deviceIntl(zone: string) {
    function DateTimeFormat(
        locale?: string | string[],
        options: Intl.DateTimeFormatOptions = {},
    ) {
        return new Intl.DateTimeFormat(locale, { timeZone: zone, ...options });
    }
    return { DateTimeFormat, NumberFormat: Intl.NumberFormat };
}

export function loadScript(file: string, options: ScriptOptions = {}) {
    const listeners = new Map<string, Listener>();
    const calls: string[] = [];
    const fetched: unknown[] = [];
    let network: () => Promise<Response> = () =>
        Promise.reject(new TypeError("Failed to fetch"));
    const record = (name: string) => () => {
        calls.push(name);
        return Promise.resolve(true);
    };
    const self = {
        addEventListener: (type: string, listener: Listener) =>
            listeners.set(type, listener),
        skipWaiting: record("skipWaiting"),
        registration: {
            scope: SCOPE,
            unregister: record("unregister"),
            update: record("update"),
            navigationPreload:
                options.preload === false
                    ? undefined
                    : { enable: record("navigationPreload.enable") },
        },
        clients: new Proxy(
            {},
            { get: (_, name) => record(`clients.${String(name)}`) },
        ),
    };
    runInNewContext(readFileSync(path.resolve(__dirname, "..", file), "utf8"), {
        self,
        URL,
        Response,
        setTimeout: options.setTimeout ?? setTimeout,
        clearTimeout: options.clearTimeout ?? clearTimeout,
        performance: { now: options.monotonic ?? (() => performance.now()) },
        fetch: (request: unknown) => {
            fetched.push(request);
            return network();
        },
        ...(options.database
            ? {
                  indexedDB: options.database.indexedDB,
                  IDBKeyRange: options.database.IDBKeyRange,
              }
            : {}),
        ...(options.now === undefined ? {} : { Date: clockAt(options.now) }),
        ...(options.zone ? { Intl: deviceIntl(options.zone) } : {}),
    });

    async function dispatch(type: string) {
        const waited: Promise<unknown>[] = [];
        listeners.get(type)?.({
            waitUntil: (work: Promise<unknown>) => waited.push(work),
        });
        await Promise.all(waited);
        return waited.length;
    }

    async function navigate(navigation: Navigation = {}) {
        const request = {
            url: navigation.url ?? `${SCOPE}w/1/`,
            method: navigation.method ?? "GET",
            mode: navigation.mode ?? "navigate",
        };
        const answers: Promise<Response>[] = [];
        listeners.get("fetch")?.({
            request,
            preloadResponse:
                navigation.preloadResponse ?? Promise.resolve(undefined),
            respondWith: (answer: Promise<Response>) => answers.push(answer),
        });
        expect(answers.length).toBeLessThanOrEqual(1);
        return { request, answer: answers[0] && (await answers[0]) };
    }

    return {
        listeners,
        calls,
        fetched,
        dispatch,
        navigate,
        answerWith: (answer: () => Promise<Response>) => {
            network = answer;
        },
    };
}
