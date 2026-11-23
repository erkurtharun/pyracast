import { expect, test } from "@playwright/test";
import {
    focusManager,
    QueryClient,
    QueryObserver,
    type QueryKey,
} from "@tanstack/react-query";
import { ApiError } from "../src/lib/api/errors";
import { keys } from "../src/lib/api/keys";
import {
    answered,
    changed,
    createQueryClient,
    fetchList,
    pollInterval,
    readAgain,
    reload,
    shownAnswer,
} from "../src/lib/api/query";

const sites = "/api/tenants/{tenant_id}/sites/";
const forecast =
    "/api/tenants/{tenant_id}/regions/{region_id}/plants/{plant_id}/forecast/";
const realFetch = globalThis.fetch;

test.afterEach(() => {
    globalThis.fetch = realFetch;
});

function cached(client: QueryClient, key: readonly unknown[]) {
    return client
        .getQueriesData({ queryKey: key })
        .map(([queryKey]) => JSON.stringify(queryKey));
}

test("the same request of two accounts or two workspaces never shares a key", () => {
    const client = new QueryClient();
    const requests = [
        keys.request("1:a", sites, { path: { tenant_id: 5 } }),
        keys.request("2:b", sites, { path: { tenant_id: 5 } }),
        keys.request("1:a", sites, { path: { tenant_id: 6 } }),
        keys.request("1:a", "/api/tenants/", {}),
        keys.request("2:b", "/api/tenants/", {}),
    ];
    for (const key of requests) client.setQueryData(key, key);
    expect(new Set(requests.map((key) => JSON.stringify(key))).size).toBe(5);
    expect(cached(client, keys.owner("1:a"))).toEqual(
        [requests[0], requests[2], requests[3]].map((key) =>
            JSON.stringify(key),
        ),
    );
    expect(cached(client, keys.workspace("1:a", 5))).toEqual([
        JSON.stringify(requests[0]),
    ]);
    expect(cached(client, keys.workspace("2:b", 5))).toEqual([
        JSON.stringify(requests[1]),
    ]);
    expect(cached(client, keys.account("2:b"))).toEqual([
        JSON.stringify(requests[4]),
    ]);
});

test("a request key carries its path parameters and query and is the same in every language", () => {
    const scope = { tenant_id: 5, region_id: 11, plant_id: 21 };
    const day = (date: string) =>
        JSON.stringify(
            keys.request("1:a", forecast, {
                path: scope,
                query: { date, data_source: "auto" },
            }),
        );
    expect(day("2026-11-20")).not.toBe(day("2026-11-21"));
    expect(
        JSON.stringify(
            keys.request("1:a", forecast, { path: { ...scope, plant_id: 22 } }),
        ),
    ).not.toBe(JSON.stringify(keys.request("1:a", forecast, { path: scope })));
    expect(
        keys.request("1:a", sites, { path: { tenant_id: 5 } }).at(-1),
    ).toEqual({});
    const client = new QueryClient();
    client.setQueryData(
        keys.request("1:a", forecast, { path: scope, query: { date: "x" } }),
        1,
    );
    client.setQueryData(
        keys.request("1:a", sites, { path: { tenant_id: 5 } }),
        2,
    );
    expect(cached(client, keys.path("1:a", forecast, scope))).toHaveLength(1);
});

const sitesKey = keys.request("1:a", sites, { path: { tenant_id: 5 } });

// Reads of one request whose answers the test delivers, in any order.
function heldReads() {
    const reads: {
        resolve: (rows: string) => void;
        reject: (error: unknown) => void;
    }[] = [];
    return {
        queryFn: answered(
            () =>
                new Promise<{ value: string; requests: number }>(
                    (resolve, reject) =>
                        reads.push({
                            resolve: (rows) =>
                                resolve({ value: rows, requests: 1 }),
                            reject,
                        }),
                ),
        ),
        read: (index: number) => reads[index]!,
        count: () => reads.length,
    };
}

type Server = ReturnType<typeof heldReads>;
type Result = Parameters<typeof shownAnswer<string>>[0];

