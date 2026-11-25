"use client";
import { localPath, routes } from "@/lib/routes";
import { storedInvitation } from "@/features/workspace/workspace-invitation";

const storageKey = "pyracast.providerReturn";
const longestAddress = 2_000;
const parentSegment = /(?:^|\/)(?:\.|%2e){2}(?:\/|$)/i;
let landing: { address: string; confirmed: boolean } | undefined;

function workspaceAddress(value: string | null) {
    const prefix = routes.workspaces();
    if (
        !value ||
        value.length > longestAddress ||
        !value.startsWith(prefix) ||
        parentSegment.test(value.split(/[?#]/, 1)[0] ?? "")
    )
        return undefined;
    const address = localPath(value, prefix, window.location.origin);
    return address && address.length <= longestAddress ? address : undefined;
}

export function rememberProviderReturn() {
    const { pathname, search } = window.location;
    const address = workspaceAddress(`${pathname}${search}`);
    try {
        window.sessionStorage.removeItem(storageKey);
        if (address) window.sessionStorage.setItem(storageKey, address);
    } catch {
        return;
    }
}

function takeStored() {
    try {
        const stored = window.sessionStorage.getItem(storageKey);
        window.sessionStorage.removeItem(storageKey);
        return workspaceAddress(stored);
    } catch {
        return undefined;
    }
}

export function receiveProviderReturn(succeeded: boolean) {
    const address = takeStored();
    landing = succeeded && address ? { address, confirmed: false } : undefined;
}

export function confirmProviderSignIn() {
    if (landing) landing = { ...landing, confirmed: true };
}

export function forgetProviderLanding() {
    landing = undefined;
}

// Runs when a session of another owner retires the document, before it reloads at its address.
export function openProviderLanding() {
    if (landing?.confirmed && !storedInvitation())
        window.history.replaceState(window.history.state, "", landing.address);
    landing = undefined;
}
