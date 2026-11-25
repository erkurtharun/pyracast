import type { paths } from "../schema";
import { readFailure } from "./errors";
import { send, type Method } from "./lane";
import { trackWrite } from "./writes";

type Verb = "get" | "post" | "put" | "patch" | "delete";
type Operation<P extends keyof paths, V extends Verb> = NonNullable<
    paths[P][V]
>;

export type PathFor<V extends Verb> = {
    [P in keyof paths]: paths[P][V] extends undefined ? never : P;
}[keyof paths];

type PathParams<O> = O extends { parameters: { path: infer T } } ? T : never;
type QueryParams<O> = O extends { parameters: { query?: infer T } }
    ? NonNullable<T>
    : never;
type RequiredBody<O> = O extends {
    requestBody: { content: { "application/json": infer B } };
}
    ? B
    : never;
type OptionalBody<O> = O extends { requestBody?: infer R }
    ? [NonNullable<R>] extends [never]
        ? never
        : NonNullable<R> extends { content: { "application/json": infer B } }
          ? B
          : never
    : never;

type Options<O> = ([PathParams<O>] extends [never]
    ? { path?: never }
    : { path: PathParams<O> }) &
    ([QueryParams<O>] extends [never]
        ? { query?: never }
        : { query?: QueryParams<O> }) &
    ([RequiredBody<O>] extends [never]
        ? [OptionalBody<O>] extends [never]
            ? { body?: never }
            : { body?: OptionalBody<O> }
        : { body: RequiredBody<O> });

type Content<R> = R extends { content: { "application/json": infer B } }
    ? B
    : null;
type Success<O> = O extends { responses: infer R }
    ? Content<R[Extract<keyof R, 200 | 201 | 202 | 204>]>
    : never;

export type RequestOptions<P extends keyof paths, V extends Verb> = Options<
    Operation<P, V>
>;
export type Answer<P extends keyof paths, V extends Verb> = Success<
    Operation<P, V>
>;
export type ListPath = {
    [P in PathFor<"get">]: Answer<P, "get"> extends { results: unknown[] }
        ? P
        : never;
}[PathFor<"get">];
type Rest<T> = object extends T ? [options?: T] : [options: T];
type Loose = {
    path?: Record<string, string | number>;
    query?: Record<string, string | number | boolean | null | undefined>;
    body?: unknown;
};

export function buildUrl(path: string, options: object = {}) {
    const { path: params, query } = options as Loose;
    const filled = path.replace(/\{(\w+)\}/g, (_, name: string) => {
        const value = params?.[name];
        if (value === undefined)
            throw new Error(`Missing path parameter: ${name}`);
        return encodeURIComponent(String(value));
    });
    const search = new URLSearchParams();
    for (const [name, value] of Object.entries(query ?? {}))
        if (value !== undefined && value !== null)
            search.set(name, String(value));
    const text = search.toString();
    return text ? `${filled}?${text}` : filled;
}

export function apiUrl<P extends PathFor<"get">>(
    path: P,
    ...[options]: Rest<RequestOptions<P, "get">>
) {
    return buildUrl(path, options);
}

// POSTs that change nothing: a lost answer is a failed read, not an unconfirmed change.
const READS: ReadonlySet<string> = new Set<PathFor<"post">>([
    "/api/invitations/preview/",
    "/api/tenants/{tenant_id}/regions/{region_id}/plants/{plant_id}/production/preview/",
    "/api/tenants/{tenant_id}/regions/{region_id}/reminders/preview/",
]);

function verb<V extends Verb>(method: Method) {
    return <P extends PathFor<V>>(
        path: P,
        ...[options]: Rest<RequestOptions<P, V>>
    ): Promise<Answer<P, V>> => {
        const answer = send<Answer<P, V>>({
            method,
            url: buildUrl(path, options),
            // Customer endpoints accept JSON bodies only, so a body-less POST sends {}.
            body:
                (options as Loose | undefined)?.body ??
                (method === "POST" ? {} : undefined),
        });
        if (READS.has(path))
            return answer.catch((error: unknown) => {
                throw readFailure(error);
            });
        return method === "GET" ? answer : trackWrite(answer);
    };
}

export const api = {
    get: verb<"get">("GET"),
    post: verb<"post">("POST"),
    put: verb<"put">("PUT"),
    patch: verb<"patch">("PATCH"),
    delete: verb<"delete">("DELETE"),
};