function watch(client: QueryClient, server: Server, queryKey = sitesKey) {
    const observer = new QueryObserver(client, {
        queryKey,
        queryFn: server.queryFn,
    });
    const delivered: ReturnType<typeof shownAnswer<string>>[] = [];
    const stop = observer.subscribe((result) =>
        delivered.push(shownAnswer(result as Result)),
    );
    return {
        observer,
        stop,
        now: () => shownAnswer(observer.getCurrentResult() as Result),
        retry: () => observer.refetch({ cancelRefetch: false }),
        // Consecutive equal results are one state on the screen.
        history: () =>
            delivered.filter(
                (shown, index) =>
                    index === 0 ||
                    JSON.stringify(shown) !==
                        JSON.stringify(delivered[index - 1]),
            ),
    };
}

const refusal = (status: number, message = "Refused.") =>
    new ApiError(message, { status });
const offline = () => new ApiError("Unreachable.", { status: 0 });
const settled = () => new Promise((resolve) => setTimeout(resolve, 0));
const rows = (value: string, error?: string) => ({
    data: value,
    error,
    denied: false,
});
const refused = (error = "Refused.") => ({
    data: undefined,
    error,
    denied: true,
});
const pending = { data: undefined, error: undefined, denied: false };

async function shownWithRows(client: QueryClient, server: Server) {
    const shown = watch(client, server);
    server.read(0).resolve("rows");
    await expect.poll(() => shown.now().data).toBe("rows");
    return shown;
}

test("no delivery carries rows after a refusal, through failed reads, a Retry and a language change, until an authorized read answers", async () => {
    const client = createQueryClient();
    const server = heldReads();
    const shown = await shownWithRows(client, server);
    try {
        void shown.retry();
        server.read(1).reject(refusal(403));
        await expect.poll(() => shown.now()).toEqual(refused());
        void shown.retry();
        server.read(2).reject(offline());
        await expect.poll(() => shown.now()).toEqual(refused("Unreachable."));
        void readAgain(client);
        await expect.poll(() => server.count()).toBe(4);
        server.read(3).reject(offline());
        await settled();
        expect(shown.now()).toEqual(refused("Unreachable."));
        void shown.retry();
        server.read(4).resolve("rows allowed again");
        await expect
            .poll(() => shown.now())
            .toEqual(rows("rows allowed again"));
        expect(shown.history()).toEqual([
            pending,
            rows("rows"),
            refused(),
            refused("Unreachable."),
            rows("rows allowed again"),
        ]);
    } finally {
        shown.stop();
    }
});

test("a Retry joins a running read, so that read's refusal is the answer shown", async () => {
    const client = createQueryClient();
    client.mount();
    const server = heldReads();
    const shown = await shownWithRows(client, server);
    try {
        void shown.retry();
        server.read(1).reject(offline());
        await expect
            .poll(() => shown.now())
            .toEqual(rows("rows", "Unreachable."));
        focusManager.setFocused(false);
        focusManager.setFocused(true);
        await expect.poll(() => server.count()).toBe(3);
        const retry = shown.retry();
        await settled();
        expect(server.count()).toBe(3);
        server.read(2).reject(refusal(403));
        await retry;
        expect(shown.now()).toEqual(refused());
        expect(server.count()).toBe(3);
    } finally {
        shown.stop();
        focusManager.setFocused(undefined);
        client.unmount();
    }
});

