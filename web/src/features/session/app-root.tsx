"use client";
import { QueryClientProvider } from "@tanstack/react-query";
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
import { captureInstallPrompt } from "@/lib/install-prompt";
import { reloadAfter, reloadPage } from "@/lib/on-demand";
import { routes } from "@/lib/routes";
import { browserErrors } from "@/i18n/browser";
import {
    clearInvitationWith,
    confirmIdleSession,
    isCurrent,
    isHandoffFailure,
    loadSession,
    observedTagKey,
    savedCopyAnswered,
    savedCopyState,
    sessionOwner,
    sessionState,
    signOut,
    startSavedCopy,
    subscribeSavedFigures,
    useHandoffFailed,
    useSessionClock,
    useSessionState,
    useSessionUnconfirmed,
    type Session,
    type SignOutState,
} from "@/lib/session-client";
import { Brand } from "@/ui/brand";
import { useErrorMessage } from "@/ui/form";
import { LiveRegion } from "@/ui/live-region";
import { clearWorkspaceInvitation } from "@/features/workspace/workspace-invitation";
import { openProviderLanding } from "@/features/auth/provider-return";
import { useLanguageHandoff } from "./language-handoff";
import { SessionLanguage, useSessionLanguage } from "./session-language";
import { Notice } from "@/ui/ui";

if (typeof window !== "undefined") {
    captureInstallPrompt();
    startSavedCopy();
}
clearInvitationWith(clearWorkspaceInvitation);

export function AppRoot({ children }: { children: ReactNode }) {
    const [client] = useState(createQueryClient);
    const { session, failure, fail, clear, retry } = useSessionBootstrap();
    const { retired, signOut: leaving } = useSessionState();
    const unchecked = useHandoffFailed();
    const handoff = useLanguageHandoff(session);
    const language = useSessionLanguage(session, handoff.status !== "ready");
    const ready =
        session !== undefined &&
        handoff.status === "ready" &&
        language.status === "ready";
    useFocusSessionRead(ready && !unchecked);
    const restored = useRestoredDocument();
    const ending = retired !== undefined || restored;
    useDocumentReload(ending, retired === "otherOwner");
    const current = useCurrentSession(
        session,
        ready && !unchecked,
        fail,
        clear,
    );
    const unconfirmed = useSessionUnconfirmed();
    return (
        <QueryClientProvider client={client}>
            <SessionLanguage value={language}>
                <IdleSessionCheck />
                {ending ? (
                    <Bootstrap retry={retry} />
                ) : leaving.status !== "idle" ? (
                    <SigningOut state={leaving} />
                ) : unchecked ? (
                    <SavedFiguresUnchecked />
                ) : ready && current ? (
                    <QueryOwner value={sessionOwner(session)}>
                        <Screens
                            covered={unconfirmed && Boolean(session?.user)}
                        >
                            {children}
                        </Screens>
                    </QueryOwner>
                ) : language.status === "failed" ? (
                    <Bootstrap
                        failure={{ kind: "catalogue" }}
                        retry={reloadPage}
                    />
                ) : handoff.status === "failed" ? (
                    <Bootstrap
                        failure={{ kind: "language" }}
                        retry={handoff.retry}
                    />
                ) : (
                    <Bootstrap
                        failure={
                            failure === undefined
                                ? undefined
                                : { kind: "session", message: failure }
                        }
                        retry={retry}
                    />
                )}
            </SessionLanguage>
        </QueryClientProvider>
    );
}

function Bootstrap({
    failure,
    retry,
}: {
    failure?:
        | { kind: "catalogue" }
        | { kind: "language" }
        | { kind: "session"; message: string };
    retry: () => void;
}) {
    const language = useTranslations("Language");
    const errors = useTranslations("Errors");
    const error =
        failure?.kind === "catalogue"
            ? errors("unexpected")
            : failure?.kind === "language"
              ? language("failed")
              : failure?.message;
    return <LaunchCard failure={error ? { error, retry } : undefined} />;
}

// Shown until a read is handed over; a retry whose read fails shows that read's error instead.
function SavedFiguresUnchecked() {
    const bootstrap = useTranslations("Bootstrap");
    const [error, setError] = useState<string>();
    return (
        <LaunchCard
            failure={{
                error: error ?? bootstrap("savedFiguresUnchecked"),
                retry: () => {
                    setError(undefined);
                    loadSession().catch((failure: unknown) => {
                        if (!isHandoffFailure(failure))
                            setError(
                                displayMessage(
                                    failure,
                                    browserErrors()("unexpected"),
                                ),
                            );
                    });
                },
            }}
        />
    );
}

