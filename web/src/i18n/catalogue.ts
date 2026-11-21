import { useSyncExternalStore } from "react";
import type { english } from "./en";
import type { Language } from "./language";

type Catalogue = typeof english;
type CatalogueEntry =
    | { status: "loading"; loaded: Promise<Catalogue> }
    | { status: "ready"; messages: Catalogue }
    | { status: "failed" };
type Catalogues = Readonly<Partial<Record<Language, CatalogueEntry>>>;

const loaders: Record<Language, () => Promise<Catalogue>> = {
    en: () => import("./en").then((module) => module.english),
    tr: () => import("./tr").then((module) => module.turkish),
};
const none: Catalogues = {};
let catalogues = none;
const listeners = new Set<() => void>();

function record(language: Language, entry: CatalogueEntry) {
    catalogues = { ...catalogues, [language]: entry };
    for (const listener of listeners) listener();
}

export function loadCatalogue(language: Language): Promise<Catalogue> {
    const entry = catalogues[language];
    if (entry?.status === "ready") return Promise.resolve(entry.messages);
    if (entry?.status === "loading") return entry.loaded;
    const loaded = loaders[language]().then(
        (messages) => {
            record(language, { status: "ready", messages });
            return messages;
        },
        (error: unknown) => {
            record(language, { status: "failed" });
            throw error;
        },
    );
    record(language, { status: "loading", loaded });
    return loaded;
}

function subscribe(listener: () => void) {
    listeners.add(listener);
    return () => {
        listeners.delete(listener);
    };
}

// The server and the hydrating render see no catalogue, so both render the shell texts.
export function useCatalogues() {
    return useSyncExternalStore(
        subscribe,
        () => catalogues,
        () => none,
    );
}
