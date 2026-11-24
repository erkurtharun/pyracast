"use client";

import { useQueryClient } from "@tanstack/react-query";
import {
    useEffect,
    useLayoutEffect,
    useRef,
    useState,
    type ReactNode,
} from "react";
import {
    NextIntlClientProvider,
    useLocale,
    type AbstractIntlMessages,
} from "next-intl";
import { loadCatalogue, useCatalogues } from "@/i18n/catalogue";
import { isLanguage, type Language } from "@/i18n/language";
import { shell } from "@/i18n/shell";
import { readAgain } from "@/lib/api/query";
import type { Session } from "@/lib/session-client";

export type SessionLanguageState = {
    locale: Language;
    messages: AbstractIntlMessages;
    status: "ready" | "loading" | "failed";
};

// `session` is undefined until the first session read, and a pending language hand-off may still
// change its language: until both have settled only the shell shows, and a hand-off starts no
// catalogue. After a catalogue was shown, a change keeps it until the next one arrives, so a screen
// never shows two languages at once.
export function useSessionLanguage(
    session: Session | undefined,
    handoff: boolean,
): SessionLanguageState {
    const initialLocale = useLocale();
    const language = session?.language;
    const wanted = isLanguage(language)
        ? language
        : isLanguage(initialLocale)
          ? initialLocale
          : "en";
    const catalogues = useCatalogues();
    const [shown, setShown] = useState<Language>();
    const entry = catalogues[wanted];
    useEffect(() => {
        if (!handoff && !entry) loadCatalogue(wanted).catch(() => {});
    }, [handoff, entry, wanted]);
    if (session === undefined || handoff)
        return { locale: wanted, messages: shell[wanted], status: "loading" };
    if (entry?.status === "ready") {
        if (shown !== wanted) setShown(wanted);
        return { locale: wanted, messages: entry.messages, status: "ready" };
    }
    const previous = shown && catalogues[shown];
    if (shown && previous?.status === "ready")
        return { locale: shown, messages: previous.messages, status: "ready" };
    return {
        locale: wanted,
        messages: shell[wanted],
        status: entry?.status === "failed" ? "failed" : "loading",
    };
}

export function SessionLanguage({
    value,
    children,
}: {
    value: SessionLanguageState;
    children: ReactNode;
}) {
    const client = useQueryClient();
    const shown = useRef(value.locale);
    useLayoutEffect(() => {
        document.documentElement.lang = value.locale;
        if (shown.current === value.locale) return;
        shown.current = value.locale;
        void readAgain(client);
    }, [value.locale, client]);
    return (
        <NextIntlClientProvider
            locale={value.locale}
            messages={value.messages}
            timeZone="UTC"
        >
            {children}
        </NextIntlClientProvider>
    );
}
