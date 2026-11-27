"use client";
import type { QueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { SAVED_FIGURES } from "@/lib/saved/feature";
import type { CopyView } from "@/lib/saved/store";
import {
    announceSavedChange,
    boundedStore,
    readStored,
    savedFlagKey,
    sessionOwner,
    sessionStill,
    subscribeSavedFigures,
    turnOnSavedFigures,
    useSessionState,
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

// Only the worker reads the copy, so without its registration the copy is neither kept nor refreshed.
async function registered() {
    try {
        return Boolean(await navigator.serviceWorker?.getRegistration("/app/"));
    } catch {
        return false;
    }
}

// Undefined where nothing can be kept; a browser that cannot keep the flag keeps nothing new, but a copy it holds stays removable.
async function deviceCopy(user: number, session: string) {
    if (!(await registered())) return undefined;
    const copy = await boundedStore(
        (store, limit) => store.copyState({ user, session }, limit),
        undefined,
        false,
    );
    if (!copy || copy.state === "unavailable") return undefined;
    return copy.state === "off" && readStored(savedFlagKey) === undefined
        ? undefined
        : copy;
}

// The worker's registration can complete after the screen mounted.
function subscribeKeeping(listener: () => void) {
    let subscribed = true;
    void navigator.serviceWorker?.ready.then(() => {
        if (subscribed) listener();
    });
    const unsubscribe = subscribeSavedFigures(listener);
    return () => {
        subscribed = false;
        unsubscribe();
    };
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

// Figures are kept by default; only this person's own "off" on this device, read in the store, stops it.
export function useSavedFigures(
    client: QueryClient,
    session: Session | undefined,
) {
    const owner = sessionOwner(session);
    const user = session?.user?.id;
    const tag = session?.session_tag;
    useEffect(() => {
        if (!SAVED_FIGURES || owner === undefined || user === undefined || !tag)
            return;
        const shown: RefreshContext = {
            client,
            owner,
            user,
            session: tag,
            current: () => sessionStill(owner),
        };
        let active = true;
        const check = async () => {
            const copy = await deviceCopy(user, tag);
            if (!active) return;
            if (copy?.state === "on") {
                startRefresh(shown);
                return;
            }
            stopRefresh();
            if (copy?.state === "off" && !copy.off) void turnOnSavedFigures();
        };
        void check();
        const unsubscribe = subscribeKeeping(() => void check());
        return () => {
            active = false;
            unsubscribe();
            stopRefresh();
        };
    }, [client, owner, user, tag]);
}

// `asked`: the choice being written, shown until the stored state is read back.
type Preference = {
    user: number;
    copy?: CopyView;
    failed: boolean;
    asked?: boolean;
};

export function useKeptFiguresPreference() {
    const { session } = useSessionState();
    const user = session?.user?.id;
    const tag = session?.session_tag;
    const [preference, setPreference] = useState<Preference>();
    useEffect(() => {
        if (!SAVED_FIGURES || user === undefined || !tag) return;
        let active = true;
        const read = () =>
            void deviceCopy(user, tag).then((copy) => {
                if (active) setPreference({ user, copy, failed: false });
            });
        read();
        const unsubscribe = subscribeKeeping(read);
        return () => {
            active = false;
            unsubscribe();
        };
    }, [user, tag]);
    if (!tag || !preference?.copy || preference.user !== user) return undefined;
    const { user: person, copy, failed, asked } = preference;
    const change = async (kept: boolean) => {
        setPreference({ user: person, copy, failed: false, asked: kept });
        const done = await boundedStore(
            (store, limit) =>
                store.setKept(person, kept, announceSavedChange, limit),
            undefined,
            false,
        );
        if (!done)
            setPreference({
                user: person,
                copy: await deviceCopy(person, tag),
                failed: true,
            });
    };
    return {
        kept: asked ?? !copy.off,
        failed,
        change: (kept: boolean) => void change(kept),
    };
}
