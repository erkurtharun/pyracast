import type { Loaded } from "@/lib/api/query";

export type ResourceState<T> =
    | { status: "loading" }
    | { status: "failed"; error: string }
    | { status: "ready"; data: T };

// Data kept from an earlier read stays ready when a refresh fails; the refresh error is shown beside it.
export function resourceState<T>(
    resource: Pick<Loaded<T>, "data" | "error">,
): ResourceState<T> {
    if (resource.data !== undefined)
        return { status: "ready", data: resource.data };
    return resource.error
        ? { status: "failed", error: resource.error }
        : { status: "loading" };
}
