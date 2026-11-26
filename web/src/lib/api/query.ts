"use client";
import {
    partialMatchKey,
    QueryClient,
    useQuery,
    useQueryClient,
    type QueryCacheNotifyEvent,
    type QueryFilters,
    type QueryKey,
} from "@tanstack/react-query";
import { createContext, useContext, useMemo } from "react";
import { browserErrors } from "@/i18n/browser";
import {
    buildUrl,
    type Answer,
    type ListPath,
    type PathFor,
    type RequestOptions,
} from "./client";
import { ApiError, displayMessage, type ErrorCode } from "./errors";
import { keys } from "./keys";
import { send } from "./lane";

// Milliseconds between polls of a visible tab; a hidden tab never polls.
export const cadence = {
    live: 30_000,
    steady: 60_000,
    background: 120_000,
} as const;

// A list read over several pages polls that many times less often, so its request rate stays one per period.
export function pollInterval(poll: number | false, requests = 1) {
    return poll && poll * requests;
}

export function createQueryClient() {
    return new QueryClient({
        defaultOptions: {
            queries: {
                retry: false,
                staleTime: 2_000,
                // The library's default mode pauses every read after an offline event, so no read would report the failure.
                networkMode: "always",
                refetchOnReconnect: true,
            },
        },
    });
}

export const QueryOwner = createContext<string | undefined>(undefined);

type Page<T> = { results: T[]; next: string | null };
type Fetched<T> = { value: T; requests: number; sentAt?: number };
type Refusal = { status: number; code: ErrorCode | null; message: string };
export type Stored<T> = Fetched<T> | { refusal: Refusal };

export async function fetchList<T>(url: string): Promise<Fetched<Page<T>>> {
    const first = await send<Page<T>>({ method: "GET", url });
    const rows = new Map<unknown, T>();
    const add = (results: T[]) => {
        for (const row of results)
            rows.set((row as { id?: unknown }).id ?? Symbol(), row);
    };
    add(first.results);
    const origin = globalThis.location?.origin ?? "http://localhost";
    const visited = new Set([url]);
    let next = first.next;
    while (next) {
        const target = new URL(next, origin);
        const page = target.pathname + target.search;
        if (
            target.origin !== origin ||
            !target.pathname.startsWith("/api/") ||
            visited.has(page)
        )
            throw new ApiError(browserErrors()("unexpected"));
        visited.add(page);
        const answer = await send<Page<T>>({ method: "GET", url: page });
        add(answer.results);
        next = answer.next;
    }
    return {
        value: { ...first, results: [...rows.values()], next: null },
        requests: visited.size,
    };
}

export type Loaded<T> = {
    data?: T;
    error?: string;
    denied: boolean;
    // A refused read keeps the answer it last stored, so its figures can outlive the refusal.
    invalidFields: string[];
    loading: boolean;
    updatedAt: number;
    retry: () => void;
    reload: () => Promise<T>;
    replace(value: T): void;
    discard(): void;
};

// `enabled: false` only reads what another reader of the same request stored.
type Polling = { poll?: number | false; enabled?: boolean };

export function answered<T>(read: () => Promise<Fetched<T>>) {
    return async (): Promise<Stored<T>> => {
        try {
            return await read();
        } catch (error) {
            if (
                error instanceof ApiError &&
                (error.status === 403 || error.status === 404)
            )
                return {
                    refusal: {
                        status: error.status,
                        code: error.code,
                        message: error.message,
                    },
                };
            throw error;
        }
    };
}

function refusalOf<T>(stored: Stored<T> | undefined) {
    return stored && "refusal" in stored ? stored.refusal : undefined;
}

export function shownAnswer<T>({
    data,
    error,
}: {
    data: Stored<T> | undefined;
    error: unknown;
}): Pick<Loaded<T>, "data" | "error" | "denied"> {
    const refusal = refusalOf(data);
    return {
        data: refusal ? undefined : (data as Fetched<T> | undefined)?.value,
        error: error
            ? displayMessage(error, browserErrors()("unexpected"))
            : refusal?.message,
        denied: !!refusal,
    };
}

export async function reload<T>(
    client: QueryClient,
    queryKey: QueryKey,
): Promise<T> {
    const filters = { queryKey, exact: true };
    const query = client.getQueryCache().find<Stored<T>>(filters);
    const written = query?.state.dataUpdateCount;
    await client.refetchQueries(filters, { throwOnError: true });
    if (
        !query ||
        client.getQueryCache().find(filters) !== query ||
        query.state.dataUpdateCount === written
    )
        throw new ApiError(browserErrors()("unexpected"));
    const refusal = refusalOf(query.state.data);
    if (refusal) throw new ApiError(refusal.message, refusal);
    return (query.state.data as Fetched<T>).value;
}

// The data is gone at once, a read in flight is never stored, and a mounted reader reads again.
export function discard(client: QueryClient, queryKey: QueryKey) {
    void client.resetQueries({ queryKey, exact: true });
}

