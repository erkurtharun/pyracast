"use client";
import { QueryClientProvider, type QueryClient } from "@tanstack/react-query";
import { useTranslations } from "next-intl";
import {
    useCallback,
    useEffect,
    useState,
    useSyncExternalStore,
    type ReactNode,
} from "react";
import { displayMessage } from "@/lib/api/errors";
import { createQueryClient, QueryOwner } from "@/lib/api/query";
import { browserErrors } from "@/i18n/browser";
import {
    isCurrent,
    loadSession,
    observedTagKey,
    sessionOwner,
    sessionState,
    useSessionState,
    type Session,
} from "@/lib/session-client";
import { Brand } from "@/ui/brand";
import { LiveRegion } from "@/ui/live-region";
import { useLanguageHandoff } from "./language-handoff";
import { SessionLanguage } from "./session-language";
import { Notice } from "@/ui/ui";

export function AppRoot({ children }: { children: ReactNode }) {
    const [client] = useState(createQueryClient);
    const { session, failure, fail, clear, retry } = useSessionBootstrap();
    const handoff = useLanguageHandoff(session);
    const ready = session !== undefined && handoff.status === "ready";
    useFocusSessionRead(ready);
    const current = useCurrentSession(session, ready, fail, clear);
    const owner = sessionOwner(session);
    useForeignCacheRemoval(client, owner);
    return (
        <QueryClientProvider client={client}>
            <SessionLanguage language={session?.language}>
                <LiveRegion>
                    {ready && current ? (
                        <QueryOwner value={owner}>{children}</QueryOwner>
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

// A failure belongs to the session held when it happened; a session adopted after it hides it.
function useSessionBootstrap() {
    const [attempt, setAttempt] = useState(0);
    const [failure, setFailure] = useState<{
        message: string;
        session?: Session;
    }>();
    const { session } = useSessionState();
    const fail = useCallback((error: unknown) => {
        setFailure({
            message: displayMessage(error, browserErrors()("unexpected")),
            session: sessionState().session,
        });
    }, []);
    const clear = useCallback(() => {
        setFailure(undefined);
    }, []);
    useEffect(() => {
        let active = true;
        loadSession().catch((error: unknown) => {
            if (active) fail(error);
        });
        return () => {
            active = false;
        };
    }, [attempt, fail]);
    return {
        session,
        failure:
            failure && failure.session === session
                ? failure.message
                : undefined,
        fail,
        clear,
        retry: () => {
            clear();
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

function subscribeObservedTag(listener: () => void) {
    const changed = (event: StorageEvent) => {
        if (event.key === observedTagKey) listener();
    };
    window.addEventListener("storage", changed);
    return () => window.removeEventListener("storage", changed);
}

// Another tab observed a session other than this tab's: its screens stay hidden until a read
// adopts the session the browser holds, and a failed read offers the retry of a first read.
function useCurrentSession(
    session: Session | undefined,
    enabled: boolean,
    fail: (error: unknown) => void,
    clear: () => void,
) {
    const current = useSyncExternalStore(
        subscribeObservedTag,
        () => isCurrent(session),
        () => true,
    );
    useEffect(() => {
        if (!enabled) return;
        clear();
        if (current) return;
        let active = true;
        loadSession().catch((error: unknown) => {
            if (active) fail(error);
        });
        return () => {
            active = false;
        };
    }, [enabled, current, fail, clear]);
    return current;
}

function useForeignCacheRemoval(
    client: QueryClient,
    owner: string | undefined,
) {
    useEffect(() => {
        client.removeQueries({
            predicate: (query) => query.queryKey[1] !== owner,
        });
    }, [client, owner]);
}
