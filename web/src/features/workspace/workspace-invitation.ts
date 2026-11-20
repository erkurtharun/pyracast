"use client";
import { useSyncExternalStore } from "react";

const storageKey = "pyracast.workspaceInvitation";
const listeners = new Set<() => void>();
// Browser privacy settings can disable session storage; this page load then keeps the token in memory.
let pending: string | null = null;

function notify() {
    for (const listener of listeners) listener();
}

function subscribe(listener: () => void) {
    listeners.add(listener);
    return () => {
        listeners.delete(listener);
    };
}

function persist(write: (storage: Storage) => void) {
    try {
        write(window.sessionStorage);
    } catch {
        return;
    }
}

export function storedInvitation() {
    if (pending !== null) return pending;
    try {
        return window.sessionStorage.getItem(storageKey);
    } catch {
        return null;
    }
}

export function storeInvitation(token: string) {
    pending = token;
    persist((storage) => storage.setItem(storageKey, token));
    notify();
}

export function clearWorkspaceInvitation() {
    pending = null;
    persist((storage) => storage.removeItem(storageKey));
    notify();
}

export function useStoredInvitation(): string | null | undefined {
    return useSyncExternalStore(subscribe, storedInvitation, () => undefined);
}
