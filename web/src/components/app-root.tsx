"use client";
import { QueryClientProvider, type QueryClient } from "@tanstack/react-query";
import { useTranslations } from "next-intl";
import { useEffect, useState, type ReactNode } from "react";
import { createQueryClient, QueryOwner } from "@/lib/api/query";
import { loadSession, useSessionState } from "@/lib/session-client";
import { Brand } from "./brand";
import { useLanguageHandoff } from "./language-handoff";
import { SessionLanguage } from "./session-language";
import { Notice } from "./ui";

export function AppRoot({ children }: { children: ReactNode }) {
    const [client] = useState(createQueryClient);
    const { session, unconfirmed, failure, retry } = useSessionBootstrap();
    const handoff = useLanguageHandoff(session);
    const user = session?.user?.id;
    useForeignCacheRemoval(client, user);
    return (
        <QueryClientProvider client={client}>
            <SessionLanguage
                language={
                    handoff.status === "failed"
                        ? handoff.language
                        : session?.language
                }
            >
                {session && !unconfirmed && handoff.status === "ready" ? (
                    <QueryOwner value={user}>{children}</QueryOwner>
                ) : (
                    <Bootstrap
                        languageFailed={handoff.status === "failed"}
                        failure={failure}
                        retry={
                            handoff.status === "failed" ? handoff.retry : retry
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

function useSessionBootstrap() {
    const [attempt, setAttempt] = useState(0);
    const { session, signals, unconfirmed } = useSessionState();
    const read = `${attempt}:${signals}`;
    const [failure, setFailure] = useState<{ read: string; message: string }>();
    useEffect(() => {
        let active = true;
        loadSession().catch((error: Error) => {
            if (active) setFailure({ read, message: error.message });
        });
        return () => {
            active = false;
        };
    }, [read]);
    useEffect(() => {
        if (!session) return;
        const refresh = () => {
            if (document.visibilityState === "visible")
                loadSession().catch(() => {});
        };
        window.addEventListener("focus", refresh);
        document.addEventListener("visibilitychange", refresh);
        return () => {
            window.removeEventListener("focus", refresh);
            document.removeEventListener("visibilitychange", refresh);
        };
    }, [session]);
    return {
        session,
        unconfirmed,
        failure: failure?.read === read ? failure.message : undefined,
        retry: () => setAttempt((value) => value + 1),
    };
}

function useForeignCacheRemoval(client: QueryClient, user: number | undefined) {
    useEffect(() => {
        client.removeQueries({
            predicate: (query) => query.queryKey[1] !== user,
        });
    }, [client, user]);
}
