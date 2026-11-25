"use client";
import { localPath, routes } from "@/lib/routes";

const storageKey = "pyracast.providerReturn";
const longestAddress = 2_000;
let landing: string | undefined;

function workspaceAddress(value: string | null) {
    return value && value.length <= longestAddress
        ? localPath(value, routes.workspaces(), window.location.origin)
        : undefined;
}

export function rememberProviderReturn() {
    const { pathname, search } = window.location;
    const address = workspaceAddress(`${pathname}${search}`);
    try {
        if (address) window.sessionStorage.setItem(storageKey, address);
        else window.sessionStorage.removeItem(storageKey);
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
    landing = succeeded ? address : undefined;
}

export function providerLanding() {
    return landing;
}

export function forgetProviderLanding() {
    landing = undefined;
}
