"use client";
import { QueryClientProvider, type QueryClient } from "@tanstack/react-query";
import { useTranslations } from "next-intl";
import { usePathname, useSearchParams } from "next/navigation";
import { useEffect, useState, type ReactNode } from "react";
import { generationKey } from "@/lib/api/generation";
import { sessionCurrent } from "@/lib/api/lane";
import { createQueryClient, QueryOwner } from "@/lib/api/query";
import {
    checkSession,
    loadSession,
    useSessionState,
} from "@/lib/session-client";
import { Brand } from "./brand";
import { useLanguageHandoff } from "./language-handoff";
import { SessionLanguage } from "./session-language";
import { Notice } from "./ui";

export function AppRoot({ children }: { children: ReactNode }) {
    const [client] = useState(createQueryClient);
    const { session, current, failure } = useSessionBootstrap();
    const handoff = useLanguageHandoff(session);
    const user = session?.user?.id;
    useForeignCacheRemoval(client, user);
    useEffect(() => {
        // A new session, also of the same account, starts with an empty cache.
        if (!current) client.clear();
    }, [client, current]);
    return (
        <QueryClientProvider client={client}>
            <SessionLanguage
                language={
                    handoff.status === "failed"
                        ? handoff.language
                        : session?.language
                }
            >
                {session && current && handoff.status === "ready" ? (
                    <QueryOwner value={user}>{children}</QueryOwner>
                ) : (
                    <Bootstrap
                        languageFailed={handoff.status === "failed"}
                        failure={failure}
                        retry={
                            handoff.status === "failed"
                                ? handoff.retry
                                : () => void loadSession().catch(() => {})
                        }
                    />
                )}
            </SessionLanguage>
        </QueryClientProvider>
    );
}

function Bootstrap({
    languageFailed,
    failure,
    retry,
}: {
    languageFailed: boolean;
    failure?: string;
    retry: () => void;
}) {
    const t = useTranslations("Dashboard");
    const language = useTranslations("Language");
    const error = languageFailed ? language("failed") : failure;
    return (
        <main className="auth-page">
            <div className="auth-card">
                <Brand />
                {error ? (
                    <>
                        <Notice error>{error}</Notice>
                        <button className="button" onClick={retry}>
                            {t("retry")}
                        </button>
                    </>
                ) : (
                    <div
                        className="loading-indicator"
                        role="progressbar"
                        aria-label="Pyracast"
                    />
                )}
            </div>
        </main>
    );
}

// The tab compares the browser's session generation on every navigation, storage change, focus and
// return to visibility, and at render, so no screen of an earlier session is shown in between.
function useSessionBootstrap() {
    const { session, current, failure } = useSessionState();
    const pathname = usePathname();
    const search = useSearchParams().toString();
    useEffect(() => {
        loadSession().catch(() => {});
    }, []);
    useEffect(() => {
        checkSession();
    }, [pathname, search]);
    useEffect(() => {
        const changed = (event: StorageEvent) => {
            if (event.key === null || event.key === generationKey)
                checkSession();
        };
        window.addEventListener("storage", changed);
        return () => window.removeEventListener("storage", changed);
    }, []);
    useEffect(() => {
        if (!session) return;
        const refresh = () => {
            if (document.visibilityState !== "visible") return;
            checkSession();
            loadSession().catch(() => {});
        };
        window.addEventListener("focus", refresh);
        document.addEventListener("visibilitychange", refresh);
        return () => {
            window.removeEventListener("focus", refresh);
            document.removeEventListener("visibilitychange", refresh);
        };
    }, [session]);
    return { session, current: current && sessionCurrent(), failure };
}

function useForeignCacheRemoval(client: QueryClient, user: number | undefined) {
    useEffect(() => {
        client.removeQueries({
            predicate: (query) => query.queryKey[1] !== user,
        });
    }, [client, user]);
}
