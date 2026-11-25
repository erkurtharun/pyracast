"use client";
import { useRouter } from "next/navigation";
import { useCallback, useLayoutEffect } from "react";
import { whenWritesChange, writesUnanswered } from "./api/writes";

const entries = new Set<() => boolean>();
// Set by the first question and never cleared: Next may have left a soft change suspended behind it.
let asked = false;

function holding() {
    for (const holds of entries) if (holds()) return true;
    return false;
}

// A full load loses a write still on its way; a route change keeps the document that awaits its answer.
function keep(event: BeforeUnloadEvent) {
    if (!holding() && !writesUnanswered()) return;
    event.preventDefault();
    asked = true;
}

export function reloadGuardedTraversal(event: PopStateEvent) {
    if (!holding() && !asked) return;
    event.stopImmediatePropagation();
    location.reload();
}

function listen() {
    if (entries.size || writesUnanswered())
        window.addEventListener("beforeunload", keep);
    else window.removeEventListener("beforeunload", keep);
}

whenWritesChange(listen);

function register(holds: () => boolean) {
    entries.add(holds);
    listen();
    return () => {
        entries.delete(holds);
        listen();
    };
}

// Registered in the commit that shows the state: a passive effect can run after a question already meets that state.
export function useLeaveGuard(unsaved: boolean) {
    useLayoutEffect(() => {
        if (unsaved) return register(() => true);
    }, [unsaved]);
}

export type Submission = { holds: () => boolean; confirm: () => void };

// Its owner confirms it once its own change is known applied, which counts before the next commit.
export function submission(): Submission {
    let confirmed = false;
    return {
        holds: () => !confirmed,
        confirm: () => {
            confirmed = true;
        },
    };
}

export function useSubmissionGuard(running: Submission | null) {
    useLayoutEffect(() => {
        if (running) return register(running.holds);
    }, [running]);
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
