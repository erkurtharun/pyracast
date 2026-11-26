"use client";

import { useEffect, useRef, type MouseEvent, type ReactNode } from "react";
import type { Language } from "@/i18n/language";
import type { components } from "@/lib/schema";

type Session = components["schemas"]["SessionOutput"];
type PendingSave = { follow: boolean; stop: AbortController };

const saveLimitMs = 3_000;

async function saveForSignedIn(language: Language, stop: AbortSignal) {
    const signal = AbortSignal.any([stop, AbortSignal.timeout(saveLimitMs)]);
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

function onNextNavigation(listener: () => void, signal: AbortSignal) {
    const { navigation } = window as Window & { navigation?: EventTarget };
    navigation?.addEventListener("navigate", listener, { once: true, signal });
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
    const pending = useRef<PendingSave | null>(null);
    useEffect(() => () => pending.current?.stop.abort(), []);

    function choose(follow: boolean) {
        if (pending.current) {
            pending.current.follow ||= follow;
            return;
        }
        const save: PendingSave = { follow, stop: new AbortController() };
        pending.current = save;
        onNextNavigation(() => {
            save.follow = false;
        }, save.stop.signal);
        const settle = () => {
            const follows = save.follow && !save.stop.signal.aborted;
            save.stop.abort();
            pending.current = null;
            if (follows) window.location.assign(href);
        };
        void saveForSignedIn(language, save.stop.signal).then(settle, settle);
    }

    function chooseByClick(event: MouseEvent<HTMLAnchorElement>) {
        const browserOpens =
            event.button !== 0 ||
            event.metaKey ||
            event.ctrlKey ||
            event.shiftKey ||
            event.altKey;
        if (!browserOpens) event.preventDefault();
        choose(!browserOpens);
    }

    function chooseByMiddleButton(event: MouseEvent<HTMLAnchorElement>) {
        if (event.button === 1) choose(false);
    }

    return (
        <a
            className={className}
            href={href}
            hrefLang={language}
            lang={language}
            onAuxClick={chooseByMiddleButton}
            onClick={chooseByClick}
        >
            {children}
        </a>
    );
}
