"use client";

import { useTranslations } from "next-intl";
import { useState } from "react";
import { ApiError, request, type Session } from "@/lib/api";
import { authRequest } from "@/lib/auth-api";
import {
    getReauthMode,
    ReauthenticationForm,
    type ReauthMode,
    refreshExpiredSession,
} from "./security-reauthentication";
import { LinkedAccounts } from "./linked-accounts";
import { TwoFactorSettings } from "./two-factor-settings";
import { ActionForm, Field } from "./ui";

export function AccountSecurity({
    token,
    mfaRequired,
    hasPassword,
    email,
    onSession,
}: {
    token: string;
    mfaRequired: boolean;
    hasPassword: boolean;
    email: string;
    onSession: (session: Session) => void;
}) {
    const t = useTranslations("Security");
    const [pendingPassword, setPendingPassword] = useState<{
        mode: ReauthMode;
        payload: Record<string, FormDataEntryValue>;
    }>();
    async function changePassword(
        payload: Record<string, FormDataEntryValue>,
        replay = false,
    ) {
        const result = await authRequest(
            "account/password/change",
            "POST",
            token,
            payload,
        );
        if (result.status === 401) {
            if (!result.meta?.is_authenticated) {
                if (result.meta?.password_changed) {
                    setPendingPassword(undefined);
                    try {
                        window.sessionStorage.setItem(
                            "pyracast.passwordChanged",
                            "1",
                        );
                    } catch {
                        // Browser privacy settings can disable session storage.
                    }
                    onSession(await request<Session>("/api/session/"));
                    return;
                }
                await refreshExpiredSession(onSession, t("sessionExpired"));
            }
            if (replay) throw new ApiError(t("reauthStillRequired"));
            setPendingPassword({
                mode: getReauthMode(result, hasPassword),
                payload,
            });
            return;
        }
        setPendingPassword(undefined);
        onSession(await request<Session>("/api/session/"));
    }

    return (
        <>
            <section className="panel narrow">
                <details className="account-disclosure password-disclosure">
                    <summary>
                        <h2>
                            {t(hasPassword ? "changePassword" : "setPassword")}
                        </h2>
                    </summary>
                    {pendingPassword ? (
                        <ReauthenticationForm
                            mode={pendingPassword.mode}
                            email={email}
                            token={token}
                            onSession={onSession}
                            autoFocus
                            onCancel={() => setPendingPassword(undefined)}
                            onVerified={() =>
                                changePassword(pendingPassword.payload, true)
                            }
                        />
                    ) : (
                        <ActionForm
                            submit={t(
                                hasPassword ? "changePassword" : "setPassword",
                            )}
                            onSubmit={async (data) =>
                                changePassword(Object.fromEntries(data))
                            }
                        >
                            {hasPassword && (
                                <Field
                                    label={t("currentPassword")}
                                    name="current_password"
                                    type="password"
                                    autoComplete="current-password"
                                />
                            )}
                            <Field
                                label={t("newPassword")}
                                name="new_password"
                                type="password"
                                autoComplete="new-password"
                                minLength={12}
                            />
                        </ActionForm>
                    )}
                </details>
            </section>
            <TwoFactorSettings
                token={token}
                mfaRequired={mfaRequired}
                hasPassword={hasPassword}
                email={email}
                onSession={onSession}
            />
            <LinkedAccounts
                token={token}
                email={email}
                hasPassword={hasPassword}
                onSession={onSession}
            />
        </>
    );
}
