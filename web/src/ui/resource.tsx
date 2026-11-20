"use client";
import type { ReactNode } from "react";
import type { Loaded } from "@/lib/api/query";
import { resourceState } from "./resource-state";
import { Notice, RefreshFailed } from "./ui";

export function Resource<T>({
    resource,
    loading,
    children,
}: {
    resource: Loaded<T>;
    loading: string;
    children: (data: T) => ReactNode;
}) {
    const state = resourceState(resource);
    if (state.status === "loading") return <p role="status">{loading}</p>;
    if (state.status === "failed") return <Notice error>{state.error}</Notice>;
    return (
        <>
            <RefreshFailed resource={resource} />
            {children(state.data)}
        </>
    );
}
