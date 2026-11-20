"use client";
import { QueryClientProvider, type QueryClient } from "@tanstack/react-query";
import { useTranslations } from "next-intl";
import { useEffect, useState, type ReactNode } from "react";
import { displayMessage } from "@/lib/api/errors";
import { createQueryClient, QueryOwner } from "@/lib/api/query";
import { browserErrors } from "@/i18n/browser";
import { loadSession, useSessionState } from "@/lib/session-client";
import { Brand } from "@/ui/brand";
import { LiveRegion } from "@/ui/live-region";
import { useLanguageHandoff } from "./language-handoff";
import { SessionLanguage } from "./session-language";
import { Notice } from "@/ui/ui";

export function AppRoot({ children }: { children: ReactNode }) {
    const [client] = useState(createQueryClient);
    const { session, failure, retry } = useSessionBootstrap();
    const handoff = useLanguageHandoff(session);
    useFocusSessionRead(session !== undefined && handoff.status === "ready");
    const user = session?.user?.id;
    useForeignCacheRemoval(client, user);
    return (
        <QueryClientProvider client={client}>
            <SessionLanguage language={session?.language}>
                <LiveRegion>
                    {session && handoff.status === "ready" ? (
                        <QueryOwner value={user}>{children}</QueryOwner>
                    ) : (
                        <Bootstrap
                            languageFailed={handoff.status === "failed"}
                            failure={failure}
                            retry={
                                handoff.status === "failed"
                                    ? handoff.retry
                                    : retry
                            }
                        />
                    )}
                </LiveRegion>
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
                        aria-label={t("loadingScreen")}
                    />
                )}
            </div>
        </main>
    );
}

function useSessionBootstrap() {
    const [attempt, setAttempt] = useState(0);
    const [failure, setFailure] = useState<string>();
    const { session } = useSessionState();
    useEffect(() => {
        let active = true;
        loadSession().catch((error: unknown) => {
            if (active)
                setFailure(
                    displayMessage(error, browserErrors()("unexpected")),
                );
        });
        return () => {
            active = false;
        };
    }, [attempt]);
    return {
        session,
        failure,
        retry: () => {
            setFailure(undefined);
            setAttempt((value) => value + 1);
        },
    };
}

// A read during a pending or failed language hand-off would adopt a session behind its retry screen.
function useFocusSessionRead(enabled: boolean) {
    useEffect(() => {
        if (!enabled) return;
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
    }, [enabled]);
}

function useForeignCacheRemoval(client: QueryClient, user: number | undefined) {
    useEffect(() => {
        client.removeQueries({
            predicate: (query) => query.queryKey[1] !== user,
        });
    }, [client, user]);
}