function invalidFields(error: unknown): string[] {
    return error instanceof ApiError && error.status === 400
        ? Object.keys(error.fields)
        : [];
}

function useOwner() {
    const owner = useContext(QueryOwner);
    if (owner === undefined)
        throw new Error("Server state needs a signed-in session.");
    return owner;
}

async function withSentAt<T>(
    read: (url: string) => Promise<Fetched<T>>,
    url: string,
): Promise<Fetched<T>> {
    const sentAt = Date.now();
    return { ...(await read(url)), sentAt };
}

function useLoaded<T>(
    path: string,
    options: object,
    { poll = false, enabled = true }: Polling,
    read: (url: string) => Promise<Fetched<T>>,
): Loaded<T> {
    const owner = useOwner();
    const client = useQueryClient();
    const url = buildUrl(path, options);
    const queryKey = keys.request(owner, path, options);
    const query = useQuery<Stored<T>>({
        queryKey,
        enabled,
        queryFn: answered(() => withSentAt(read, url)),
        refetchInterval: (query) => {
            const stored = query.state.data;
            return pollInterval(
                poll,
                stored && "requests" in stored ? stored.requests : 1,
            );
        },
    });
    return {
        ...shownAnswer<T>(query),
        invalidFields: invalidFields(query.error),
        loading: query.isPending,
        updatedAt: query.dataUpdatedAt,
        retry: () => void query.refetch({ cancelRefetch: false }),
        reload: () => reload<T>(client, queryKey),
        replace: (value) =>
            client.setQueryData<Fetched<T>>(queryKey, { value, requests: 1 }),
        discard: () => discard(client, queryKey),
    };
}

// When the read whose answer a cache event stored was sent; a refusal or a written value stores none.
export function answerSentAt(event: QueryCacheNotifyEvent) {
    if (event.type !== "updated" || event.action.type !== "success")
        return undefined;
    // Query Core marks setQueryData successes as manual.
    if (event.action.manual) return undefined;
    const stored = event.action.data as Stored<unknown> | undefined;
    return stored && "sentAt" in stored ? stored.sentAt : undefined;
}

export function useApiQuery<P extends PathFor<"get">>(
    path: P,
    options: RequestOptions<P, "get">,
    polling: Polling = {},
) {
    return useLoaded(path, options, polling, async (url) => ({
        value: await send<Answer<P, "get">>({ method: "GET", url }),
        requests: 1,
    }));
}

export function useApiList<P extends ListPath>(
    path: P,
    options: RequestOptions<P, "get">,
    polling: Polling = {},
) {
    return useLoaded(
        path,
        options,
        polling,
        (url) => fetchList(url) as Promise<Fetched<Answer<P, "get">>>,
    );
}

export function readListOnce<P extends ListPath>(
    client: QueryClient,
    owner: string,
    path: P,
    options: RequestOptions<P, "get">,
) {
    return client.fetchQuery<Stored<Answer<P, "get">>>({
        queryKey: keys.request(owner, path, options),
        queryFn: answered(() =>
            withSentAt(
                (url) => fetchList(url) as Promise<Fetched<Answer<P, "get">>>,
                buildUrl(path, options),
            ),
        ),
        staleTime: cadence.live,
    });
}

export async function readAgain(
    client: QueryClient,
    filters: QueryFilters = {},
) {
    await client.cancelQueries(filters);
    return client.invalidateQueries(filters);
}

// Pass `mounted` only when no read of it was running as the change was answered: a read running now started afterwards and stands instead of being cancelled and repeated.
export function changed(queryKey: QueryKey, mounted?: QueryKey): QueryFilters {
    return {
        queryKey,
        predicate: (query) =>
            !mounted ||
            query.state.fetchStatus !== "fetching" ||
            !partialMatchKey(query.queryKey, mounted),
    };
}

type Request = { path: string; options: Parameters<typeof keys.request>[2] };

export function useRefresh() {
    const client = useQueryClient();
    const owner = useOwner();
    return useMemo(() => {
        const tenants = keys.path(owner, "/api/tenants/");
        const askedFor = (queryKey: QueryKey) =>
            client.invalidateQueries({ queryKey }, { cancelRefetch: false });
        const key = (request: Request) =>
            keys.request(owner, request.path, request.options);
        return {
            reading: (request: Request) =>
                client.isFetching({ queryKey: key(request) }) > 0,
            workspace: (tenant: number, mounted?: Request) =>
                Promise.all([
                    readAgain(
                        client,
                        changed(
                            keys.workspace(owner, tenant),
                            mounted && key(mounted),
                        ),
                    ),
                    readAgain(client, changed(tenants)),
                ]),
            requests: (tenant: number) =>
                Promise.all([
                    askedFor(
                        keys.path(
                            owner,
                            "/api/tenants/{tenant_id}/change-requests/",
                            {
                                tenant_id: tenant,
                            },
                        ),
                    ),
                    askedFor(tenants),
                ]),
            tenants: () => client.invalidateQueries({ queryKey: tenants }),
        };
    }, [client, owner]);
}
