"use client";

import type { MouseEvent, ReactNode } from "react";
import type { Language } from "@/i18n/language";
import type { components } from "@/lib/schema";

type Session = components["schemas"]["SessionOutput"];

const saveLimitMs = 3_000;

async function saveForSignedIn(language: Language) {
    const signal = AbortSignal.timeout(saveLimitMs);
    const read = await fetch("/api/session/", { cache: "no-store", signal });
    if (!read.ok) return;
    const session = (await read.json()) as Session;
    if (!session.user) return;
    await fetch("/api/session/language/", {
        method: "POST",
        cache: "no-store",
        signal,
        headers: {
            "Content-Type": "application/json",
            "X-CSRFToken": session.csrf_token,
            ...(session.session_tag
                ? { "X-Session-Tag": session.session_tag }
                : {}),
        },
        body: JSON.stringify({ language }),
    });
}

export function LanguageLink({
    language,
    href,
    className,
    children,
}: {
    language: Language;
    href: string;
    className?: string;
    children: ReactNode;
}) {
    function choose(event: MouseEvent<HTMLAnchorElement>) {
        if (
            event.button !== 0 ||
            event.metaKey ||
            event.ctrlKey ||
            event.shiftKey ||
            event.altKey
        )
            return;
        event.preventDefault();
        const follow = () => window.location.assign(href);
        void saveForSignedIn(language).then(follow, follow);
    }
    return (
        <a
            className={className}
            href={href}
            hrefLang={language}
            lang={language}
            onClick={choose}
        >
            {children}
        </a>
    );
}
