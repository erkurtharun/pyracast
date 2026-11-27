"use client";
import type { QueryClient } from "@tanstack/react-query";
import { useEffect, useState, useSyncExternalStore } from "react";
import { SAVED_FIGURES } from "@/lib/saved/feature";
import type { CopyView } from "@/lib/saved/store";
import {
    boundedStore,
    commitToStore,
    sessionOwner,
    sessionStill,
    subscribeSavedFigures,
    turnOnSavedFigures,
    useSessionState,
    type Session,
} from "@/lib/session-client";

type ShownPlant = {
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
// The owner whose copy this device refused to create.
let unkept: string | undefined;
const unkeptListeners = new Set<() => void>();

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
        return undefined;
    }
}

// "none" where nothing can be kept, "unread" when the device did not answer.
async function deviceCopy(
    user: number,
    session: string,
): Promise<CopyView | "none" | "unread"> {
    const worker = await registered();
    if (worker === false) return "none";
    if (worker === undefined) return "unread";
    const copy = await boundedStore(
        (store, limit) => store.copyState({ user, session }, limit),
        undefined,
        false,
    );
    return !copy || copy.state === "unavailable" ? "unread" : copy;
}

function noteUnkept(owner: string | undefined) {
    if (unkept === owner) return;
    unkept = owner;
    for (const listener of unkeptListeners) listener();
}

function subscribeUnkept(listener: () => void) {
    unkeptListeners.add(listener);
    return () => {
        unkeptListeners.delete(listener);
    };
}

function unkeptOwner() {
    return unkept;
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
            if (typeof copy === "object" && copy.state === "on") {
                noteUnkept(undefined);
                startRefresh(shown);
                return;
            }
            stopRefresh();
            if (typeof copy !== "object") return;
            if (copy.state !== "off" || copy.off) {
                noteUnkept(undefined);
                return;
            }
            const accepted = await turnOnSavedFigures();
            if (active) noteUnkept(accepted ? undefined : owner);
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

// `copy` is the last state the store confirmed; a read that fails keeps it.
type Preference = {
    user: number;
    copy: CopyView;
    choice: "confirmed" | "saving" | "failed";
};

export function useKeptFiguresPreference() {
    const { session } = useSessionState();
    const user = session?.user?.id;
    const tag = session?.session_tag;
    const refused =
        useSyncExternalStore(subscribeUnkept, unkeptOwner, () => undefined) ===
        sessionOwner(session);
    const [preference, setPreference] = useState<Preference>();
    useEffect(() => {
        if (!SAVED_FIGURES || user === undefined || !tag) return;
        let active = true;
        const read = () =>
            void deviceCopy(user, tag).then((copy) => {
                if (!active || copy === "unread") return;
                setPreference((known) =>
                    copy === "none"
                        ? undefined
                        : {
                              user,
                              copy,
                              choice:
                                  known?.user === user
                                      ? known.choice
                                      : "confirmed",
                          },
                );
            });
        read();
        const unsubscribe = subscribeKeeping(read);
        return () => {
            active = false;
            unsubscribe();
        };
    }, [user, tag]);
    if (!tag || !preference || preference.user !== user) return undefined;
    const { user: person, copy, choice } = preference;
    const change = async (kept: boolean) => {
        if (choice === "saving") return;
        setPreference({ ...preference, choice: "saving" });
        const committed = await commitToStore((store, limit) =>
            store.setKept(person, kept, limit),
        );
        setPreference((known) =>
            known?.user !== person
                ? known
                : committed
                  ? {
                        user: person,
                        copy: { ...known.copy, off: !kept },
                        choice: "confirmed",
                    }
                  : { ...known, choice: "failed" },
        );
    };
    return {
        kept: !copy.off && !refused,
        saving: choice === "saving",
        failed: choice === "failed" || refused,
        change: (kept: boolean) => void change(kept),
    };
}
