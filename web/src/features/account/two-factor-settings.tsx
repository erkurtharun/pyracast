"use client";

import { useLocale, useTranslations } from "next-intl";
import {
    useCallback,
    useEffect,
    useEffectEvent,
    useRef,
    useState,
} from "react";
import { ApiError, displayMessage, UnconfirmedError } from "@/lib/api/errors";
import {
    authRequest,
    type AuthenticatorRecord,
    type AuthResult,
} from "@/lib/auth-api";
import { refreshExpiredSession } from "./security-reauthentication";
import { StepUpPrompt, stepUpMode, useStepUp } from "./step-up";
import { AuthenticatorSetup } from "./authenticator-setup";
import { RecoveryCodes } from "./recovery-codes";
import { ConfirmStep } from "./two-factor/confirm-step";
import { EnabledPanel } from "./two-factor/enabled-panel";
import { LoadError } from "./two-factor/load-error";
import {
    parseAuthenticators,
    type Facts,
    type StatusNotice,
    type TwoFactorView,
} from "./two-factor/model";
import { TwoFactorStatusTag } from "./two-factor/status-tag";
import { UnconfirmedTitle, useWriteErrorMessage } from "@/ui/form";

type Feedback = { text: string; locale: string } | { lost: UnconfirmedError };
type Action = "setup" | "activate" | "replace" | "remove";

// The server answered and changed nothing.
function refused(failure: unknown) {
    return (
        failure instanceof ApiError &&
        failure.status >= 400 &&
        failure.status < 500
    );
}

