import { expect, test } from "@playwright/test";
import { QueryClient, QueryObserver } from "@tanstack/react-query";
import { keys } from "../src/lib/api/keys";
import {
    fetchList,
    invalidate,
    pollInterval,
    sameRequest,
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
        keys.request("1:a", sites, { path: { tenant_id: 5 } }, "en"),
        keys.request("2:b", sites, { path: { tenant_id: 5 } }, "en"),
        keys.request("1:a", sites, { path: { tenant_id: 6 } }, "en"),
        keys.request("1:a", "/api/tenants/", {}, "en"),
        keys.request("2:b", "/api/tenants/", {}, "en"),
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

test("a request key carries its path parameters, query and language", () => {
    const scope = { tenant_id: 5, region_id: 11, plant_id: 21 };
    const day = (date: string, locale = "en") =>
        JSON.stringify(
            keys.request(
                "1:a",
                forecast,
                { path: scope, query: { date, data_source: "auto" } },
                locale,
            ),
        );
    expect(day("2026-11-20")).not.toBe(day("2026-11-21"));
    expect(day("2026-11-20")).not.toBe(day("2026-11-20", "tr"));
    expect(
        JSON.stringify(
            keys.request("1:a", forecast, { path: { ...scope, plant_id: 22 } }),
        ),
    ).not.toBe(JSON.stringify(keys.request("1:a", forecast, { path: scope })));
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

test("a request read in another language is the same request, one for another plant or day is not", () => {
    const scope = { tenant_id: 5, region_id: 11, plant_id: 21 };
    const read = (plant: number, date: string, locale: string) =>
        keys.request(
            "1:a",
            forecast,
            { path: { ...scope, plant_id: plant }, query: { date } },
            locale,
        );
    const english = read(21, "2026-11-20", "en");
    expect(sameRequest(english, read(21, "2026-11-20", "tr"))).toBe(true);
    expect(sameRequest(english, read(22, "2026-11-20", "tr"))).toBe(false);
    expect(sameRequest(english, read(21, "2026-11-21", "tr"))).toBe(false);
    expect(
        sameRequest(
            keys.request("1:a", sites, { path: { tenant_id: 5 } }, "en"),
            keys.request("2:b", sites, { path: { tenant_id: 5 } }, "tr"),
        ),
    ).toBe(false);
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
        "en",
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
        await invalidate(client, keys.workspace("1:a", 5), pageOne);
        expect(reads).toBe(1);
        await expect.poll(() => observer.getCurrentResult().data).toBe(1);
        await invalidate(client, keys.workspace("1:a", 5), pageOne);
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
