"use client";
import { useRouter } from "next/navigation";
import { useCallback, useEffect } from "react";
import { confirmedSince } from "./api/writes";

const entries = new Set<() => boolean>();
// Set by the first question and never cleared: Next may have left a soft change suspended behind it.
let asked = false;

function holding() {
    for (const holds of entries) if (holds()) return true;
    return false;
}

function keep(event: BeforeUnloadEvent) {
    if (!holding()) return;
    event.preventDefault();
    asked = true;
}

export function reloadGuardedTraversal(event: PopStateEvent) {
    if (!holding() && !asked) return;
    event.stopImmediatePropagation();
    location.reload();
}

function listen() {
    if (entries.size) window.addEventListener("beforeunload", keep);
    else window.removeEventListener("beforeunload", keep);
}

function register(holds: () => boolean) {
    entries.add(holds);
    listen();
    return () => {
        entries.delete(holds);
        listen();
    };
}

export function useLeaveGuard(unsaved: boolean) {
    useEffect(() => {
        if (unsaved) return register(() => true);
    }, [unsaved]);
}

// Holds until a write started after `since` is confirmed; after a refused or lost answer, until `since` is null.
export function useSubmissionGuard(since: number | null) {
    useEffect(() => {
        if (since !== null) return register(() => !confirmedSince(since));
    }, [since]);
}

type Mode = "push" | "replace";

export function useNavigate() {
    const router = useRouter();
    return useCallback(
        (href: string, mode: Mode = "push") => {
            if (holding() || asked) {
                if (mode === "push") location.assign(href);
                else location.replace(href);
                return false;
            }
            router[mode](href);
            return true;
        },
        [router],
    );
}

// The pinned router takes up an address set through history without a request, so nothing is pending or asked.
export function replaceAddress(href: string) {
    history.replaceState(null, "", href);
}