test("reading again after a change ends every shown query with a read sent after it and leaves an unshown one invalidated", async () => {
    const client = createQueryClient();
    const workspace = keys.workspace("1:a", 5);
    const initialKey = keys.request("1:a", sites, {
        path: { tenant_id: 5 },
        query: { page: 2 },
    });
    const unshownKey = keys.request("1:a", sites, {
        path: { tenant_id: 5 },
        query: { page: 3 },
    });
    const populatedServer = heldReads();
    const populated = await shownWithRows(client, populatedServer);
    const initialServer = heldReads();
    const initial = watch(client, initialServer, initialKey);
    const unshownServer = heldReads();
    client.setQueryData(unshownKey, { value: "unshown rows", requests: 1 });
    const unshown = client.getQueryCache().find({
        queryKey: unshownKey,
        exact: true,
    })!;
    try {
        void populated.retry();
        await expect.poll(() => initialServer.count()).toBe(1);
        void unshown
            .fetch({ queryKey: unshownKey, queryFn: unshownServer.queryFn })
            .catch(() => undefined);
        expect(populatedServer.count()).toBe(2);
        expect(unshown.state.fetchStatus).toBe("fetching");

        const refreshed = readAgain(client, changed(workspace));
        await settled();
        expect(populatedServer.count()).toBe(3);
        expect(initialServer.count()).toBe(2);
        expect(unshownServer.count()).toBe(1);
        populatedServer.read(1).resolve("rows sent before the change");
        initialServer.read(0).resolve("rows sent before the change");
        unshownServer.read(0).resolve("rows sent before the change");
        await settled();
        populatedServer.read(2).resolve("rows after the change");
        initialServer.read(1).resolve("rows after the change");
        await refreshed;

        expect(populated.history()).toEqual([
            pending,
            rows("rows"),
            rows("rows after the change"),
        ]);
        expect(initial.history()).toEqual([
            pending,
            rows("rows after the change"),
        ]);
        expect(unshown.state.isInvalidated).toBe(true);
        expect(unshown.state.fetchStatus).toBe("idle");
        expect(unshown.state.data).toEqual({
            value: "unshown rows",
            requests: 1,
        });
    } finally {
        populated.stop();
        initial.stop();
    }
});

test("a change's read cancels a held read whose answer would be a refusal; when the read after the change fails the earlier rows stay with the failure", async () => {
    const client = createQueryClient();
    const server = heldReads();
    const shown = await shownWithRows(client, server);
    const answeredAt = shown.observer.getCurrentResult().dataUpdatedAt;
    try {
        void shown.retry();
        const refreshed = readAgain(client, changed(keys.workspace("1:a", 5)));
        await settled();
        expect(server.count()).toBe(3);
        server.read(1).reject(refusal(403));
        server.read(2).reject(offline());
        await refreshed;
        await settled();
        expect(shown.now()).toEqual(rows("rows", "Unreachable."));
        expect(shown.observer.getCurrentResult().dataUpdatedAt).toBe(
            answeredAt,
        );
        expect(shown.history()).toEqual([
            pending,
            rows("rows"),
            rows("rows", "Unreachable."),
        ]);
    } finally {
        shown.stop();
    }
});

async function outcome(promise: Promise<unknown>) {
    return promise.then(
        (value) => ({ value }),
        (error: unknown) => ({
            status: error instanceof ApiError ? error.status : "other",
        }),
    );
}

for (const [answer, expected] of [
    ["rows", { value: "newer rows" }],
    ["refusal", { status: 403 }],
    ["failure", { status: 0 }],
] as const)
    test(`a reload resolves only with a value read after it started (${answer})`, async () => {
        const client = createQueryClient();
        const server = heldReads();
        const shown = await shownWithRows(client, server);
        try {
            const reloaded = outcome(reload<string>(client, sitesKey));
            await settled();
            expect(server.count()).toBe(2);
            if (answer === "rows") server.read(1).resolve("newer rows");
            else if (answer === "refusal") server.read(1).reject(refusal(403));
            else server.read(1).reject(offline());
            expect(await reloaded).toEqual(expected);
        } finally {
            shown.stop();
        }
    });

test("a reload rejects when its query is removed while it reads or was never read", async () => {
    const client = createQueryClient();
    const server = heldReads();
    const shown = await shownWithRows(client, server);
    try {
        const reloaded = outcome(reload<string>(client, sitesKey));
        await settled();
        client.removeQueries({ queryKey: sitesKey, exact: true });
        server.read(1).resolve("rows of a removed query");
        expect(await reloaded).toEqual({ status: "other" });
        const absent: QueryKey = keys.request("1:a", sites, {
            path: { tenant_id: 9 },
        });
        expect(await outcome(reload<string>(client, absent))).toEqual({
            status: 0,
        });
        expect(server.count()).toBe(2);
    } finally {
        shown.stop();
    }
});

