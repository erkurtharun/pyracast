"use client";
import { useRouter } from "next/navigation";
import { useCallback, useEffect, useRef } from "react";

const dirty = new Set<object>();
// Set by the first question and never cleared: Next may have left a soft change suspended behind it.
let asked = false;

function keep(event: BeforeUnloadEvent) {
    event.preventDefault();
    asked = true;
}

export function reloadGuardedTraversal(event: PopStateEvent) {
    if (!dirty.size && !asked) return;
    event.stopImmediatePropagation();
    location.reload();
}

function listen() {
    if (dirty.size) window.addEventListener("beforeunload", keep);
    else window.removeEventListener("beforeunload", keep);
}

function register(entry: object) {
    dirty.add(entry);
    listen();
}

function release(entry: object) {
    dirty.delete(entry);
    listen();
}

export function useLeaveGuard(unsaved: boolean) {
    const entry = useRef<object | null>(null);
    useEffect(() => {
        if (!unsaved) return;
        const own = {};
        entry.current = own;
        register(own);
        return () => release(own);
    }, [unsaved]);
    return useCallback(() => {
        if (entry.current) release(entry.current);
    }, []);
}

type Mode = "push" | "replace";

export function useNavigate() {
    const router = useRouter();
    return useCallback(
        (href: string, mode: Mode = "push") => {
            if (dirty.size || asked) {
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
