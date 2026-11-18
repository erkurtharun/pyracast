"use client";
import { useSyncExternalStore } from "react";

const storageKey = "pyracast.workspaceInvitation";
const listeners = new Set<() => void>();

function notify() {
    for (const listener of listeners) listener();
}

function subscribe(listener: () => void) {
    listeners.add(listener);
    return () => {
        listeners.delete(listener);
    };
}

export function storedInvitation() {
    try {
        return window.sessionStorage.getItem(storageKey);
    } catch {
        return null;
    }
}

export function storeInvitation(token: string) {
    try {
        window.sessionStorage.setItem(storageKey, token);
    } catch {
        // Browser privacy settings can disable session storage.
    }
    notify();
}

export function clearWorkspaceInvitation() {
    try {
        window.sessionStorage.removeItem(storageKey);
    } catch {
        // Browser privacy settings can disable session storage.
    }
    notify();
}

export function useStoredInvitation(): string | null | undefined {
    return useSyncExternalStore(subscribe, storedInvitation, () => undefined);
}