test("a reload whose key the screen left meanwhile answers with that key's own read, never the shown key's", async () => {
    const client = createQueryClient();
    const server = heldReads();
    const shown = await shownWithRows(client, server);
    const otherKey = keys.request("1:a", sites, {
        path: { tenant_id: 5 },
        query: { page: 2 },
    });
    try {
        const reloaded = outcome(reload<string>(client, sitesKey));
        await settled();
        shown.observer.setOptions({
            queryKey: otherKey,
            queryFn: server.queryFn,
        });
        await expect.poll(() => server.count()).toBe(3);
        server.read(2).resolve("rows of the shown key");
        server.read(1).resolve("rows of the left key");
        expect(await reloaded).toEqual({ value: "rows of the left key" });
        await expect.poll(() => shown.now().data).toBe("rows of the shown key");
    } finally {
        shown.stop();
    }
});

function json(body: unknown, status = 200) {
    return new Response(JSON.stringify(body), {
        status,
        headers: { "Content-Type": "application/json" },
    });
}

function pages(...answers: unknown[]) {
    const urls: string[] = [];
    globalThis.fetch = (input) => {
        urls.push(input instanceof Request ? input.url : String(input));
        const answer = answers[urls.length - 1];
        if (answer === undefined)
            return Promise.reject(new Error("No page is prepared."));
        return Promise.resolve(json(answer));
    };
    return urls;
}

test("a list follows its pages and shows a row repeated on the next page once", async () => {
    const urls = pages(
        {
            count: 3,
            next: "/api/tenants/5/sites/?page=2",
            previous: null,
            results: [{ id: 1 }, { id: 2 }],
        },
        {
            count: 3,
            next: null,
            previous: "/api/tenants/5/sites/",
            results: [{ id: 2 }, { id: 3 }],
        },
    );
    const { value: list, requests } = await fetchList<{ id: number }>(
        "/api/tenants/5/sites/",
    );
    expect(urls).toEqual([
        "/api/tenants/5/sites/",
        "/api/tenants/5/sites/?page=2",
    ]);
    expect(requests).toBe(2);
    expect(list.results.map((row) => row.id)).toEqual([1, 2, 3]);
    expect(list.next).toBeNull();
});

test("a poll of a list over several pages waits that many periods", () => {
    expect(pollInterval(30_000)).toBe(30_000);
    expect(pollInterval(30_000, 1)).toBe(30_000);
    expect(pollInterval(30_000, 3)).toBe(90_000);
    expect(pollInterval(false, 3)).toBe(false);
});

test("a refresh after a change reuses the read a just-mounted page started and still refreshes an idle one", async () => {
    const client = new QueryClient({
        defaultOptions: { queries: { retry: false, staleTime: 2_000 } },
    });
    const pageOne = keys.request(
        "1:a",
        "/api/tenants/{tenant_id}/change-requests/",
        { path: { tenant_id: 5 }, query: { scope: "pending", page: 1 } },
    );
    let reads = 0;
    client.setQueryData(pageOne, 0, { updatedAt: 0 });
    const observer = new QueryObserver(client, {
        queryKey: pageOne,
        queryFn: async () => {
            reads++;
            await new Promise((resolve) => setTimeout(resolve, 10));
            return reads;
        },
    });
    const unsubscribe = observer.subscribe(() => {});
    try {
        await expect.poll(() => reads).toBe(1);
        await readAgain(client, changed(keys.workspace("1:a", 5), pageOne));
        expect(reads).toBe(1);
        await expect.poll(() => observer.getCurrentResult().data).toBe(1);
        await readAgain(client, changed(keys.workspace("1:a", 5), pageOne));
        expect(reads).toBe(2);
    } finally {
        unsubscribe();
    }
});

test("a list refuses a next page outside the API or one already read", async () => {
    pages({
        count: 1,
        next: "https://other.example/api/tenants/5/sites/?page=2",
        previous: null,
        results: [{ id: 1 }],
    });
    await expect(fetchList("/api/tenants/5/sites/")).rejects.toThrow();
    pages(
        {
            count: 2,
            next: "/api/tenants/5/sites/?page=2",
            previous: null,
            results: [{ id: 1 }],
        },
        {
            count: 2,
            next: "/api/tenants/5/sites/?page=2",
            previous: null,
            results: [{ id: 2 }],
        },
    );
    await expect(fetchList("/api/tenants/5/sites/")).rejects.toThrow();
});
