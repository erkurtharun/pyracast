"use client";
import { useEffect, useSyncExternalStore } from "react";

const installedWindowQueries = [
    "standalone",
    "minimal-ui",
    "window-controls-overlay",
].map((mode) => `(display-mode: ${mode})`);

function subscribeDisplayMode(listener: () => void) {
    const queries = installedWindowQueries.map((query) =>
        window.matchMedia(query),
    );
    for (const query of queries) query.addEventListener("change", listener);
    return () => {
        for (const query of queries)
            query.removeEventListener("change", listener);
    };
}

function inInstalledWindow() {
    return installedWindowQueries.some(
        (query) => window.matchMedia(query).matches,
    );
}

// The /app layout keeps one <title> mounted; a screen renames it while it is shown.
export function usePageTitle(screen: string) {
    const installed = useSyncExternalStore(
        subscribeDisplayMode,
        inInstalledWindow,
        () => false,
    );
    const title = installed ? screen : `${screen} | Pyracast`;
    useEffect(() => {
        const previous = document.title;
        document.title = title;
        return () => {
            document.title = previous;
        };
    }, [title]);
}
