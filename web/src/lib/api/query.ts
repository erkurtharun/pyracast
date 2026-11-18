"use client";
import {
    QueryClient,
    useQuery,
    useQueryClient,
    type QueryKey,
} from "@tanstack/react-query";
import { useLocale } from "next-intl";
import { createContext, useContext } from "react";
import { browserErrors } from "@/i18n/browser";
import {
    buildUrl,
    type Answer,
    type ListPath,
    type PathFor,
    type RequestOptions,
} from "./client";
import { ApiError } from "./errors";
import { keys } from "./keys";
import { send } from "./lane";

// Milliseconds between polls of a visible tab; a hidden tab never polls.
export const cadence = {
    live: 30_000,
    steady: 60_000,
    background: 120_000,
} as const;

export function createQueryClient() {
    return new QueryClient({
        // A navigation that remounts a screen reuses data read within the last two seconds.
        defaultOptions: { queries: { retry: false, staleTime: 2_000 } },
    });
}

export const QueryOwner = createContext<number | undefined>(undefined);

type Page<T> = { results: T[]; next: string | null };

export async function fetchList<T>(url: string): Promise<Page<T>> {
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
            throw new ApiError(browserErrors()("invalidPage"));
        visited.add(page);
        const answer = await send<Page<T>>({ method: "GET", url: page });
        add(answer.results);
        next = answer.next;
    }
    return { ...first, results: [...rows.values()], next: null };
}

export type Loaded<T> = {
    data?: T;
    error?: string;
    loading: boolean;
    updatedAt: number;
    retry: () => void;
    reload: () => Promise<T>;
};

type Polling = { poll?: number | false };

function useOwner() {
    const owner = useContext(QueryOwner);
    if (owner === undefined)
        throw new Error("Server state needs a signed-in session.");
    return owner;
}

function useLoaded<T>(
    path: string,
    options: object,
    { poll = false }: Polling,
    read: (url: string) => Promise<T>,
): Loaded<T> {
    const owner = useOwner();
    const locale = useLocale();
    const url = buildUrl(path, options);
    const query = useQuery({
        queryKey: keys.request(owner, path, options, locale),
        queryFn: () => read(url),
        refetchInterval: poll,
    });
    const denied =
        query.error instanceof ApiError &&
        (query.error.status === 403 || query.error.status === 404);
    return {
        data: denied ? undefined : query.data,
        error: query.error?.message,
        loading: query.isPending,
        updatedAt: query.dataUpdatedAt,
        retry: () => void query.refetch(),
        reload: async () => {
            const result = await query.refetch();
            if (result.error) throw result.error;
            return result.data as T;
        },
    };
}

export function useApiQuery<P extends PathFor<"get">>(
    path: P,
    options: RequestOptions<P, "get">,
    polling: Polling = {},
) {
    return useLoaded(path, options, polling, (url) =>
        send<Answer<P, "get">>({ method: "GET", url }),
    );
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
        (url) => fetchList(url) as Promise<Answer<P, "get">>,
    );
}

export function useRefresh() {
    const client = useQueryClient();
    const owner = useOwner();
    const refresh = (queryKey: QueryKey) =>
        client.invalidateQueries({ queryKey });
    return {
        workspace: (tenant: number) =>
            Promise.all([
                refresh(keys.workspace(owner, tenant)),
                refresh(keys.path(owner, "/api/tenants/")),
            ]),
        tenants: () => refresh(keys.path(owner, "/api/tenants/")),
    };
}
