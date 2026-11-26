"use client";
import type { QueryClient } from "@tanstack/react-query";
import { useEffect } from "react";
import {
    savedCopyState,
    sessionOwner,
    sessionStill,
    subscribeSavedFigures,
    type Session,
} from "@/lib/session-client";

export type ShownPlant = {
    tenant: number;
    site: number;
    plant: number;
    name: string;
    workspace: string;
};
export type RefreshContext = {
    client: QueryClient;
    owner: string;
    user: number;
    session: string;
    current: () => boolean;
};

let plant: ShownPlant | undefined;
let workspace: number | undefined;
const opened = new Set<() => void>();
let mounted: { owner: string; stop?: () => void } | undefined;

export function shownPlant() {
    return plant;
}

export function shownWorkspace() {
    return workspace;
}

export function subscribeOpened(listener: () => void) {
    opened.add(listener);
    return () => {
        opened.delete(listener);
    };
}

export function useShownPlant(shown: ShownPlant) {
    const { tenant, site, plant: id, name, workspace: named } = shown;
    useEffect(() => {
        const current = { tenant, site, plant: id, name, workspace: named };
        plant = current;
        for (const listener of opened) listener();
        return () => {
            if (plant === current) plant = undefined;
        };
    }, [tenant, site, id, name, named]);
}

export function useShownWorkspace(tenant: number | undefined) {
    useEffect(() => {
        workspace = tenant;
        return () => {
            if (workspace === tenant) workspace = undefined;
        };
    }, [tenant]);
}

// Only the worker reads the copy, so without its registration the copy is neither offered nor refreshed.
export async function registered() {
    try {
        return Boolean(await navigator.serviceWorker?.getRegistration("/app/"));
    } catch {
        return false;
    }
}

function startRefresh(context: RefreshContext) {
    if (mounted?.owner === context.owner) return;
    stopRefresh();
    const entry: { owner: string; stop?: () => void } = {
        owner: context.owner,
    };
    mounted = entry;
    void import("./refresh").then(
        ({ mountRefresh }) => {
            if (mounted === entry) entry.stop = mountRefresh(context);
        },
        () => {
            if (mounted === entry) mounted = undefined;
        },
    );
}

function stopRefresh() {
    mounted?.stop?.();
    mounted = undefined;
}

export function useSavedFiguresRefresh(
    client: QueryClient,
    session: Session | undefined,
) {
    const owner = sessionOwner(session);
    const user = session?.user?.id;
    const tag = session?.session_tag;
    useEffect(() => {
        if (owner === undefined || user === undefined || !tag) return;
        const shown: RefreshContext = {
            client,
            owner,
            user,
            session: tag,
            current: () => sessionStill(owner),
        };
        let active = true;
        const check = async () => {
            const on =
                (await registered()) && (await savedCopyState()).state === "on";
            if (!active) return;
            if (on) startRefresh(shown);
            else stopRefresh();
        };
        void check();
        const unsubscribe = subscribeSavedFigures(() => void check());
        return () => {
            active = false;
            unsubscribe();
            stopRefresh();
        };
    }, [client, owner, user, tag]);
}
