"use client";
import type { QueryClient } from "@tanstack/react-query";
import { useEffect, useState, useSyncExternalStore } from "react";
import {
    readStored,
    removeSavedFiguresHere,
    savedCopyState,
    savedFiguresAvailable,
    sessionOwner,
    sessionStill,
    subscribeSavedFigures,
    turnOnSavedFigures,
    useSessionState,
    writeStored,
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

const turnedOffPrefix = "pyracast.savedOff.";
const choiceListeners = new Set<() => void>();
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

async function keepingPossible() {
    return (await registered()) && (await savedFiguresAvailable());
}

// Figures are kept by default; only this person's own "off" on this device stops it.
function turnedOff(user: number) {
    return readStored(turnedOffPrefix + user) !== null;
}

function subscribeChoice(listener: () => void) {
    choiceListeners.add(listener);
    const unsubscribe = subscribeSavedFigures(listener);
    return () => {
        choiceListeners.delete(listener);
        unsubscribe();
    };
}

async function keepFigures(user: number, kept: boolean) {
    writeStored(turnedOffPrefix + user, kept ? null : "1");
    for (const listener of choiceListeners) listener();
    await (kept ? turnOnSavedFigures() : removeSavedFiguresHere());
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

export function useSavedFigures(
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
            const state = (await registered())
                ? (await savedCopyState()).state
                : "unavailable";
            if (!active) return;
            if (state === "on") {
                startRefresh(shown);
                return;
            }
            stopRefresh();
            if (
                state === "off" &&
                !turnedOff(user) &&
                (await savedFiguresAvailable()) &&
                active
            )
                void turnOnSavedFigures();
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

export function useKeptFiguresPreference() {
    const user = useSessionState().session?.user?.id;
    const [possible, setPossible] = useState(false);
    const kept = useSyncExternalStore(
        subscribeChoice,
        () => user !== undefined && !turnedOff(user),
        () => true,
    );
    useEffect(() => {
        let active = true;
        void keepingPossible().then((answer) => {
            if (active) setPossible(answer);
        });
        return () => {
            active = false;
        };
    }, []);
    if (!possible || user === undefined) return undefined;
    return {
        kept,
        change: (next: boolean) => void keepFigures(user, next),
    };
}
