"use client";

import { useLayoutEffect, type ReactNode } from "react";
import { NextIntlClientProvider, useLocale } from "next-intl";
import { isLanguage } from "@/i18n/language";
import { messages } from "@/i18n/messages";

export function SessionLanguage({
    language,
    children,
}: {
    language?: string;
    children: ReactNode;
}) {
    const initialLocale = useLocale();
    const locale = isLanguage(language)
        ? language
        : isLanguage(initialLocale)
          ? initialLocale
          : "en";
    useLayoutEffect(() => {
        document.documentElement.lang = locale;
    }, [locale]);
    return (
        <NextIntlClientProvider
            locale={locale}
            messages={messages[locale]}
            timeZone="UTC"
        >
            {children}
        </NextIntlClientProvider>
    );
}
