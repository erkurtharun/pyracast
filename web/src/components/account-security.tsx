"use client";
import { useLocale, useTranslations } from "next-intl";

import { useState } from "react";
import { ApiError, request, type Session } from "@/lib/api";
import { authRequest, type AuthResult } from "@/lib/auth-api";
import { ActionForm, Field } from "./ui";

type SecurityMessage =
    | {
          kind: "notice";
          key:
              | "identityConfirmed"
              | "authenticatorConfirmed"
              | "authenticatorActivated"
              | "codesAvailable"
              | "codesReplaced"
              | "authenticatorRemoved";
      }
    | { kind: "error"; text: string; locale: string };

export function AccountSecurity({
    token,
    onSession,
}: {
    token: string;
    onSession: (session: Session) => void;
}) {
    const t = useTranslations("Security");
    const locale = useLocale();
    const [setup, setSetup] = useState<{
        secret?: string;
        totp_url?: string;
    }>();
    const [codes, setCodes] = useState<string[]>([]);
    const [message, setMessage] = useState<SecurityMessage>();
    async function call(
        path: string,
        method: string,
        body?: unknown,
    ): Promise<AuthResult> {
        const result = await authRequest(path, method, token, body);
        if (result.status === 401 && !result.meta?.is_authenticated) {
            onSession(await request<Session>("/api/session/"));
            throw new ApiError(t("sessionExpired"));
        }
        if (result.status === 401)
            throw new ApiError(
                result.errors?.some(
                    (error) => error.code === "mfa_reauthentication_required",
                )
                    ? t("verifyAuthenticator")
                    : t("confirmPasswordFirst"),
            );
        return result;
    }
    return (
        <>
            <section className="panel narrow">
                <h2>{t("identity")}</h2>
                <p>{t("sensitiveChanges")}</p>
                <ActionForm
                    submit={t("confirmPassword")}
                    onSubmit={async (data) => {
                        await call(
                            "auth/reauthenticate",
                            "POST",
                            Object.fromEntries(data),
                        );
                        setMessage({
                            kind: "notice",
                            key: "identityConfirmed",
                        });
                    }}
                >
                    <Field
                        label={t("currentPassword")}
                        name="password"
                        type="password"
                        autoComplete="current-password"
                    />
                </ActionForm>
                <ActionForm
                    submit={t("confirmAuthenticator")}
                    onSubmit={async (data) => {
                        await call(
                            "auth/2fa/reauthenticate",
                            "POST",
                            Object.fromEntries(data),
                        );
                        setMessage({
                            kind: "notice",
                            key: "authenticatorConfirmed",
                        });
                    }}
                >
                    <Field
                        label={t("authenticatorOrRecovery")}
                        name="code"
                        autoComplete="one-time-code"
                    />
                </ActionForm>
                {message?.kind === "notice" && (
                    <p role="status">{t(message.key)}</p>
                )}
                {message?.kind === "error" && message.locale === locale && (
                    <p role="alert">{message.text}</p>
                )}
            </section>
            <section className="panel narrow">
                <h2>{t("changePassword")}</h2>
                <ActionForm
                    submit={t("changePassword")}
                    onSubmit={async (data) => {
                        const result = await authRequest(
                            "account/password/change",
                            "POST",
                            token,
                            Object.fromEntries(data),
                        );
                        if (result.status === 401) {
                            const session =
                                await request<Session>("/api/session/");
                            if (!session.user) {
                                onSession(session);
                                return;
                            }
                            throw new ApiError(t("confirmIdentityFirst"));
                        }
                    }}
                >
                    <Field
                        label={t("currentPassword")}
                        name="current_password"
                        type="password"
                        autoComplete="current-password"
                    />
                    <Field
                        label={t("newPassword")}
                        name="new_password"
                        type="password"
                        autoComplete="new-password"
                        minLength={12}
                    />
                </ActionForm>
            </section>
            <section className="panel narrow">
                <h2>{t("twoFactor")}</h2>
                <p>{t("authenticatorHelp")}</p>
                <button
                    className="button"
                    type="button"
                    onClick={async () => {
                        try {
                            const result = await call(
                                "account/authenticators/totp",
                                "GET",
                            );
                            setSetup(result.meta);
                            setMessage(undefined);
                        } catch (error) {
                            setMessage({
                                kind: "error",
                                text: (error as Error).message,
                                locale,
                            });
                        }
                    }}
                >
                    {t("setupAuthenticator")}
                </button>
                {setup?.secret && (
                    <>
                        <p>
                            {t("setupKey")} <code>{setup.secret}</code>
                        </p>
                        <ActionForm
                            submit={t("activateAuthenticator")}
                            onSubmit={async (data) => {
                                await call(
                                    "account/authenticators/totp",
                                    "POST",
                                    Object.fromEntries(data),
                                );
                                setSetup(undefined);
                                setMessage({
                                    kind: "notice",
                                    key: "authenticatorActivated",
                                });
                            }}
                        >
                            <Field
                                label={t("authenticatorCode")}
                                name="code"
                                autoComplete="one-time-code"
                            />
                        </ActionForm>
                    </>
                )}
                <button
                    className="text-button"
                    type="button"
                    onClick={async () => {
                        try {
                            const result = await call(
                                "account/authenticators/recovery-codes",
                                "GET",
                            );
                            setCodes(
                                (result.data?.unused_codes as
                                    string[] | undefined) ?? [],
                            );
                            setMessage({
                                kind: "notice",
                                key: "codesAvailable",
                            });
                        } catch (error) {
                            setMessage({
                                kind: "error",
                                text: (error as Error).message,
                                locale,
                            });
                        }
                    }}
                >
                    {t("showRecovery")}
                </button>
                <button
                    className="text-button"
                    type="button"
                    onClick={async () => {
                        try {
                            const result = await call(
                                "account/authenticators/recovery-codes",
                                "POST",
                                {},
                            );
                            setCodes(
                                (result.data?.unused_codes as
                                    string[] | undefined) ?? [],
                            );
                            setMessage({
                                kind: "notice",
                                key: "codesReplaced",
                            });
                        } catch (error) {
                            setMessage({
                                kind: "error",
                                text: (error as Error).message,
                                locale,
                            });
                        }
                    }}
                >
                    {t("replaceRecovery")}
                </button>
                {codes.length > 0 && (
                    <ul>
                        {codes.map((code) => (
                            <li key={code}>
                                <code>{code}</code>
                            </li>
                        ))}
                    </ul>
                )}
                <button
                    className="text-button"
                    type="button"
                    onClick={async () => {
                        try {
                            await call("account/authenticators/totp", "DELETE");
                            setCodes([]);
                            setMessage({
                                kind: "notice",
                                key: "authenticatorRemoved",
                            });
                        } catch (error) {
                            setMessage({
                                kind: "error",
                                text: (error as Error).message,
                                locale,
                            });
                        }
                    }}
                >
                    {t("removeAuthenticator")}
                </button>
            </section>
        </>
    );
}