export function TwoFactorSettings({
    mfaRequired,
    email,
}: {
    mfaRequired: boolean;
    email: string;
}) {
    const t = useTranslations("Security");
    const locale = useLocale();
    const stepUp = useStepUp();
    const [view, setView] = useState<TwoFactorView>({ kind: "loading" });
    const [loadCount, setLoadCount] = useState(0);
    const [running, setRunning] = useState<Action>();
    const [setupCode, setSetupCode] = useState("");
    const [notice, setNotice] = useState<StatusNotice>();
    const [error, setError] = useState<Feedback>();
    const [codeError, setCodeError] = useState<{
        text: string;
        locale: string;
    }>();
    const writeErrorMessage = useWriteErrorMessage();
    const stepHeadingRef = useRef<HTMLHeadingElement>(null);
    const errorRef = useRef<HTMLDivElement>(null);
    const inFlight = useRef(false);
    const blobUrls = useRef<Set<{ url: string; timer: number }>>(new Set());

    const show = useCallback((next: TwoFactorView) => {
        setView(next);
        requestAnimationFrame(() => stepHeadingRef.current?.focus());
    }, []);

    const fail = useCallback(
        (message: string, failure?: unknown) => {
            setError(
                failure instanceof UnconfirmedError
                    ? { lost: failure }
                    : { text: message, locale },
            );
            requestAnimationFrame(() => errorRef.current?.focus());
        },
        [locale],
    );

    async function readFacts() {
        const result = await authRequest<AuthenticatorRecord[]>(
            "account/authenticators",
            "GET",
        );
        if (result.status === 401)
            await refreshExpiredSession(t("sessionExpired"));
        return parseAuthenticators(result.data);
    }

    const loadFacts = useEffectEvent(readFacts);

    useEffect(() => {
        let active = true;
        const urls = blobUrls.current;
        loadFacts()
            .then(({ totpEnabled, facts }) => {
                if (active)
                    setView(
                        totpEnabled
                            ? { kind: "enabled", facts }
                            : { kind: "disabled" },
                    );
            })
            .catch((failure: unknown) => {
                if (active)
                    setView({
                        kind: "loadError",
                        message: displayMessage(failure, ""),
                    });
            });
        return () => {
            active = false;
            for (const entry of urls) {
                window.clearTimeout(entry.timer);
                URL.revokeObjectURL(entry.url);
            }
            urls.clear();
        };
    }, [loadCount]);

    // Codes shown once must reach the screen even when the list cannot be read after them.
    const currentFacts = () =>
        readFacts().then(
            ({ facts }) => facts,
            (): Facts => ({}),
        );

    async function confirmedFirst(
        result: AuthResult<unknown>,
        resume: () => Promise<void>,
    ) {
        if (result.status !== 401) return false;
        const mode = stepUpMode(result);
        if (!mode) await refreshExpiredSession(t("sessionExpired"));
        else stepUp.ask(mode, resume);
        return true;
    }

    // A change whose answer was lost may have committed: the list says what the account holds.
    async function reconcile(action: Action) {
        try {
            const { totpEnabled, facts } = await readFacts();
            if (totpEnabled) {
                setSetupCode("");
                setView({ kind: "enabled", facts });
            } else if (action !== "activate") setView({ kind: "disabled" });
        } catch {
            setView({ kind: "loadError" });
        }
    }

    async function run(action: Action, work: () => Promise<void>) {
        if (inFlight.current) return;
        inFlight.current = true;
        setRunning(action);
        setNotice(undefined);
        setError(undefined);
        setCodeError(undefined);
        try {
            await work();
        } catch (failure) {
            if (failure instanceof ApiError && failure.fields.code)
                setCodeError({ text: failure.fields.code, locale });
            fail(displayMessage(failure, t("loadFailed")), failure);
            if (action !== "setup" && !refused(failure))
                await reconcile(action);
        } finally {
            inFlight.current = false;
            setRunning(undefined);
        }
    }

    const startSetup = (): Promise<void> =>
        run("setup", async () => {
            const result = await authRequest(
                "account/authenticators/totp",
                "GET",
            );
            if (await confirmedFirst(result, startSetup)) return;
            const { secret, totp_url: totpUrl } = result.meta ?? {};
            if (result.status === 200 || !secret || !totpUrl) {
                setLoadCount((count) => count + 1);
                return;
            }
            setSetupCode("");
            show({ kind: "enrolling", secret, totpUrl });
        });

    const activate = (
        enrolling: Extract<TwoFactorView, { kind: "enrolling" }>,
        code: string,
    ): Promise<void> =>
        run("activate", async () => {
            const result = await authRequest(
                "account/authenticators/totp",
                "POST",
                { code },
            );
            // A confirmation that lapsed while the code was typed asks for it again; the code
            // has expired by then, so the setup asks for a fresh one.
            const fresh = () => {
                setSetupCode("");
                setNotice("enterFreshCode");
                show(enrolling);
                return Promise.resolve();
            };
            if (await confirmedFirst(result, fresh)) return;
            setSetupCode("");
            const facts = await currentFacts();
            setNotice("authenticatorActivated");
            const codes = result.meta?.recovery_codes;
            show(
                codes?.length
                    ? { kind: "codes", facts, codes }
                    : { kind: "enabled", facts },
            );
        });

    const replaceCodes = (): Promise<void> =>
        run("replace", async () => {
            const result = await authRequest(
                "account/authenticators/recovery-codes",
                "POST",
                {},
            );
            if (await confirmedFirst(result, replaceCodes)) return;
            const facts = await currentFacts();
            setNotice("codesReplaced");
            show({
                kind: "codes",
                facts,
                codes: result.data?.unused_codes ?? [],
            });
        });

    const remove = (): Promise<void> =>
        run("remove", async () => {
            const result = await authRequest(
                "account/authenticators/totp",
                "DELETE",
            );
            if (await confirmedFirst(result, remove)) return;
            setSetupCode("");
            setNotice("authenticatorRemoved");
            show({ kind: "disabled" });
        });

    async function copy(text: string, copied: StatusNotice) {
        setError(undefined);
        try {
            if (!navigator.clipboard?.writeText) throw new Error();
            await navigator.clipboard.writeText(text);
            setNotice(copied);
        } catch {
            fail(t("copyFailed"));
        }
    }

    function download(codes: string[]) {
        setError(undefined);
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
                blobUrls.current.delete(entry);
            }, 1000),
        };
        blobUrls.current.add(entry);
        setNotice("codesDownloaded");
    }

    const facts: Facts = "facts" in view ? view.facts : {};
    const busy = running !== undefined || view.kind === "loading";
    const activeError =
        error && "lost" in error
            ? writeErrorMessage(error.lost)
            : error?.locale === locale
              ? error.text
              : undefined;
    const activeCodeError =
        codeError?.locale === locale ? codeError.text : undefined;

    return (
        <section className="panel narrow" aria-busy={busy || undefined}>
            <div className="mfa-header">
                <h2>{t("twoFactor")}</h2>
                <TwoFactorStatusTag view={view} mfaRequired={mfaRequired} />
            </div>
            {view.kind === "disabled" && <p>{t("authenticatorHelp")}</p>}
            {notice && (
                <div className="notice" role="status">
                    {t(notice)}
                </div>
            )}
            {activeError && (
                <div
                    ref={errorRef}
                    className="notice error"
                    role="alert"
                    tabIndex={-1}
                >
                    {error && "lost" in error ? (
                        <>
                            <UnconfirmedTitle error={error.lost} />
                            <p>{activeError}</p>
                        </>
                    ) : (
                        activeError
                    )}
                </div>
            )}
            <StepUpPrompt
                stepUp={stepUp}
                email={email}
                onError={(message) => fail(message)}
            />
            {view.kind === "loading" && (
                <div role="status">
                    <p>{t("loading")}</p>
                    <div className="loading-indicator" aria-hidden="true" />
                </div>
            )}
            {view.kind === "loadError" && (
                <LoadError
                    message={view.message}
                    busy={busy}
                    onRetry={() => {
                        setError(undefined);
                        setView({ kind: "loading" });
                        setLoadCount((count) => count + 1);
                    }}
                />
            )}
            {!stepUp.pending && view.kind === "disabled" && (
                <div className="mfa-actions">
                    {mfaRequired ? (
                        <div className="notice" role="status">
                            {t("policySetupUnavailable")}
                        </div>
                    ) : (
                        <button
                            className="button"
                            type="button"
                            disabled={busy}
                            onClick={() => void startSetup()}
                        >
                            {running === "setup"
                                ? t("working")
                                : t("setupAuthenticator")}
                        </button>
                    )}
                </div>
            )}
            {!stepUp.pending && view.kind === "enrolling" && (
                <AuthenticatorSetup
                    headingRef={stepHeadingRef}
                    secret={view.secret}
                    totpUrl={view.totpUrl}
                    code={setupCode}
                    onCodeChange={setSetupCode}
                    error={activeCodeError}
                    busy={busy}
                    verifying={running === "activate"}
                    onCopyKey={() => void copy(view.secret, "setupKeyCopied")}
                    onActivate={() => {
                        const code = setupCode.trim();
                        if (!code) {
                            setCodeError({ text: t("enterCode"), locale });
                            return;
                        }
                        void activate(view, code);
                    }}
                    onCancel={() => {
                        setSetupCode("");
                        setError(undefined);
                        setCodeError(undefined);
                        show({ kind: "disabled" });
                    }}
                />
            )}
            {!stepUp.pending && view.kind === "enabled" && (
                <EnabledPanel
                    facts={view.facts}
                    mfaRequired={mfaRequired}
                    busy={busy}
                    onReplace={() => show({ kind: "confirmReplace", facts })}
                    onRemove={() => show({ kind: "confirmRemove", facts })}
                />
            )}
            {view.kind === "codes" && (
                <RecoveryCodes
                    headingRef={stepHeadingRef}
                    codes={view.codes}
                    busy={busy}
                    onCopy={() =>
                        void copy(view.codes.join("\n"), "codesCopied")
                    }
                    onDownload={() => download(view.codes)}
                    onAcknowledge={() => {
                        setError(undefined);
                        setNotice("recoverySavedAck");
                        show({ kind: "enabled", facts });
                    }}
                />
            )}
            {!stepUp.pending && view.kind === "confirmReplace" && (
                <ConfirmStep
                    headingRef={stepHeadingRef}
                    action="replace"
                    busy={busy}
                    working={running === "replace"}
                    onConfirm={() => void replaceCodes()}
                    onCancel={() => show({ kind: "enabled", facts })}
                />
            )}
            {!stepUp.pending &&
                view.kind === "confirmRemove" &&
                !mfaRequired && (
                    <ConfirmStep
                        headingRef={stepHeadingRef}
                        action="remove"
                        busy={busy}
                        working={running === "remove"}
                        onConfirm={() => void remove()}
                        onCancel={() => show({ kind: "enabled", facts })}
                    />
                )}
        </section>
    );
}
