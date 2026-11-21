"use client";

import { useEffect, useLayoutEffect, useState, type ReactNode } from "react";
import {
    NextIntlClientProvider,
    useLocale,
    type AbstractIntlMessages,
} from "next-intl";
import { loadCatalogue, useCatalogues } from "@/i18n/catalogue";
import { isLanguage, type Language } from "@/i18n/language";
import { shell } from "@/i18n/shell";

export type SessionLanguageState = {
    locale: Language;
    messages: AbstractIntlMessages;
    status: "ready" | "loading" | "failed";
};

// `language` is undefined until the first session read, and a pending language hand-off may still
// change it: until both have settled only the shell shows, and a hand-off starts no catalogue. After a
// catalogue was shown, a change keeps it until the next one arrives, so a screen never shows two
// languages at once.
export function useSessionLanguage(
    language: string | undefined,
    handoff: boolean,
): SessionLanguageState {
    const initialLocale = useLocale();
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
    if (language === undefined || handoff)
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
    useLayoutEffect(() => {
        document.documentElement.lang = value.locale;
    }, [value.locale]);
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
