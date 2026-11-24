import { expect, test } from "@playwright/test";
import {
    defaultScheduler,
    focusManager,
    notifyManager,
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
    discard,
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

test.beforeEach(() => {
    notifyManager.setScheduler((callback) => callback());
});

test.afterEach(() => {
    globalThis.fetch = realFetch;
    notifyManager.setScheduler(defaultScheduler);
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
const workspaceChange = (client: QueryClient) =>
    readAgain(client, changed(keys.workspace("1:a", 5)));

type Fetched = { value: string; requests: number };

function heldReads() {
    const reads: {
        resolve: (rows: string) => void;
        reject: (error: unknown) => void;
        answer: ReturnType<ReturnType<typeof answered<string>>>;
    }[] = [];
    const waiting: { count: number; resolve: () => void }[] = [];
    const sent = (count: number) =>
        reads.length >= count
            ? Promise.resolve()
            : new Promise<void>((resolve) => waiting.push({ count, resolve }));
    const read = async (index: number) => {
        await sent(index + 1);
        return reads[index]!;
    };
    return {
        queryFn: () => {
            let settle!: Pick<(typeof reads)[number], "resolve" | "reject">;
            const pending = new Promise<Fetched>((resolve, reject) => {
                settle = {
                    resolve: (rows) => resolve({ value: rows, requests: 1 }),
                    reject,
                };
            });
            const answer = answered(() => pending)();
            reads.push({ ...settle, answer });
            for (const waiter of waiting)
                if (reads.length >= waiter.count) waiter.resolve();
            return answer;
        },
        sent,
        read,
        resolve: async (index: number, rows: string) =>
            (await read(index)).resolve(rows),
        reject: async (index: number, error: unknown) =>
            (await read(index)).reject(error),
        count: () => reads.length,
    };
}

type Server = ReturnType<typeof heldReads>;
type Result = Parameters<typeof shownAnswer<string>>[0];
type Shown = ReturnType<typeof shownAnswer<string>>;

const same = (left: Shown, right: Shown) =>
    JSON.stringify(left) === JSON.stringify(right);

function watch(client: QueryClient, server: Server, queryKey = sitesKey) {
    const observer = new QueryObserver(client, {
        queryKey,
        queryFn: server.queryFn,
    });
    const delivered: Shown[] = [];
    const waiting: { expected: Shown; resolve: () => void }[] = [];
    const stop = observer.subscribe((result) => {
        const shown = shownAnswer(result as Result);
        delivered.push(shown);
        for (const waiter of waiting)
            if (same(shown, waiter.expected)) waiter.resolve();
    });
    const now = () => shownAnswer(observer.getCurrentResult() as Result);
    return {
        observer,
        stop,
        now,
        retry: () => observer.refetch({ cancelRefetch: false }),
        shows: (expected: Shown) =>
            same(now(), expected)
                ? Promise.resolve()
                : new Promise<void>((resolve) =>
                      waiting.push({ expected, resolve }),
                  ),
        history: () =>
            delivered.filter(
                (shown, index) =>
                    index === 0 || !same(shown, delivered[index - 1]!),
            ),
    };
}

const refusal = (status: number, message = "Refused.") =>
    new ApiError(message, { status });
const offline = () => new ApiError("Unreachable.", { status: 0 });
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
    await server.resolve(0, "rows");
    await shown.shows(rows("rows"));
    return shown;
}

function stored(client: QueryClient, queryKey: QueryKey) {
    return client.getQueryCache().find({ queryKey, exact: true })!;
}

test("no delivery carries rows after a refusal, through failed reads, a Retry and a language change, until an authorized read answers", async () => {
    const client = createQueryClient();
    const server = heldReads();
    const shown = await shownWithRows(client, server);
    try {
        const refusedRetry = shown.retry();
        await server.reject(1, refusal(403));
        await refusedRetry;
        expect(shown.now()).toEqual(refused());
        const lostRetry = shown.retry();
        await server.reject(2, offline());
        await lostRetry;
        expect(shown.now()).toEqual(refused("Unreachable."));
        const languageRead = readAgain(client);
        await server.reject(3, offline());
        await languageRead;
        expect(shown.now()).toEqual(refused("Unreachable."));
        const allowedRetry = shown.retry();
        await server.resolve(4, "rows allowed again");
        await allowedRetry;
        expect(shown.now()).toEqual(rows("rows allowed again"));
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
        const lostRetry = shown.retry();
        await server.reject(1, offline());
        await lostRetry;
        expect(shown.now()).toEqual(rows("rows", "Unreachable."));
        focusManager.setFocused(false);
        focusManager.setFocused(true);
        await server.sent(3);
        const retry = shown.retry();
        await server.reject(2, refusal(403));
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
    const unshown = stored(client, unshownKey);
    try {
        void populated.retry();
        await populatedServer.sent(2);
        await initialServer.sent(1);
        void unshown
            .fetch({ queryKey: unshownKey, queryFn: unshownServer.queryFn })
            .catch(() => undefined);
        await unshownServer.sent(1);
        expect(unshown.state.fetchStatus).toBe("fetching");

        const refreshed = workspaceChange(client);
        await populatedServer.resolve(1, "rows sent before the change");
        await initialServer.resolve(0, "rows sent before the change");
        await unshownServer.resolve(0, "rows sent before the change");
        await populatedServer.resolve(2, "rows after the change");
        await initialServer.resolve(1, "rows after the change");
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
        expect(unshownServer.count()).toBe(1);
    } finally {
        populated.stop();
        initial.stop();
    }
});

test("a first read whose answer is accepted as a change is answered still ends with a read sent after the change", async () => {
    const client = createQueryClient();
    const server = heldReads();
    const shown = watch(client, server);
    try {
        const before = await server.read(0);
        const refreshed = before.answer.then(() => workspaceChange(client));
        before.resolve("rows sent before the change");
        await Promise.race([refreshed, server.sent(2)]);
        expect(server.count()).toBe(2);
        await server.resolve(1, "rows after the change");
        await refreshed;
        expect(shown.now()).toEqual(rows("rows after the change"));
        expect(shown.history()).toEqual([
            pending,
            rows("rows sent before the change"),
            rows("rows after the change"),
        ]);
    } finally {
        shown.stop();
    }
});

test("an unshown read whose answer is accepted as a change is answered stays invalidated", async () => {
    const client = createQueryClient();
    const server = heldReads();
    const unshownKey = keys.request("1:a", sites, {
        path: { tenant_id: 5 },
        query: { page: 3 },
    });
    client.setQueryData(unshownKey, { value: "unshown rows", requests: 1 });
    const unshown = stored(client, unshownKey);
    const fetched = unshown
        .fetch({ queryKey: unshownKey, queryFn: server.queryFn })
        .catch(() => undefined);
    const before = await server.read(0);
    const refreshed = before.answer.then(() => workspaceChange(client));
    before.resolve("rows sent before the change");
    await refreshed;
    await fetched;
    expect(unshown.state.isInvalidated).toBe(true);
    expect(unshown.state.fetchStatus).toBe("idle");
    expect(unshown.state.data).toEqual({
        value: "rows sent before the change",
        requests: 1,
    });
    expect(server.count()).toBe(1);
});

test("a shown read whose answer is accepted as a change is answered is replaced by a read sent after the change", async () => {
    const client = createQueryClient();
    const server = heldReads();
    const shown = await shownWithRows(client, server);
    try {
        void shown.observer.refetch();
        const before = await server.read(1);
        const refreshed = before.answer.then(() => workspaceChange(client));
        before.resolve("rows sent before the change");
        await server.resolve(2, "rows after the change");
        await refreshed;
        expect(server.count()).toBe(3);
        expect(shown.history()).toEqual([
            pending,
            rows("rows"),
            rows("rows sent before the change"),
            rows("rows after the change"),
        ]);
    } finally {
        shown.stop();
    }
});

test("a change's read cancels a held read whose answer would be a refusal; when the read after the change fails the earlier rows stay with the failure", async () => {
    const client = createQueryClient();
    const server = heldReads();
    const shown = await shownWithRows(client, server);
    const answeredAt = shown.observer.getCurrentResult().dataUpdatedAt;
    try {
        void shown.retry();
        await server.sent(2);
        const refreshed = workspaceChange(client);
        await server.sent(3);
        await server.reject(1, refusal(403));
        await server.reject(2, offline());
        await refreshed;
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
            if (answer === "rows") await server.resolve(1, "newer rows");
            else if (answer === "refusal") await server.reject(1, refusal(403));
            else await server.reject(1, offline());
            expect(await reloaded).toEqual(expected);
            expect(server.count()).toBe(2);
        } finally {
            shown.stop();
        }
    });

test("a reload whose read is cancelled and reverted rejects while the earlier rows stay", async () => {
    const client = createQueryClient();
    const server = heldReads();
    const shown = await shownWithRows(client, server);
    try {
        const reloaded = outcome(reload<string>(client, sitesKey));
        await server.sent(2);
        await client.cancelQueries({ queryKey: sitesKey, exact: true });
        expect(await reloaded).toEqual({ status: 0 });
        expect(stored(client, sitesKey).state).toMatchObject({
            data: { value: "rows", requests: 1 },
            dataUpdateCount: 1,
        });
    } finally {
        shown.stop();
    }
});

test("a reload resolves with an answer written in the same millisecond as the one before it", async () => {
    const realNow = Date.now;
    const instant = realNow();
    Date.now = () => instant;
    const client = createQueryClient();
    const server = heldReads();
    try {
        const shown = await shownWithRows(client, server);
        try {
            const reloaded = outcome(reload<string>(client, sitesKey));
            await server.resolve(1, "newer rows");
            expect(await reloaded).toEqual({ value: "newer rows" });
            expect(stored(client, sitesKey).state).toMatchObject({
                dataUpdatedAt: instant,
                dataUpdateCount: 2,
            });
        } finally {
            shown.stop();
        }
    } finally {
        Date.now = realNow;
    }
});

test("a reload rejects when its query is removed while it reads or was never read", async () => {
    const client = createQueryClient();
    const server = heldReads();
    const shown = await shownWithRows(client, server);
    try {
        const reloaded = outcome(reload<string>(client, sitesKey));
        await server.sent(2);
        client.removeQueries({ queryKey: sitesKey, exact: true });
        await server.resolve(1, "rows of a removed query");
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
        await server.sent(2);
        shown.observer.setOptions({
            queryKey: otherKey,
            queryFn: server.queryFn,
        });
        await server.resolve(2, "rows of the shown key");
        await server.resolve(1, "rows of the left key");
        expect(await reloaded).toEqual({ value: "rows of the left key" });
        await shown.shows(rows("rows of the shown key"));
    } finally {
        shown.stop();
    }
});

test("a discarded entry loses its rows at once, never stores the read that was running and is read again", async () => {
    const client = createQueryClient();
    const server = heldReads();
    const shown = await shownWithRows(client, server);
    try {
        void client.refetchQueries({ queryKey: sitesKey, exact: true });
        await server.sent(2);
        discard(client, sitesKey);
        expect(client.getQueryData(sitesKey)).toBeUndefined();
        expect(shown.now()).toEqual(pending);
        await server.sent(3);
        await server.resolve(1, "rows read before the discard");
        expect(stored(client, sitesKey).state.data).toBeUndefined();
        expect(shown.now()).toEqual(pending);
        await server.resolve(2, "rows read after the discard");
        await shown.shows(rows("rows read after the discard"));
        expect(shown.history()).toEqual([
            pending,
            rows("rows"),
            pending,
            rows("rows read after the discard"),
        ]);
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
