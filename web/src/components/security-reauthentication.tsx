"use client";

import { useTranslations } from "next-intl";
import { useEffect, useId, useRef, useState } from "react";
import { ApiError, request, type Session } from "@/lib/api";
import {
    authRequest,
    AuthResponseError,
    type AuthResult,
} from "@/lib/auth-api";

export type ReauthMode = "mfa" | "password";

export function getReauthMode(result: AuthResult<unknown>): ReauthMode {
    return result.errors?.some(
        (error) => error.code === "mfa_reauthentication_required",
    )
        ? "mfa"
        : "password";
}

export async function refreshExpiredSession(
    onSession: (session: Session) => void,
    expiredMessage: string,
): Promise<never> {
    onSession(await request<Session>("/api/session/"));
    throw new AuthResponseError(expiredMessage, 401);
}

export function ReauthenticationForm({
    mode,
    token,
    onSession,
    onVerified,
    onCancel,
    disabled = false,
    autoFocus = false,
    submitLabel,
    label,
}: {
    mode: ReauthMode;
    token: string;
    onSession: (session: Session) => void;
    onVerified: () => Promise<void> | void;
    onCancel?: () => void;
    disabled?: boolean;
    autoFocus?: boolean;
    submitLabel?: string;
    label?: string;
}) {
    const t = useTranslations("Security");
    const inputId = useId();
    const fieldName = mode === "mfa" ? "code" : "password";
    const [value, setValue] = useState("");
    const [submitting, setSubmitting] = useState(false);
    const [error, setError] = useState<string>();
    const [fieldError, setFieldError] = useState<string>();
    const mountedRef = useRef(true);
    const pendingRef = useRef(false);
    const inputRef = useRef<HTMLInputElement>(null);
    const errorRef = useRef<HTMLDivElement>(null);

    useEffect(() => {
        mountedRef.current = true;
        if (autoFocus) inputRef.current?.focus();
        return () => {
            mountedRef.current = false;
            pendingRef.current = false;
        };
    }, [autoFocus]);

    async function handleSubmit(event: React.FormEvent<HTMLFormElement>) {
        event.preventDefault();
        if (disabled || pendingRef.current) return;
        const payloadValue = mode === "mfa" ? value.trim() : value;
        if (!payloadValue) {
            setFieldError(t("enterCode"));
            inputRef.current?.focus();
            return;
        }

        pendingRef.current = true;
        setSubmitting(true);
        setError(undefined);
        setFieldError(undefined);

        try {
            const result = await authRequest(
                mode === "mfa"
                    ? "auth/2fa/reauthenticate"
                    : "auth/reauthenticate",
                "POST",
                token,
                mode === "mfa"
                    ? { code: payloadValue }
                    : { password: payloadValue },
            );

            if (result.status === 401) {
                if (!result.meta?.is_authenticated) {
                    await refreshExpiredSession(onSession, t("sessionExpired"));
                }
                throw new ApiError(
                    result.errors?.map((item) => item.message).join(" ") ||
                        (mode === "mfa"
                            ? t("verifyAuthenticator")
                            : t("confirmPasswordFirst")),
                );
            }

            if (!mountedRef.current) return;
            setValue("");
            await onVerified();
        } catch (err) {
            if (!mountedRef.current) return;
            const message =
                err instanceof Error ? err.message : t("reauthStillRequired");
            const nextFieldError =
                err instanceof ApiError ? err.fields[fieldName] : undefined;
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

    const defaultLabel =
        mode === "mfa" ? t("authenticatorOrRecovery") : t("currentPassword");
    const defaultSubmit =
        mode === "mfa" ? t("confirmAuthenticator") : t("confirmPassword");

    return (
        <form onSubmit={handleSubmit}>
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
                <div className="field">
                    <label htmlFor={inputId}>{label ?? defaultLabel}</label>
                    <input
                        ref={inputRef}
                        id={inputId}
                        name={fieldName}
                        type={mode === "mfa" ? "text" : "password"}
                        autoComplete={
                            mode === "mfa"
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
                        <span className="field-error" id={`${inputId}-error`}>
                            {fieldError}
                        </span>
                    )}
                </div>
                <div className="button-group">
                    <button className="button" type="submit">
                        {submitting
                            ? t("verifying")
                            : (submitLabel ?? defaultSubmit)}
                    </button>
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
            </fieldset>
        </form>
    );
}
