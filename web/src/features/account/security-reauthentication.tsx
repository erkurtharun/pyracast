"use client";

import { useTranslations } from "next-intl";
import { useEffect, useId, useRef, useState, type SubmitEvent } from "react";
import { loadSession } from "@/lib/session-client";
import { ApiError, displayMessage } from "@/lib/api/errors";
import { authRequest } from "@/lib/auth-api";
import {
    assertPasskey,
    cancelledPasskey,
    usePasskeySignal,
    usePasskeySupport,
} from "@/lib/passkeys";

export type ReauthMode =
    | "mfa"
    | "password"
    | "email"
    | "passkey"
    | "mfaOrPasskey";

export async function refreshExpiredSession(
    expiredMessage: string,
): Promise<never> {
    await loadSession();
    throw new ApiError(expiredMessage, { status: 401 });
}

export function ReauthenticationForm({
    mode,
    email,
    onVerified,
    onCancel,
    disabled = false,
    autoFocus = false,
}: {
    mode: ReauthMode;
    email: string;
    onVerified: () => Promise<void> | void;
    onCancel?: () => void;
    disabled?: boolean;
    autoFocus?: boolean;
}) {
    const t = useTranslations("Security");
    const passkeys = useTranslations("Passkeys");
    const passkeySupported = usePasskeySupport();
    const signalForPasskey = usePasskeySignal();
    const codeMfa = mode === "mfa" || mode === "mfaOrPasskey";
    const passkeyMfa = mode === "passkey" || mode === "mfaOrPasskey";
    const inputId = useId();
    const fieldName = mode === "password" ? "password" : "code";
    const [codeSent, setCodeSent] = useState(false);
    const [value, setValue] = useState("");
    const [submitting, setSubmitting] = useState(false);
    const [error, setError] = useState<string>();
    const [fieldError, setFieldError] = useState<string>();
    const mountedRef = useRef(true);
    const pendingRef = useRef(false);
    const inputRef = useRef<HTMLInputElement>(null);
    const errorRef = useRef<HTMLDivElement>(null);
    const submitRef = useRef<HTMLButtonElement>(null);

    useEffect(() => {
        mountedRef.current = true;
        if (autoFocus) (inputRef.current ?? submitRef.current)?.focus();
        return () => {
            mountedRef.current = false;
            pendingRef.current = false;
        };
    }, [autoFocus]);

    useEffect(() => {
        if (codeSent) inputRef.current?.focus();
    }, [codeSent]);

    async function verify(byPasskey: boolean) {
        if (disabled || pendingRef.current) return;
        const payloadValue = mode === "password" ? value : value.trim();
        if (!byPasskey && !payloadValue && !(mode === "email" && !codeSent)) {
            setFieldError(t("enterCode"));
            inputRef.current?.focus();
            return;
        }

        pendingRef.current = true;
        setSubmitting(true);
        setError(undefined);
        setFieldError(undefined);

        try {
            if (mode === "email" && !codeSent) {
                const sent = await authRequest(
                    "auth/reauthenticate/code/request",
                    "POST",
                    { email },
                );
                if (sent.status === 401 && !sent.meta?.is_authenticated)
                    await refreshExpiredSession(t("sessionExpired"));
                if (sent.status !== 200)
                    throw new ApiError(t("reauthStillRequired"));
                if (mountedRef.current) setCodeSent(true);
                return;
            }
            const result = byPasskey
                ? await assertPasskey("reauthenticate", signalForPasskey())
                : await authRequest(
                      codeMfa
                          ? "auth/2fa/reauthenticate"
                          : mode === "email"
                            ? "auth/reauthenticate/code/confirm"
                            : "auth/reauthenticate",
                      "POST",
                      mode !== "password"
                          ? { code: payloadValue }
                          : { password: payloadValue },
                  );

            if (result.status === 401) {
                if (!result.meta?.is_authenticated) {
                    await refreshExpiredSession(t("sessionExpired"));
                }
                throw new ApiError(
                    result.errors?.map((item) => item.message).join(" ") ||
                        (byPasskey
                            ? passkeys("failed")
                            : codeMfa
                              ? t("verifyAuthenticator")
                              : t("confirmPasswordFirst")),
                );
            }

            if (!mountedRef.current) return;
            setValue("");
            await onVerified();
        } catch (err) {
            if (!mountedRef.current) return;
            const message = cancelledPasskey(err)
                ? passkeys("cancelled")
                : displayMessage(err, t("reauthStillRequired"));
            const nextFieldError =
                !byPasskey && err instanceof ApiError
                    ? err.fields[fieldName]
                    : undefined;
            setError(message);
            setFieldError(nextFieldError);
            requestAnimationFrame(() => {
                errorRef.current?.focus();
            });
        } finally {
            if (mountedRef.current) {
                pendingRef.current = false;
                setSubmitting(false);
            }
        }
    }

    function handleSubmit(event: SubmitEvent<HTMLFormElement>) {
        event.preventDefault();
        return verify(mode === "passkey");
    }

    const defaultLabel = codeMfa
        ? t("authenticatorOrRecovery")
        : mode === "email"
          ? t("emailReauthCode")
          : t("currentPassword");
    const defaultSubmit = codeMfa
        ? t("confirmAuthenticator")
        : mode === "email"
          ? codeSent
              ? t("confirmEmailCode")
              : t("sendEmailCode")
          : t("confirmPassword");

    return (
        <form onSubmit={(event) => void handleSubmit(event)}>
            {error && (
                <div
                    ref={errorRef}
                    className="notice error"
                    role="alert"
                    tabIndex={-1}
                >
                    {error}
                </div>
            )}
            <fieldset disabled={disabled || submitting}>
                {mode === "email" && (
                    <p>
                        {codeSent
                            ? t("emailReauthCheck")
                            : t("emailReauthDescription", { email })}
                    </p>
                )}
                {mode !== "passkey" && (mode !== "email" || codeSent) && (
                    <div className="field">
                        <label htmlFor={inputId}>{defaultLabel}</label>
                        <input
                            ref={inputRef}
                            id={inputId}
                            name={fieldName}
                            type={mode === "password" ? "password" : "text"}
                            autoComplete={
                                mode !== "password"
                                    ? "one-time-code"
                                    : "current-password"
                            }
                            required
                            value={value}
                            onChange={(event) => setValue(event.target.value)}
                            aria-invalid={fieldError ? true : undefined}
                            aria-describedby={
                                fieldError ? `${inputId}-error` : undefined
                            }
                        />
                        {fieldError && (
                            <span
                                className="field-error"
                                id={`${inputId}-error`}
                            >
                                {fieldError}
                            </span>
                        )}
                    </div>
                )}
                <div className="button-group">
                    {mode !== "passkey" && (
                        <button ref={submitRef} className="button" type="submit">
                            {submitting ? t("verifying") : defaultSubmit}
                        </button>
                    )}
                    {passkeyMfa && passkeySupported && (
                        <button
                            ref={mode === "passkey" ? submitRef : undefined}
                            className="button secondary"
                            type="button"
                            onClick={() => void verify(true)}
                        >
                            {submitting ? t("verifying") : passkeys("verify")}
                        </button>
                    )}
                    {onCancel && (
                        <button
                            className="button secondary"
                            type="button"
                            onClick={() => {
                                setValue("");
                                setError(undefined);
                                setFieldError(undefined);
                                onCancel();
                            }}
                        >
                            {t("cancel")}
                        </button>
                    )}
                </div>
                {mode === "passkey" && !passkeySupported && (
                    <p>{passkeys("unsupported")}</p>
                )}
            </fieldset>
        </form>
    );
}
