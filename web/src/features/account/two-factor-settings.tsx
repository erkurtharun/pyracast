"use client";

import { useLocale, useTranslations } from "next-intl";
import { useCallback, useEffect, useRef, useState } from "react";
import { loadSession } from "@/lib/session-client";
import { ApiError } from "@/lib/api/errors";
import {
    authRequest,
    type AuthenticatorRecord,
    type AuthResult,
} from "@/lib/auth-api";
import {
    getReauthMode,
    refreshExpiredSession,
} from "./security-reauthentication";
import { AuthenticatorSetup } from "./authenticator-setup";
import { RecoveryCodes } from "./recovery-codes";
import { ConfirmStep } from "./two-factor/confirm-step";
import { EnabledPanel } from "./two-factor/enabled-panel";
import { LoadError } from "./two-factor/load-error";
import {
    parseAuthenticators,
    type LocalFeedback,
    type PendingOperation,
    type RecoveryCounts,
    type StatusNotice,
    type TwoFactorView,
} from "./two-factor/model";
import { ReauthStep } from "./two-factor/reauth-step";
import { RecoveryViewed } from "./two-factor/recovery-viewed";
import { TwoFactorStatusTag } from "./two-factor/status-tag";

export function TwoFactorSettings({
    mfaRequired,
    hasPassword,
    email,
}: {
    mfaRequired: boolean;
    hasPassword: boolean;
    email: string;
}) {
    const t = useTranslations("Security");
    const locale = useLocale();

    const [view, setView] = useState<TwoFactorView>({ kind: "loading" });
    const [loadCount, setLoadCount] = useState(0);
    const [pendingAction, setPendingAction] = useState<string | null>(null);
    const [setupCode, setSetupCode] = useState("");
    const [statusNotice, setStatusNotice] = useState<StatusNotice>();
    const [errorFeedback, setErrorFeedback] = useState<LocalFeedback>();
    const [codeFieldError, setCodeFieldError] = useState<LocalFeedback>();

    const mountedRef = useRef(true);
    const pendingRef = useRef(false);
    const hasInteractedRef = useRef(false);
    const stepHeadingRef = useRef<HTMLHeadingElement>(null);
    const panelHeadingRef = useRef<HTMLHeadingElement>(null);
    const errorRef = useRef<HTMLDivElement>(null);
    const activeBlobUrlsRef = useRef<Set<{ url: string; timer: number }>>(
        new Set(),
    );

    const clearTransientFeedback = useCallback(() => {
        setStatusNotice(undefined);
        setErrorFeedback(undefined);
        setCodeFieldError(undefined);
    }, []);

    const focusStepHeading = useCallback(() => {
        if (!hasInteractedRef.current) return;
        requestAnimationFrame(() => {
            (stepHeadingRef.current ?? panelHeadingRef.current)?.focus();
        });
    }, []);

    const focusError = useCallback(() => {
        requestAnimationFrame(() => {
            errorRef.current?.focus();
        });
    }, []);

    const fetchAuthenticators = useCallback(async (): Promise<{
        totpEnabled: boolean;
        recovery?: RecoveryCounts;
    }> => {
        const result = await authRequest<AuthenticatorRecord[]>(
            "account/authenticators",
            "GET",
        );
        if (result.status === 401) {
            if (!result.meta?.is_authenticated) {
                await refreshExpiredSession(t("sessionExpired"));
            }
            throw new ApiError(t("confirmIdentityFirst"));
        }
        return parseAuthenticators(result.data);
    }, [t]);

    useEffect(() => {
        mountedRef.current = true;
        pendingRef.current = false;
        const blobEntries = activeBlobUrlsRef.current;
        let active = true;

        authRequest<AuthenticatorRecord[]>("account/authenticators", "GET")
            .then(async (result) => {
                if (!active || !mountedRef.current) return;
                if (result.status === 401) {
                    if (!result.meta?.is_authenticated) {
                        await loadSession();
                        if (!active || !mountedRef.current) return;
                        setView({
                            kind: "loadError",
                            reason: "sessionExpired",
                        });
                        return;
                    }
                    setView({
                        kind: "loadError",
                        reason: "identityRequired",
                    });
                    return;
                }
                const { totpEnabled, recovery } = parseAuthenticators(
                    result.data,
                );
                setView(
                    totpEnabled
                        ? { kind: "enabled", recovery }
                        : { kind: "disabled" },
                );
                focusStepHeading();
            })
            .catch((err: unknown) => {
                if (!active || !mountedRef.current) return;
                setView({
                    kind: "loadError",
                    message: err instanceof Error ? err.message : undefined,
                });
            });

        return () => {
            active = false;
            mountedRef.current = false;
            pendingRef.current = false;
            for (const entry of blobEntries) {
                window.clearTimeout(entry.timer);
                URL.revokeObjectURL(entry.url);
            }
            blobEntries.clear();
        };
    }, [loadCount, focusStepHeading]);

    async function handleAuthenticated401(
        result: AuthResult<unknown>,
        operation: PendingOperation,
        returnView: TwoFactorView,
        afterActivation: boolean,
        isReplay: boolean,
    ): Promise<boolean> {
        if (!mountedRef.current) return true;
        if (result.status !== 401) return false;
        if (!result.meta?.is_authenticated) {
            await refreshExpiredSession(t("sessionExpired"));
        }
        if (isReplay) {
            throw new ApiError(t("reauthStillRequired"), { status: 401 });
        }
        if (afterActivation) {
            setStatusNotice(undefined);
        }
        setView({
            kind: "reauth",
            mode: getReauthMode(result, hasPassword),
            operation,
            returnView,
            afterActivation,
        });
        focusStepHeading();
        return true;
    }

    async function runRecoveryFetch(
        fallbackRecovery: RecoveryCounts | undefined,
        afterActivation: boolean,
        isReplay: boolean,
    ) {
        const result = await authRequest(
            "account/authenticators/recovery-codes",
            "GET",
        );
        const returnView: TwoFactorView = {
            kind: "enabled",
            recovery: fallbackRecovery,
        };
        if (
            await handleAuthenticated401(
                result,
                { type: "getRecovery", afterActivation },
                returnView,
                afterActivation,
                isReplay,
            )
        ) {
            return;
        }
        if (!mountedRef.current) return;
        if (afterActivation) setStatusNotice(undefined);
        const unusedCodes = result.data?.unused_codes;
        const recovery =
            result.data?.unused_code_count !== undefined &&
            result.data.total_code_count !== undefined
                ? {
                      unused: result.data.unused_code_count,
                      total: result.data.total_code_count,
                  }
                : fallbackRecovery;
        if (Array.isArray(unusedCodes) && unusedCodes.length > 0) {
            setView({
                kind: "recoverySaving",
                recovery,
                codes: unusedCodes,
            });
        } else {
            setView({
                kind: "recoveryViewed",
                recovery,
            });
        }
        focusStepHeading();
    }

    async function executeOperation(
        operation: PendingOperation,
        options: { isReplay: boolean; fallbackView: TwoFactorView },
    ) {
        if (!mountedRef.current || pendingRef.current) return;
        hasInteractedRef.current = true;
        pendingRef.current = true;
        setPendingAction(operation.type);
        clearTransientFeedback();

        try {
            switch (operation.type) {
                case "startSetup": {
                    if (mfaRequired) return;
                    const result = await authRequest(
                        "account/authenticators/totp",
                        "GET",
                    );
                    if (
                        await handleAuthenticated401(
                            result,
                            operation,
                            options.fallbackView,
                            false,
                            options.isReplay,
                        )
                    ) {
                        return;
                    }
                    if (result.status === 200) {
                        const { totpEnabled, recovery } =
                            await fetchAuthenticators();
                        if (!mountedRef.current) return;
                        setView(
                            totpEnabled
                                ? { kind: "enabled", recovery }
                                : { kind: "disabled" },
                        );
                        focusStepHeading();
                        return;
                    }
                    const secret = result.meta?.secret;
                    const totpUrl = result.meta?.totp_url;
                    if (!secret || !totpUrl) {
                        throw new ApiError(t("setupUnavailable"));
                    }
                    if (!mountedRef.current) return;
                    setSetupCode("");
                    setView({ kind: "enrolling", secret, totpUrl });
                    focusStepHeading();
                    return;
                }

                case "activateTotp": {
                    if (mfaRequired) return;
                    const result = await authRequest(
                        "account/authenticators/totp",
                        "POST",
                        { code: operation.code },
                    );
                    if (
                        await handleAuthenticated401(
                            result,
                            operation,
                            options.fallbackView,
                            false,
                            options.isReplay,
                        )
                    ) {
                        return;
                    }
                    if (!mountedRef.current) return;
                    setSetupCode("");

                    let recovery: RecoveryCounts | undefined;
                    try {
                        const refreshed = await fetchAuthenticators();
                        recovery = refreshed.recovery;
                    } catch {
                        recovery = undefined;
                    }
                    if (!mountedRef.current) return;
                    setStatusNotice("authenticatorActivated");

                    try {
                        await runRecoveryFetch(recovery, true, false);
                    } catch (recoveryErr) {
                        if (!mountedRef.current) return;
                        setView({ kind: "enabled", recovery });
                        setErrorFeedback({
                            text:
                                recoveryErr instanceof Error
                                    ? recoveryErr.message
                                    : t("loadFailed"),
                            locale,
                        });
                        focusError();
                    }
                    return;
                }

                case "getRecovery": {
                    const recovery =
                        "recovery" in options.fallbackView
                            ? options.fallbackView.recovery
                            : undefined;
                    await runRecoveryFetch(
                        recovery,
                        operation.afterActivation,
                        options.isReplay,
                    );
                    return;
                }

                case "replaceRecovery": {
                    const result = await authRequest(
                        "account/authenticators/recovery-codes",
                        "POST",
                        {},
                    );
                    if (
                        await handleAuthenticated401(
                            result,
                            operation,
                            options.fallbackView,
                            false,
                            options.isReplay,
                        )
                    ) {
                        return;
                    }
                    if (!mountedRef.current) return;
                    const unusedCodes = result.data?.unused_codes ?? [];
                    let recovery: RecoveryCounts | undefined =
                        "recovery" in options.fallbackView
                            ? options.fallbackView.recovery
                            : undefined;
                    try {
                        const refreshed = await fetchAuthenticators();
                        recovery = refreshed.recovery ?? recovery;
                    } catch {
                        if (unusedCodes.length > 0) {
                            recovery = {
                                unused: unusedCodes.length,
                                total: unusedCodes.length,
                            };
                        }
                    }
                    if (!mountedRef.current) return;
                    setStatusNotice("codesReplaced");
                    if (unusedCodes.length > 0) {
                        setView({
                            kind: "recoverySaving",
                            recovery,
                            codes: unusedCodes,
                        });
                    } else {
                        setView({
                            kind: "recoveryViewed",
                            recovery,
                        });
                    }
                    focusStepHeading();
                    return;
                }

                case "removeTotp": {
                    if (mfaRequired) return;
                    const result = await authRequest(
                        "account/authenticators/totp",
                        "DELETE",
                    );
                    if (
                        await handleAuthenticated401(
                            result,
                            operation,
                            options.fallbackView,
                            false,
                            options.isReplay,
                        )
                    ) {
                        return;
                    }
                    if (!mountedRef.current) return;
                    setSetupCode("");
                    setStatusNotice("authenticatorRemoved");
                    setView({ kind: "disabled" });
                    focusStepHeading();
                    return;
                }
            }
        } catch (err) {
            if (!mountedRef.current) return;
            if (options.isReplay) {
                setView(options.fallbackView);
            }
            if (
                (!(err instanceof ApiError) ||
                    err.status >= 500 ||
                    err.status < 400) &&
                (operation.type === "activateTotp" ||
                    operation.type === "removeTotp" ||
                    operation.type === "replaceRecovery")
            ) {
                try {
                    const current = await fetchAuthenticators();
                    if (!mountedRef.current) return;
                    if (current.totpEnabled) {
                        setSetupCode("");
                        setView({
                            kind: "enabled",
                            recovery: current.recovery,
                        });
                    } else if (operation.type !== "activateTotp") {
                        setView({ kind: "disabled" });
                    }
                } catch {
                    if (!mountedRef.current) return;
                    setView({ kind: "loadError" });
                    return;
                }
            }
            const message =
                err instanceof Error ? err.message : t("loadFailed");
            setErrorFeedback({ text: message, locale });
            if (err instanceof ApiError && err.fields.code) {
                setCodeFieldError({ text: err.fields.code, locale });
            }
            focusError();
        } finally {
            if (mountedRef.current) {
                pendingRef.current = false;
                setPendingAction(null);
            }
        }
    }

    async function copyToClipboard(text: string, notice: StatusNotice) {
        setErrorFeedback(undefined);
        try {
            if (!navigator.clipboard?.writeText) {
                throw new Error(t("copyFailed"));
            }
            await navigator.clipboard.writeText(text);
            if (!mountedRef.current) return;
            setStatusNotice(notice);
        } catch {
            if (!mountedRef.current) return;
            setErrorFeedback({ text: t("copyFailed"), locale });
            focusError();
        }
    }

    function downloadCodes(codes: string[]) {
        setErrorFeedback(undefined);
        const blob = new Blob([`${codes.join("\n")}\n`], {
            type: "text/plain;charset=utf-8",
        });
        const url = URL.createObjectURL(blob);
        const link = document.createElement("a");
        link.href = url;
        link.download = "pyracast-recovery-codes.txt";
        document.body.append(link);
        link.click();
        link.remove();
        const entry = {
            url,
            timer: window.setTimeout(() => {
                URL.revokeObjectURL(url);
                activeBlobUrlsRef.current.delete(entry);
            }, 1000),
        };
        activeBlobUrlsRef.current.add(entry);
        setStatusNotice("codesDownloaded");
    }

    function showStep(next: TwoFactorView) {
        hasInteractedRef.current = true;
        clearTransientFeedback();
        setView(next);
        focusStepHeading();
    }

    const activeError =
        errorFeedback?.locale === locale ? errorFeedback.text : undefined;
    const activeCodeError =
        codeFieldError?.locale === locale ? codeFieldError.text : undefined;
    const isBusy = pendingAction !== null || view.kind === "loading";

    return (
        <section className="panel narrow" aria-busy={isBusy || undefined}>
            <div className="mfa-header">
                <h2 ref={panelHeadingRef} tabIndex={-1}>
                    {t("twoFactor")}
                </h2>
                <TwoFactorStatusTag view={view} mfaRequired={mfaRequired} />
            </div>
            {view.kind === "disabled" && <p>{t("authenticatorHelp")}</p>}

            {statusNotice && (
                <div className="notice" role="status">
                    {t(statusNotice)}
                </div>
            )}

            {activeError && (
                <div
                    ref={errorRef}
                    className="notice error"
                    role="alert"
                    tabIndex={-1}
                >
                    {activeError}
                </div>
            )}

            {view.kind === "loading" && (
                <div role="status">
                    <p>{t("loading")}</p>
                    <div className="loading-indicator" aria-hidden="true" />
                </div>
            )}

            {view.kind === "loadError" && (
                <LoadError
                    view={view}
                    busy={isBusy}
                    onRetry={() => {
                        hasInteractedRef.current = true;
                        clearTransientFeedback();
                        setView({ kind: "loading" });
                        setLoadCount((count) => count + 1);
                    }}
                />
            )}

            {view.kind === "disabled" && (
                <div className="mfa-actions">
                    {mfaRequired ? (
                        <div className="notice" role="status">
                            {t("policySetupUnavailable")}
                        </div>
                    ) : (
                        <button
                            className="button"
                            type="button"
                            disabled={isBusy}
                            onClick={() =>
                                void executeOperation(
                                    { type: "startSetup" },
                                    { isReplay: false, fallbackView: view },
                                )
                            }
                        >
                            {pendingAction === "startSetup"
                                ? t("working")
                                : t("setupAuthenticator")}
                        </button>
                    )}
                </div>
            )}

            {view.kind === "enrolling" && (
                <AuthenticatorSetup
                    headingRef={stepHeadingRef}
                    secret={view.secret}
                    totpUrl={view.totpUrl}
                    code={setupCode}
                    onCodeChange={setSetupCode}
                    error={activeCodeError}
                    busy={isBusy}
                    verifying={pendingAction === "activateTotp"}
                    onCopyKey={() =>
                        void copyToClipboard(view.secret, "setupKeyCopied")
                    }
                    onActivate={() => {
                        const trimmed = setupCode.trim();
                        if (!trimmed) {
                            setCodeFieldError({ text: t("enterCode"), locale });
                            return;
                        }
                        void executeOperation(
                            { type: "activateTotp", code: trimmed },
                            { isReplay: false, fallbackView: view },
                        );
                    }}
                    onCancel={() => {
                        hasInteractedRef.current = true;
                        setSetupCode("");
                        clearTransientFeedback();
                        setView({ kind: "disabled" });
                        focusStepHeading();
                    }}
                />
            )}

            {view.kind === "enabled" && (
                <EnabledPanel
                    recovery={view.recovery}
                    mfaRequired={mfaRequired}
                    busy={isBusy}
                    loadingRecovery={pendingAction === "getRecovery"}
                    onShowRecovery={() =>
                        void executeOperation(
                            { type: "getRecovery", afterActivation: false },
                            { isReplay: false, fallbackView: view },
                        )
                    }
                    onReplace={() =>
                        showStep({
                            kind: "confirmReplace",
                            recovery: view.recovery,
                        })
                    }
                    onRemove={() =>
                        showStep({
                            kind: "confirmRemove",
                            recovery: view.recovery,
                        })
                    }
                />
            )}

            {view.kind === "recoveryViewed" && (
                <RecoveryViewed
                    headingRef={stepHeadingRef}
                    recovery={view.recovery}
                    busy={isBusy}
                    onReplace={() =>
                        showStep({
                            kind: "confirmReplace",
                            recovery: view.recovery,
                        })
                    }
                    onBack={() =>
                        showStep({ kind: "enabled", recovery: view.recovery })
                    }
                />
            )}

            {view.kind === "recoverySaving" && (
                <RecoveryCodes
                    headingRef={stepHeadingRef}
                    codes={view.codes}
                    busy={isBusy}
                    onCopy={() =>
                        void copyToClipboard(
                            view.codes.join("\n"),
                            "codesCopied",
                        )
                    }
                    onDownload={() => downloadCodes(view.codes)}
                    onAcknowledge={() => {
                        hasInteractedRef.current = true;
                        setErrorFeedback(undefined);
                        setStatusNotice("recoverySavedAck");
                        setView({ kind: "enabled", recovery: view.recovery });
                        focusStepHeading();
                    }}
                />
            )}

            {view.kind === "confirmReplace" && (
                <ConfirmStep
                    headingRef={stepHeadingRef}
                    action="replace"
                    busy={isBusy}
                    working={pendingAction === "replaceRecovery"}
                    onConfirm={() =>
                        void executeOperation(
                            { type: "replaceRecovery" },
                            { isReplay: false, fallbackView: view },
                        )
                    }
                    onCancel={() =>
                        showStep({ kind: "enabled", recovery: view.recovery })
                    }
                />
            )}

            {view.kind === "confirmRemove" && !mfaRequired && (
                <ConfirmStep
                    headingRef={stepHeadingRef}
                    action="remove"
                    busy={isBusy}
                    working={pendingAction === "removeTotp"}
                    onConfirm={() =>
                        void executeOperation(
                            { type: "removeTotp" },
                            { isReplay: false, fallbackView: view },
                        )
                    }
                    onCancel={() =>
                        showStep({ kind: "enabled", recovery: view.recovery })
                    }
                />
            )}

            {view.kind === "reauth" && (
                <ReauthStep
                    headingRef={stepHeadingRef}
                    view={view}
                    email={email}
                    busy={isBusy}
                    onCancel={() => showStep(view.returnView)}
                    onVerified={async () => {
                        if (view.operation.type === "activateTotp") {
                            setSetupCode("");
                            setView(view.returnView);
                            focusStepHeading();
                            return;
                        }
                        await executeOperation(view.operation, {
                            isReplay: true,
                            fallbackView: view.returnView,
                        });
                    }}
                />
            )}
        </section>
    );
}