export function LaunchCard({
    failure,
}: {
    failure?: { error: string; retry: () => void };
}) {
    const t = useTranslations("Bootstrap");
    const page = useTranslations("AppError");
    return (
        <main className="auth-page">
            <div className="auth-card">
                {failure ? (
                    <>
                        <Brand surface="light" />
                        <h1>{t("title")}</h1>
                        <Notice error>{failure.error}</Notice>
                        <button className="button" onClick={failure.retry}>
                            {t("retry")}
                        </button>
                        <a className="text-button" href={routes.entry()}>
                            {page("start")}
                        </a>
                    </>
                ) : (
                    <>
                        <h1 className="launch-brand">
                            <Brand surface="light" />
                        </h1>
                        <div role="status">
                            <div
                                className="loading-indicator"
                                role="progressbar"
                                aria-label={t("loading")}
                            />
                        </div>
                    </>
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

// The reload runs after the ending document's screens and their leave listeners are gone and the copy answered: an answer reaches only its document.
function useDocumentReload(ending: boolean, signedIn: boolean) {
    useEffect(() => {
        if (!ending) return;
        if (signedIn) openProviderLanding();
        return reloadAfter(savedCopyAnswered());
    }, [ending, signedIn]);
}

// A document restored from the back-forward cache may show a session that ended meanwhile.
function useRestoredDocument() {
    const [restored, setRestored] = useState(false);
    useEffect(() => {
        const restore = (event: PageTransitionEvent) => {
            if (event.persisted) setRestored(true);
        };
        window.addEventListener("pageshow", restore);
        return () => window.removeEventListener("pageshow", restore);
    }, []);
    return restored;
}

function IdleSessionCheck() {
    const now = useSessionClock();
    useEffect(() => {
        if (document.visibilityState === "visible") confirmIdleSession();
    }, [now]);
    return null;
}

function Screens({
    covered,
    children,
}: {
    covered: boolean;
    children: ReactNode;
}) {
    return (
        <>
            {covered && <SessionUnconfirmed />}
            <div hidden={covered} inert={covered}>
                <LiveRegion>{children}</LiveRegion>
            </div>
        </>
    );
}

function useSavedFiguresShown() {
    const [shown, setShown] = useState(false);
    useEffect(() => {
        let active = true;
        const read = () =>
            void savedCopyState().then(({ state }) => {
                if (active) setShown(state === "on");
            });
        read();
        const unsubscribe = subscribeSavedFigures(read);
        return () => {
            active = false;
            unsubscribe();
        };
    }, []);
    return shown;
}

function SessionUnconfirmed() {
    const t = useTranslations("Session");
    const savedFigures = useSavedFiguresShown();
    return (
        <main className="auth-page">
            <div className="auth-card">
                <Brand surface="light" />
                <h1>{t("unconfirmed")}</h1>
                <p>{t("unconfirmedHelp")}</p>
                <button
                    className="button"
                    onClick={() => void loadSession().catch(() => {})}
                >
                    {t("retry")}
                </button>
                {savedFigures && (
                    <button className="button secondary" onClick={reloadPage}>
                        {t("showSavedFigures")}
                    </button>
                )}
            </div>
        </main>
    );
}

function SigningOut({
    state,
}: {
    state: Exclude<SignOutState, { status: "idle" }>;
}) {
    const t = useTranslations("Session");
    const errorMessage = useErrorMessage();
    return (
        <main className="auth-page">
            <div className="auth-card">
                <Brand surface="light" />
                <h1>{t("signingOut")}</h1>
                {state.status === "failed" ? (
                    <>
                        <Notice error>{errorMessage(state.error)}</Notice>
                        <button
                            className="button"
                            onClick={() =>
                                void signOut({
                                    keepInvitation: state.keepInvitation,
                                }).catch(() => {})
                            }
                        >
                            {t("retry")}
                        </button>
                    </>
                ) : (
                    <div
                        className="loading-indicator"
                        role="progressbar"
                        aria-label={t("signingOut")}
                    />
                )}
            </div>
        </main>
    );
}
